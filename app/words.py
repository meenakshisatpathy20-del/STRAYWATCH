"""Words people really type, in English, Hinglish and the 5 Indian scripts StrayWatch supports.

Code uses these for decisions the AI must not be trusted with alone (the reporter says it's hurt, it's dead,
it's on the road) and for commands (menu, volunteer, pause). Matching is substring-based so word endings
(घायल / घायली, আহত / আহতো) still match. Keep entries short and specific.
"""
from __future__ import annotations

import re

_HURT = [
    # English + Hinglish
    "bleed", "blood", "khoon", "injur", "hurt", "ghayal", "chot", "zakhm", "wound", "fracture", "limp", "langd",
    "broken", "toot", "can't walk", "cant walk", "cannot walk", "chal nahi", "uth nahi", "not moving", "hil nahi",
    "hilta nahi", "behosh", "unconscious", "accident", "hit by", "takkar", "gaadi ne", "kuchal", "maar", "trapped",
    "stuck", "phans", "fasa", "dying", "sick", "bimar", "beemar", "not eating", "kha nahi", "ulti", "vomit", "maggot",
    "keede", "keeda", "fell down", "gir gay", "pada hai", "padi hai", "dead", "mar gay", "mara hua",
    # Hindi / Marathi (Devanagari)
    "घायल", "चोट", "खून", "ज़ख्म", "जख्म", "लंगड़", "टूट", "हिल नहीं", "उठ नहीं", "चल नहीं", "बेहोश", "एक्सीडेंट",
    "दुर्घटना", "टक्कर", "कुचल", "फंस", "फँस", "फसा", "बीमार", "खा नहीं", "उल्टी", "कीड़", "गिर", "पड़ा है", "पड़ी है",
    "मर गय", "मरा हुआ", "मृत", "जखमी", "रक्त", "अपघात", "आजारी", "अडकल", "मेलेल", "हलत नाही",
    # Bengali
    "আহত", "রক্ত", "ক্ষত", "খোঁড়া", "ভাঙা", "নড়ছে না", "অজ্ঞান", "দুর্ঘটনা", "ধাক্কা", "আটকে", "মারা গেছে", "মৃত",
    "অসুস্থ", "খাচ্ছে না", "বমি", "পোকা", "পড়ে আছে",
    # Tamil
    "காயம்", "இரத்தம்", "ரத்தம்", "அடிபட்ட", "நொண்டி", "உடைந்த", "அசையவில்லை", "மயக்க", "விபத்து", "மோதி", "சிக்கி",
    "இறந்த", "செத்த", "நோய்", "சாப்பிடவில்லை", "வாந்தி", "புழு",
    # Telugu
    "గాయ", "రక్తం", "దెబ్బ", "కుంటు", "విరిగ", "కదలడం లేదు", "స్పృహ", "ప్రమాదం", "ఢీ", "చిక్కుక", "చనిపోయ",
    "అనారోగ్య", "తినడం లేదు", "వాంతి", "పురుగు",
]
_DEAD = ["dead", "mar gay", "mara hua", "died", "मर गय", "मरा हुआ", "मृत", "मेलेल", "মারা গেছে", "মৃত", "இறந்த", "செத்த",
         "చనిపోయ"]
_TRAPPED = ["trapped", "stuck", "phans", "fasa", "फंस", "फँस", "फसा", "अडकल", "আটকে", "சிக்கி", "చిక్కుక"]
_ROAD = ["road", "sadak", "highway", "accident", "hit by", "takkar", "gaadi", "vehicle", "truck", "bike", "car ",
         "सड़क", "सडक", "रोड", "गाड़ी", "गाडी", "ट्रक", "हाईवे", "रस्त", "অ্যাক্সিডেন্ট", "রাস্তা", "গাড়ি", "সড়ক",
         "சாலை", "ரோடு", "வண்டி", "லாரி", "రోడ్డు", "రహదారి", "వాహన", "లారీ"]
_MENU = ["hi", "hii", "hello", "hey", "namaste", "namaskar", "start", "menu", "cancel", "stop", "नमस्ते", "नमस्कार",
         "हैलो", "हेलो", "मेनू", "शुरू", "নমস্কার", "হ্যালো", "মেনু", "வணக்கம்", "மெனு", "నమస్కారం", "నమస్తే", "మెను"]
_LANGUAGE = ["language", "bhasha", "भाषा", "ভাষা", "மொழி", "భాష"]
_VOLUNTEER = ["volunteer", "helper", "join", "स्वयंसेवक", "वॉलंटियर", "वालंटियर", "স্বেচ্ছাসেবক", "தன்னார்வ", "వాలంటీర్",
              "స్వచ్ఛంద"]
_CASES = ["cases", "open cases", "nearby cases", "cases near me", "केस", "केस दिखाओ", "पास के केस", "কেস", "வழக்குகள்",
          "కేసులు", "केसेस"]
_STOP_CHAT = ["stop", "end", "bye", "बंद", "खत्म", "বন্ধ", "நிறுத்து", "ఆపు", "थांबा"]
_PAUSE = ["pause", "रोको", "रुको", "बंद करो", "থামাও", "বিরতি", "நிறுத்து", "ఆపు"]
_RESUME = ["resume", "फिर शुरू", "चालू करो", "আবার চালু", "மீண்டும்", "మళ్ళీ"]
_YES_DONE = ["vaccinated", "done", "yes", "haan", "ho gaya", "हाँ", "हां", "हो गया", "लग गया", "होय", "झाले", "হ্যাঁ",
             "হয়েছে", "ஆம்", "ஆமாம்", "అవును", "అయింది"]
_VET = ["vet", "doctor", "daktar", "doctor sahab", "डॉक्टर", "डाक्टर", "पशु चिकित्सक", "पशुवैद्य", "ডাক্তার", "পশু চিকিৎসক",
        "மருத்துவர்", "டாக்டர்", "కాల్నడ", "పశువైద్య", "డాక్టర్"]
_LOST = ["lost", "missing", "kho gay", "kho gai", "gum ho", "nahi mil", "खो गय", "गुम", "नहीं मिल", "हरवल", "হারিয়ে",
         "নিখোঁজ", "காணவில்லை", "தொலைந்த", "తప్పిపోయ", "కనిపించడం లేదు"]


def _any(words: list[str], text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in words)


def says_hurt(text: str) -> bool:
    return _any(_HURT, text)


def says_dead(text: str) -> bool:
    return _any(_DEAD, text)


def says_trapped(text: str) -> bool:
    return _any(_TRAPPED, text)


def says_road(text: str) -> bool:
    return _any(_ROAD, (text or "") + " ")


def wants_vet(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in _VET[1:]) or bool(re.search(r"\bvets?\b", t))


def says_lost(text: str) -> bool:
    return _any(_LOST, text)


def _exact(words: list[str], text: str) -> bool:
    t = re.sub(r"[\s.!।?]+$", "", (text or "").strip().lower())
    return t in words


def is_menu(text: str) -> bool:
    return _exact(_MENU, text)


def is_language(text: str) -> bool:
    return _exact(_LANGUAGE, text)


def is_volunteer(text: str) -> bool:
    return _exact(_VOLUNTEER, text)


def wants_cases(text: str) -> bool:
    return _exact(_CASES, text)


def is_stop_chat(text: str) -> bool:
    return _exact(_STOP_CHAT, text)


def is_pause(text: str) -> bool:
    return _exact(_PAUSE, text)


def is_resume(text: str) -> bool:
    return _exact(_RESUME, text)


def is_yes_done(text: str) -> bool:
    return _exact(_YES_DONE, text)
