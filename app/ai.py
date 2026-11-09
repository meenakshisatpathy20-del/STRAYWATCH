"""Everything StrayWatch asks the BharatCode (Adaption) model API to do.

Design rule: the AI *reads and compares*; plain code *decides and enforces*.
Every call returns validated data, is timed and written to the trace, and has a safe fallback.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any

from . import config
from .db import Trace, TranslationCache

log = logging.getLogger("straywatch.ai")

SPECIES = {"dog", "cow", "buffalo", "cat", "goat", "pig", "horse", "donkey", "monkey", "bird", "other"}
CONDITIONS = {"healthy", "injured", "sick", "trapped", "dead", "unknown"}
URGENCY = {"high", "medium", "low", "none"}

_client = None
_SLOTS = __import__("threading").BoundedSemaphore(3)


def client():
    global _client
    if _client is None:
        from adaption import Adaption  # reads ADAPTION_API_KEY
        _client = Adaption(timeout=config.AI_TIMEOUT, max_retries=1)
    return _client


class AIError(RuntimeError):
    pass


def _trace(s, user_id: str | None, step: str, detail: str, model: str | None = None, ms: int | None = None):
    if s is not None and user_id:
        s.add(Trace(user_id=user_id, step=step, detail=detail[:2000], model=model, ms=ms))


def call(s, user_id, step: str, model: str, instructions: str, text: str, images: list[str] | None = None,
         max_tokens: int = 700, reasoning: str = "none") -> str:
    """One Responses API call. images are data: URLs. Raises AIError on failure.

    reasoning="none" keeps simple reading jobs fast (~3 s); comparisons use "low" and get a bigger budget,
    because a reasoning model spends tokens thinking before it answers."""
    if reasoning != "none":
        max_tokens += 2500
    content: list[dict[str, Any]] = [{"type": "input_text", "text": text}]
    for img in images or []:
        content.append({"type": "input_image", "image_url": img})
    t0 = time.time()
    last_err: Exception | None = None
    out = None
    for attempt in range(3):
        try:
            with _SLOTS:  # cap parallel calls so a burst doesn't trip the rate limit
                r = client().responses.create(
                    model=model, instructions=instructions, max_output_tokens=max_tokens, temperature=0.1,
                    input=[{"type": "message", "role": "user", "content": content}],
                    extra_body={"reasoning": {"effort": reasoning}},
                )
            out = (r.output_text or "").strip()
            if r.status == "incomplete" and not out:
                raise AIError("reply cut off before any answer")
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            msg = str(e)
            if ("429" in msg or "rate limit" in msg.lower()) and attempt < 2:
                time.sleep(8 * (attempt + 1))  # back off, then retry
                continue
            break
    if out is None:
        ms = int((time.time() - t0) * 1000)
        _trace(s, user_id, step + " (failed)", f"{type(last_err).__name__}: {last_err}", model, ms)
        raise AIError(str(last_err)) from last_err
    ms = int((time.time() - t0) * 1000)
    _trace(s, user_id, step, out, model, ms)
    return out


def parse_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply (handles ```json fences and chatter)."""
    t = re.sub(r"```(?:json)?", "", text)
    start = t.find("{")
    if start < 0:
        raise AIError("no JSON in reply")
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except json.JSONDecodeError as e:
                    raise AIError(f"bad JSON: {e}") from e
    raise AIError("unterminated JSON")


def _enum(v, allowed, default):
    v = str(v or "").strip().lower()
    return v if v in allowed else default


def _bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"true", "yes", "1"}


def _float01(v, default=0.5) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- looking at an animal

FIRST_AID_IDS = ["road_injury", "bleeding", "trapped", "aggressive_or_scared", "heat_stress",
                 "cattle_on_road", "sick", "dead_animal", "puppies_or_kittens", "general"]

