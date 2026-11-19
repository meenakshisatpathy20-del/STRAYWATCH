"""Build app/static/i18n.js: the installable app's own labels (tabs, buttons, hints) in all 6 languages.

Chat messages come from the server already translated; these are the words printed on the app screen itself.
Run (needs ADAPTION_API_KEY):  python scripts/build_ui_i18n.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import ai  # noqa: E402

EN = {
    "tab_chat": "Chat", "tab_lost": "Lost & Found", "tab_more": "More", "install": "Install app", "lang_btn": "Language",
    "placeholder": "Type in any language…",
    "push_bar": "🔔 Get alerts on this phone, even when the app is closed.", "push_on": "Turn on",
    "lost_title": "Help them get home", "lost_none": "No lost animals right now. 💛",
    "lost_note": "Lost your animal? Go to Chat → Lost & found.",
    "my_reports": "My reports", "help_more": "Help more", "loading": "Loading…",
    "vol_title": "🦸 Become a volunteer", "vol_desc": "Get alerts for hurt animals within 8 km. At most 6 a day; pause any time.",
    "notif_title": "🔔 Notifications", "notif_off": "Turn on alerts for this phone", "notif_on": "On ✅",
    "notif_blocked": "Blocked: allow notifications in your browser settings",
    "notif_unsupported": "Not supported in this browser",
    "vet_title": "🩺 Vet care", "vet_desc": "Talk to a vet online, animal ambulance 1962",
    "city_title": "City view", "city_desc": "Where animals get hurt, rescue times and vaccination near you",
    "promise_title": "Our promise", "promise_desc": "Safety, privacy, and how the AI is used",
    "wa_title": "WhatsApp add-on", "wa_desc": "Coming soon: use StrayWatch from WhatsApp too",
    "wa_on_title": "Also on WhatsApp", "wa_on_desc": "Use StrayWatch from WhatsApp too",
    "foot": "StrayWatch helps people help animals. It does not replace a vet or emergency services. If a person is hurt, call 112.",
    "reports_none": "No reports yet. See an animal in trouble? Go to Chat and tap “Hurt animal: SOS”.",
    "reports_err": "Couldn't load your reports. Check your network.",
    "share_loc": "📍 Share my location", "typing": "StrayWatch is looking…",
    "network_err": "Network problem. Please try again. 🙏",
    "storage_full": "Network problem, and your phone's storage is full. Please try again, or call 1962. 🙏",
    "push_unsupported": "This browser can't show notifications. Keep StrayWatch open, or use the WhatsApp add-on.",
    "photo_btn": "Send a photo", "send_btn": "Send", "loc_btn": "Share location",
}
LANGS = ["hi", "bn", "ta", "te", "mr"]


def main():
    out_path = ROOT / "app" / "static" / "i18n.js"
    old = {}
    if out_path.exists():
        m = re.search(r"const I18N = (\{.*?\});\n", out_path.read_text(encoding="utf-8"), re.S)
        old = json.loads(m.group(1)) if m else {}
    table = {"en": EN}
    keys = list(EN)
    for lang in LANGS:
        have = {k: v for k, v in old.get(lang, {}).items() if k in EN}
        todo = [k for k in keys if k not in have]
        for i in range(0, len(todo), 12):
            chunk = todo[i:i + 12]
            for k, tr in zip(chunk, ai.translate_batch([EN[k] for k in chunk], lang)):
                if tr and tr != EN[k]:
                    have[k] = tr
        table[lang] = {k: have[k] for k in keys if k in have}
        print(lang, len(table[lang]), "/", len(keys), flush=True)
    js = ("// The app's own labels in 6 languages (built by scripts/build_ui_i18n.py; edit freely).\n"
          f"const I18N = {json.dumps(table, ensure_ascii=False, indent=1)};\n"
          + (ROOT / "scripts" / "i18n_runtime.js").read_text(encoding="utf-8"))
    out_path.write_text(js, encoding="utf-8")
    print("wrote", out_path)


if __name__ == "__main__":
    main()
