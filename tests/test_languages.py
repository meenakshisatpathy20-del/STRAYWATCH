"""Every language works in the rules, not just in translation: what people type in their own script is understood."""
import json
from pathlib import Path

import pytest

from app import ai, engine, words
from app.db import Case
from test_engine import LALPUR, photo, s, talk, texts  # noqa: F401
from test_lifecycle import desc

HURT = {
    "hinglish": "kutta ghayal hai, khoon aa raha hai",
    "hi": "कुत्ता घायल है, खून बह रहा है",
    "mr": "कुत्रा जखमी आहे, रक्त येत आहे",
    "bn": "কুকুরটা আহত, রক্ত পড়ছে",
    "ta": "நாய் காயம் அடைந்துள்ளது, இரத்தம் வருகிறது",
    "te": "కుక్కకు గాయమైంది, రక్తం కారుతోంది",
}
ROAD = {"hi": "सड़क पर पड़ा है", "bn": "রাস্তায় পড়ে আছে", "ta": "சாலையில் கிடக்கிறது", "te": "రోడ్డు మీద పడి ఉంది",
        "mr": "रस्त्यावर पडला आहे", "hinglish": "sadak pe pada hai"}
DEAD = {"hi": "गाय मर गयी है", "bn": "গরুটা মারা গেছে", "ta": "மாடு இறந்து விட்டது", "te": "ఆవు చనిపోయింది",
        "hinglish": "gaay mar gayi"}
LOST = {"hi": "मेरी गाय खो गयी", "bn": "আমার গরু হারিয়ে গেছে", "ta": "என் மாடு காணவில்லை", "te": "నా ఆవు తప్పిపోయింది",
        "mr": "माझी गाय हरवली", "hinglish": "meri gaay kho gayi"}


@pytest.mark.parametrize("lang", HURT)
def test_hurt_is_understood_in_every_language(lang):
    assert words.says_hurt(HURT[lang])


@pytest.mark.parametrize("lang", ROAD)
def test_road_is_understood_in_every_language(lang):
    assert words.says_road(ROAD[lang])


@pytest.mark.parametrize("lang", DEAD)
def test_dead_is_understood(lang):
    assert words.says_dead(DEAD[lang])


@pytest.mark.parametrize("lang", LOST)
def test_lost_is_understood(lang):
    assert words.says_lost(LOST[lang])


def test_commands_in_every_script():
    for t in ("नमस्ते", "নমস্কার", "வணக்கம்", "నమస్కారం", "hi", "Menu!"):
        assert words.is_menu(t), t
    for t in ("भाषा", "ভাষা", "மொழி", "భాష"):
        assert words.is_language(t), t
    for t in ("स्वयंसेवक", "স্বেচ্ছাসেবক", "volunteer"):
        assert words.is_volunteer(t), t
    for t in ("हाँ", "হ্যাঁ", "ஆம்", "అవును", "होय", "ho gaya"):
        assert words.is_yes_done(t), t


def test_place_names_are_not_mistaken_for_lost():
    assert not words.says_lost("Gumla") and not words.says_hurt("Ratu Road, Ranchi")


@pytest.mark.parametrize("lang", ["hi", "bn", "ta", "te", "mr"])
def test_reporter_in_own_script_makes_the_case_urgent(s, monkeypatch, lang):
    monkeypatch.setattr(ai, "describe", desc(condition="healthy", summary="A dog."))
    monkeypatch.setattr(ai, "translate_batch", lambda t, l: list(t))
    talk(s, "web:r", button=f"lang:{lang}")
    talk(s, "web:r", button="m:sos")
    talk(s, "web:r", image=photo(), text=HURT[lang] + " " + ROAD[lang if lang in ROAD else "hi"])
    talk(s, "web:r", lat=LALPUR[0], lon=LALPUR[1])
    c = s.query(Case).one()
    assert c.level == "HIGH" and c.triage["condition"] == "injured" and c.triage["on_road"]


def test_buttons_are_not_cut_short_in_the_app(s, monkeypatch):
    long = "ஆபத்தில் உள்ள விலங்குக்கு உதவி வேண்டும்"
    monkeypatch.setattr(ai, "translate_batch", lambda t, l: [long for _ in t])
    out = engine.localise(s, [engine.Out("web:x", "Hi", buttons=[("m:sos", "Hurt animal: SOS")])])
    s.add(engine.User(id="web:x", lang="ta", state={}))
    out = engine.localise(s, [engine.Out("web:x", "Hi", buttons=[("m:sos", "Hurt animal: SOS")])])
    assert out[0].buttons[0][1] == long


def test_premade_translations_load_into_the_cache(s, tmp_path, monkeypatch):
    f = tmp_path / "translations.json"
    f.write_text(json.dumps({"languages": {"hi": {"Hurt animal: SOS": "घायल जानवर: SOS"}}}, ensure_ascii=False))
    monkeypatch.setattr(engine, "TRANSLATIONS_FILE", f)
    assert engine.seed_translations(s) == 1
    called = []
    monkeypatch.setattr(ai, "translate_batch", lambda t, l: called.append(t) or list(t))
    assert engine.translate_texts(s, ["Hurt animal: SOS"], "hi")["Hurt animal: SOS"] == "घायल जानवर: SOS"
    assert not called, "no AI call needed for a pre-made translation"