DESCRIBE_INSTRUCTIONS = """You are the eyes of StrayWatch, an Indian community-animal care service.
Look at the photo (and the person's message, which may be Hindi, Hinglish or another Indian language) and describe
the animal for identification and triage. Be factual. Only describe what is visible. Never invent injuries.
Reply with ONE JSON object and nothing else:
{
 "is_animal": true/false,               // false if there is no real animal in the photo
 "species": "dog|cow|buffalo|cat|goat|pig|horse|donkey|monkey|bird|other",
 "count": 1,                             // animals clearly visible
 "colour": "main coat colours",
 "markings": "distinctive marks: patches, spots, scars, tail, horn shape",
 "ear_notch": true/false,               // a V-notch/clipped ear tip = sterilised street dog in India
 "collar": "none or describe",
 "ear_tag": true/false,                 // yellow cattle ear tag visible
 "sex_guess": "male|female|unknown",
 "age_guess": "puppy/calf|young|adult|old|unknown",
 "condition": "healthy|injured|sick|trapped|dead|unknown",
 "injuries": "visible wounds/limping/bleeding, or 'none visible'",
 "on_road": true/false,                 // animal on or at the edge of a road with traffic
 "urgency": "high|medium|low|none",    // high = bleeding, can't move, hit by vehicle, trapped, on a busy road
 "first_aid_card": "road_injury|bleeding|trapped|aggressive_or_scared|heat_stress|cattle_on_road|sick|dead_animal|puppies_or_kittens|general",
 "summary": "one short plain-English sentence a rescuer can act on",
 "confidence": 0.0-1.0                  // how sure you are of the species and condition
}"""


def describe(s, user_id, image: str | None, message: str = "") -> dict:
    """Describe an animal from a photo and/or text. Always returns a valid dict."""
    text = "Person's message: " + (message.strip() or "(none)")
    if not image:
        text += "\nNo photo was sent (it may be dark). Use the message only; set confidence lower."
    try:
        raw = call(s, user_id, "Looked at the animal", config.VISION_MODEL, DESCRIBE_INSTRUCTIONS, text,
                   [image] if image else None)
        d = parse_json(raw)
    except AIError:
        d = {}
        _trace(s, user_id, "Fallback used", "AI reply unusable; using safe defaults (treated as urgent if reported as SOS).")
    out = {
        "is_animal": _bool(d.get("is_animal", True)),
        "species": _enum(d.get("species"), SPECIES, "other"),
        "count": int(d.get("count") or 1) if str(d.get("count", "1")).isdigit() else 1,
        "colour": str(d.get("colour") or "")[:80],
        "markings": str(d.get("markings") or "")[:160],
        "ear_notch": _bool(d.get("ear_notch", False)),
        "collar": str(d.get("collar") or "none")[:60],
        "ear_tag": _bool(d.get("ear_tag", False)),
        "sex_guess": _enum(d.get("sex_guess"), {"male", "female", "unknown"}, "unknown"),
        "age_guess": str(d.get("age_guess") or "unknown")[:20],
        "condition": _enum(d.get("condition"), CONDITIONS, "unknown"),
        "injuries": str(d.get("injuries") or "none visible")[:160],
        "on_road": _bool(d.get("on_road", False)),
        "urgency": _enum(d.get("urgency"), URGENCY, "medium"),
        "first_aid_card": _enum(d.get("first_aid_card"), set(FIRST_AID_IDS), "general"),
        "summary": str(d.get("summary") or "Animal reported, details unclear.")[:220],
        "confidence": _float01(d.get("confidence"), 0.5 if d else 0.0),
        "ai_ok": bool(d),
    }
    # Code-side safety rules (the model can't talk us out of these):
    if out["condition"] in {"injured", "trapped"} and out["urgency"] in {"low", "none"}:
        out["urgency"] = "medium"
    if out["condition"] == "dead":
        out["first_aid_card"] = "dead_animal"
    return out


# ---------------------------------------------------------------- is this the same animal?

