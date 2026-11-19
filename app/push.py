"""Web Push: phone notifications for responders and reporters, even when StrayWatch isn't open.

Without this, a responder using the app (not WhatsApp) would only see an alert if the page happened to be open.
The signing key (VAPID) is created once and kept in the database, so it survives restarts.
Sending happens on a background thread so a slow push service never delays a rescue turn.
"""
from __future__ import annotations

import base64
import json
import logging
import queue
import re
import threading

from sqlalchemy import select

from .db import PushSub, SessionLocal, Setting

log = logging.getLogger("straywatch.push")
_q: "queue.Queue[tuple[str, dict]]" = queue.Queue(maxsize=2000)
_worker: threading.Thread | None = None
_vapid = None
_public_b64 = ""


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def keys(s=None) -> str:
    """Load (or create once) the VAPID key pair. Returns the public key for the browser."""
    global _vapid, _public_b64
    if _vapid is not None:
        return _public_b64
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid
    own = s is None
    s = s or SessionLocal()
    try:
        row = s.get(Setting, "vapid_pem")
        if row:
            v = Vapid.from_pem(row.value.encode())
        else:
            v = Vapid()
            v.generate_keys()
            s.add(Setting(key="vapid_pem", value=v.private_pem().decode()))
            s.commit()
        _vapid = v
        _public_b64 = _b64url(v.public_key.public_bytes(serialization.Encoding.X962,
                                                        serialization.PublicFormat.UncompressedPoint))
        return _public_b64
    finally:
        if own:
            s.close()


def subscribe(s, user_id: str, sub: dict) -> bool:
    ep = str(sub.get("endpoint") or "")
    k = sub.get("keys") or {}
    if not ep.startswith("https://") or not k.get("p256dh") or not k.get("auth"):
        return False
    old = s.scalars(select(PushSub).where(PushSub.endpoint == ep)).first()
    if old:
        old.user_id = user_id
    else:
        s.add(PushSub(user_id=user_id, endpoint=ep[:2000], p256dh=str(k["p256dh"])[:200], auth=str(k["auth"])[:80]))
    return True


def has_subscription(s, user_id: str) -> bool:
    return s.scalars(select(PushSub.id).where(PushSub.user_id == user_id)).first() is not None


def notify(user_id: str, text: str, url: str = "/app"):
    """Queue a notification. First line becomes the title; markdown stars are removed."""
    clean = re.sub(r"\*([^*]+)\*", r"\1", text or "").strip()
    lines = [x for x in clean.split("\n") if x.strip()]
    title = (lines[0] if lines else "StrayWatch")[:80]
    body = " ".join(lines[1:])[:180] or "Open StrayWatch"
    try:
        _q.put_nowait((user_id, {"title": title, "body": body, "url": url}))
    except queue.Full:
        log.warning("push queue full; dropping a notification")
        return
    _start()


def _start():
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_run, name="webpush", daemon=True)
        _worker.start()


def _run():
    from pywebpush import WebPushException, webpush
    while True:
        user_id, payload = _q.get()
        try:
            with SessionLocal() as s:
                keys(s)
                for sub in list(s.scalars(select(PushSub).where(PushSub.user_id == user_id))):
                    try:
                        webpush(subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                                data=json.dumps(payload), vapid_private_key=_vapid,
                                vapid_claims={"sub": "mailto:alerts@straywatch.app"}, ttl=3600)
                    except WebPushException as e:
                        code = getattr(getattr(e, "response", None), "status_code", None)
                        if code in (404, 410):  # the phone unsubscribed: forget it
                            s.delete(sub)
                        else:
                            log.warning("push failed (%s): %s", code, e)
                s.commit()
        except Exception:  # noqa: BLE001
            log.exception("push worker error")
