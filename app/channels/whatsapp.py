"""WhatsApp Cloud API (Meta) adapter: webhook parsing, media download, sending."""
from __future__ import annotations

import hashlib
import hmac
import logging

import httpx

from .. import config
from ..engine import Incoming, Out

log = logging.getLogger("straywatch.whatsapp")


def enabled() -> bool:
    return bool(config.WA_TOKEN and config.WA_PHONE_ID)


def verify_signature(body: bytes, header: str | None) -> bool:
    """Checks Meta's X-Hub-Signature-256 when an app secret is configured."""
    if not config.WA_APP_SECRET:
        return True
    if not header or not header.startswith("sha256="):
        return False
    want = hmac.new(config.WA_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header[7:])


def parse(payload: dict) -> list[tuple[str, str, Incoming, str | None]]:
    """Webhook JSON -> [(message_id, phone, Incoming, media_id)]. Status updates are ignored."""
    out = []
    for entry in payload.get("entry", []):
        for ch in entry.get("changes", []):
            for m in ch.get("value", {}).get("messages", []) or []:
                phone, mid, t = m.get("from"), m.get("id"), m.get("type")
                inc, media = Incoming(), None
                if t == "text":
                    inc.text = m["text"].get("body", "")
                elif t == "image":
                    media = m["image"]["id"]
                    inc.text = m["image"].get("caption", "")
                elif t == "location":
                    inc.lat, inc.lon = m["location"]["latitude"], m["location"]["longitude"]
                elif t == "interactive":
                    it = m["interactive"]
                    if it.get("type") == "button_reply":
                        inc.button = it["button_reply"]["id"]
                    elif it.get("type") == "list_reply":
                        inc.button = it["list_reply"]["id"]
                elif t == "button":  # template quick-reply
                    inc.text = m["button"].get("text", "")
                else:
                    inc.text = ""
                if phone:
                    out.append((mid, phone, inc, media))
    return out


def download_media(media_id: str) -> bytes | None:
    h = {"Authorization": f"Bearer {config.WA_TOKEN}"}
    try:
        with httpx.Client(timeout=30) as c:
            meta = c.get(f"{config.GRAPH}/{media_id}", headers=h).json()
            r = c.get(meta["url"], headers=h)
            r.raise_for_status()
            return r.content
    except Exception as e:  # noqa: BLE001
        log.warning("media download failed: %s", e)
        return None


def _post(payload: dict) -> bool:
    try:
        r = httpx.post(f"{config.GRAPH}/{config.WA_PHONE_ID}/messages", timeout=20,
                       headers={"Authorization": f"Bearer {config.WA_TOKEN}"},
                       json={"messaging_product": "whatsapp", **payload})
        if r.status_code >= 300:
            log.warning("WhatsApp send failed %s: %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("WhatsApp send error: %s", e)
        return False


def send_alert_template(phone: str, summary: str) -> bool:
    """Outside WhatsApp's 24-hour window a business may only start a chat with an approved template.
    Template (create in WhatsApp Manager, category Utility), one body variable:
    'An animal near you needs help: {{1}}. Reply to this message to see the case.'"""
    if not config.WA_ALERT_TEMPLATE:
        return False
    return _post({"to": phone, "type": "template", "template": {
        "name": config.WA_ALERT_TEMPLATE, "language": {"code": config.WA_TEMPLATE_LANG},
        "components": [{"type": "body", "parameters": [{"type": "text", "text": summary[:200]}]}]}})


def send(phone: str, o: Out) -> bool:
    """One engine message -> one or two WhatsApp messages (image first, then text with buttons)."""
    text = o.text + (f"\n{o.link}" if o.link else "")
    text = text[:1020]
    if o.image_url and not o.image_url.startswith("http://localhost"):
        _post({"to": phone, "type": "image", "image": {"link": o.image_url}})
    if o.buttons:
        return _post({"to": phone, "type": "interactive", "interactive": {
            "type": "button", "body": {"text": text},
            "action": {"buttons": [{"type": "reply", "reply": {"id": i[:256], "title": t[:20]}}
                                   for i, t in o.buttons[:3]]}}})
    if o.options:
        return _post({"to": phone, "type": "interactive", "interactive": {
            "type": "list", "body": {"text": text},
            "action": {"button": "Choose", "sections": [{"title": "Options", "rows": [
                {"id": i[:200], "title": t[:24]} for i, t in o.options[:10]]}]}}})
    if o.ask_location:
        return _post({"to": phone, "type": "interactive", "interactive": {
            "type": "location_request_message", "body": {"text": text}, "action": {"name": "send_location"}}})
    return _post({"to": phone, "type": "text", "text": {"body": text, "preview_url": bool(o.link)}})