MATCH_INSTRUCTIONS = """You help StrayWatch recognise individual street animals in India.
Image 1 is a NEW photo. Image 2 is a grid of candidate animals, each tile labelled with a yellow letter (A, B, C...).
Decide whether the new photo shows the SAME individual animal as one candidate. Compare coat colours and pattern,
patches, ear shape and notches, tail, horns, collar, scars, body size. Judge the ANIMAL ONLY: ignore the background,
place, vehicles and people, because the same animal is photographed in different places. Lighting, angle and crop will
differ, and a photo may be mirrored.
IMPORTANT: Indian street dogs very often look alike. "Tan/brown coat with white muzzle, chest or paws and a curled tail"
is the MOST COMMON look and is NOT distinctive on its own; the same goes for a plain white cow. Only call it a match
(confidence 0.95 or more) if a DISTINCTIVE feature also agrees: a unique patch shape or position, scar, ear notch or
torn ear, collar, ear tag, horn shape, unusual tail or markings. If only common features agree, use confidence 0.7 or
less. If unsure, say none. Reply with ONE JSON object only:
{"match": "A|B|C|...|none", "confidence": 0.0-1.0, "reason": "the features that agree or differ, one sentence"}"""


def match(s, user_id, new_image: bytes, candidates: list[tuple[Any, bytes, str]], purpose: str,
          step: str = "Compared with known animals") -> dict:
    """new_image and candidate images are raw JPEG bytes. candidates: [(key, image_bytes, short description)].
    Returns {key|None, confidence, reason}. Sends exactly two images: the new photo and a labelled contact sheet."""
    from . import photos
    cands = [c for c in candidates if c[1]][:9]
    if not cands or not new_image:
        return {"key": None, "confidence": 0.0, "reason": "no candidates nearby"}
    labels = [chr(ord("A") + i) for i in range(len(cands))]
    sheet = photos.contact_sheet([img for _, img, _ in cands])
    text = "Purpose: " + purpose + "\nCandidates in the grid:\n" + "\n".join(
        f"{l}: {desc}" for l, (_, _, desc) in zip(labels, cands))
    try:
        raw = call(s, user_id, step, config.VISION_MODEL, MATCH_INSTRUCTIONS, text,
                   [photos.to_data_url(new_image), photos.to_data_url(sheet)], max_tokens=300)
        d = parse_json(raw)
    except AIError:
        return {"key": None, "confidence": 0.0, "reason": "comparison failed"}
    pick = str(d.get("match", "none")).strip().upper()[:1]
    conf = _float01(d.get("confidence"), 0.0)
    key = None
    if pick in labels:
        key = cands[labels.index(pick)][0]
    return {"key": key, "confidence": conf, "reason": str(d.get("reason") or "")[:240]}


HEALTH_INSTRUCTIONS = """Two photos of the SAME street animal: first is older, second is today.
Note any visible health change a caretaker should check (new wound, limping posture, weight loss, eye/skin problem).
Do not diagnose. If nothing clearly changed, say so. Reply with ONE JSON object only:
{"changed": true/false, "note": "one short, kind sentence for the caretaker", "see_vet": true/false}"""


def compare_health(s, user_id, old_image: str | None, new_image: str) -> dict:
    if not old_image:
        return {"changed": False, "note": "", "see_vet": False}
    try:
        d = parse_json(call(s, user_id, "Compared health with last photo", config.FAST_MODEL,
                            HEALTH_INSTRUCTIONS, "Compare the two photos.", [old_image, new_image], max_tokens=200))
    except AIError:
        return {"changed": False, "note": "", "see_vet": False}
    return {"changed": _bool(d.get("changed")), "note": str(d.get("note") or "")[:240],
            "see_vet": _bool(d.get("see_vet"))}


# ---------------------------------------------------------------- understanding free text

INTENT_INSTRUCTIONS = """Classify a message sent to StrayWatch (Indian street-animal care bot). Messages may be in
English, Hindi, Hinglish, Bengali, Tamil, Telugu or Marathi. Reply with ONE JSON object only:
{"intent": "sos|register|checkin|lost|seen|menu|language|help|thanks|other",
 "name": "animal name if the person gave one, else null"}
sos = an animal is hurt/sick/trapped/dead/in danger or blocking traffic now. lost = their own animal is missing.
seen = they saw an animal that someone may have lost. register = add an animal they care for.
checkin = daily update on an animal they already care for. menu = hi/hello/start/options."""

_KEYWORDS = [
    ("sos", r"\b(sos|help|injur|hurt|bleed|accident|ghayal|khoon|chot|emergency|trapped|stuck|dying|maar|takkar)"),
    ("lost", r"\b(lost|missing|kho|gum|nahi mil|kho gay)"),
    ("register", r"\b(register|add (a |my )?(dog|cow|cat|animal))"),
    ("menu", r"^\s*(hi|hello|hey|hii+|namaste|namaskar|start|menu|options)\s*[.!]*\s*$"),
    ("language", r"\b(language|bhasha|bhaasha)\b"),
]


def intent(s, user_id, text: str) -> dict:
    low = text.lower()
    for name, pat in _KEYWORDS:
        if re.search(pat, low):
            return {"intent": name, "name": None, "via": "keyword"}
    try:
        d = parse_json(call(s, user_id, "Understood the message", config.FAST_MODEL, INTENT_INSTRUCTIONS, text,
                            max_tokens=80))
        return {"intent": _enum(d.get("intent"), {"sos", "register", "checkin", "lost", "seen", "menu",
                                                  "language", "help", "thanks", "other"}, "other"),
                "name": d.get("name"), "via": "ai"}
    except AIError:
        return {"intent": "other", "name": None, "via": "fallback"}


# ---------------------------------------------------------------- languages

LANGS = {
    "en": ("English", "English"),
    "hi": ("Hindi, written in Devanagari script", "हिंदी"),
    "bn": ("Bengali, written in Bengali script", "বাংলা"),
    "ta": ("Tamil, written in Tamil script", "தமிழ்"),
    "te": ("Telugu, written in Telugu script", "తెలుగు"),
    "mr": ("Marathi, written in Devanagari script", "मराठी"),
}

TRANSLATE_INSTRUCTIONS = """Translate the message for a WhatsApp chat in India into {lang}.
Use simple, warm, everyday words that an older person would understand (not formal or bookish).
Never assume the reader's gender: prefer gender-neutral phrasing.
Keep names, numbers, links, case codes like SW-12 or LF-3 and *bold markers* exactly as they are.
Keep line breaks. Reply with the translation only."""


def translate(s, text: str, lang: str | None, user_id: str | None = None) -> str:
    """Translate bot text into the user's language, cached. Falls back to English on any failure."""
    if not text or not lang or lang == "en" or lang not in LANGS:
        return text
    key = hashlib.sha256(f"{lang}|{text}".encode()).hexdigest()[:64]
    if s is not None:
        hit = s.get(TranslationCache, key)
        if hit:
            return hit.text
    try:
        out = call(None, None, "Translated", config.TEXT_MODEL,
                   TRANSLATE_INSTRUCTIONS.format(lang=LANGS[lang][0]), text, max_tokens=900)
    except AIError:
        return text
    out = out.strip().strip('"')
    if not out:
        return text
    if s is not None:
        s.merge(TranslationCache(key=key, text=out))
    return out


BATCH_INSTRUCTIONS = """Translate each string in the JSON list into {lang} for a WhatsApp chat in India.
Use simple, warm, everyday words an older person would understand. Never assume the reader's gender: prefer
gender-neutral phrasing (e.g. Hindi "रास्ते में हूँ" rather than "जा रहा हूँ"). Keep names, numbers, links, codes like SW-12 or
LF-1234, emojis and *bold markers* exactly as they are. Keep line breaks. Short button labels must stay short
(under 20 characters). Reply with ONLY a JSON object: {{"t": ["translation 1", "translation 2", ...]}} with the same
number of items in the same order."""


def translate_batch(texts: list[str], lang: str) -> list[str]:
    """Translate many strings in ONE call (the API is rate limited). Falls back to English per item."""
    if not texts or lang == "en" or lang not in LANGS:
        return list(texts)
    try:
        raw = call(None, None, "Translated", config.TEXT_MODEL, BATCH_INSTRUCTIONS.format(lang=LANGS[lang][0]),
                   json.dumps(texts, ensure_ascii=False), max_tokens=400 + 120 * len(texts))
        out = parse_json(raw).get("t")
        if isinstance(out, list) and len(out) == len(texts):
            return [str(o).strip() or t for o, t in zip(out, texts)]
        log.warning("batch translation returned %s items for %s", len(out or []), len(texts))
    except AIError as e:
        log.warning("batch translation failed (%s); translating one by one", e)
    # Fallback: one call per string, so a caretaker never gets the wrong language because one item broke the batch.
    return [translate(None, t, lang) for t in texts]
