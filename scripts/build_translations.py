"""Build data/translations.json: every FIXED message StrayWatch sends, translated into the 5 Indian languages.

Why: menus, prompts and first-aid cards must work in every language even if the AI is slow or down, and a
native speaker can review this one file. Messages with names or numbers in them are still translated live.

Run (needs ADAPTION_API_KEY):  python scripts/build_translations.py
"""
from __future__ import annotations

import ast
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import ai, engine  # noqa: E402

LANGS = ["hi", "bn", "ta", "te", "mr"]


def literal(node) -> str | None:
    """A plain string, or plain strings joined with +; None if it has variables in it."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = literal(node.left), literal(node.right)
        return a + b if a is not None and b is not None else None
    if isinstance(node, ast.JoinedStr) and all(isinstance(v, ast.Constant) for v in node.values):
        return "".join(v.value for v in node.values)
    return None


def fixed_strings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("Out", "_out"):
            if len(node.args) > 1 and (t := literal(node.args[1])):
                out.append(t)
            for kw in node.keywords:
                if kw.arg in ("buttons", "options") and isinstance(kw.value, ast.List):
                    for el in kw.value.elts:
                        if isinstance(el, ast.Tuple) and len(el.elts) == 2 and (t := literal(el.elts[1])):
                            out.append(t)
    return out


def main():
    texts = fixed_strings(ROOT / "app" / "engine.py")
    for extra in ("vet.py",):
        if (ROOT / "app" / extra).exists():
            texts += fixed_strings(ROOT / "app" / extra)
    texts += [t for _, t in engine.MENU_BUTTONS] + [engine.LOCATION_ASK] + list(engine.LEVEL_WORDS.values())
    for card in engine.FIRST_AID["cards"]:
        texts += [engine.first_aid_text(card, True), engine.first_aid_text(card, False)]
    from app import vet
    texts += [vet.card_text(c) for c in vet.CARDS["cards"]] + [vet.urgency_text(u) for u in vet.CARDS["urgency_words"]]
    texts += [vet.guide_text(sp) for sp in vet.FOOD["guides"]]
    texts += [vet.food_text(f, v) for f in vet.FOOD["foods"] for v in set(f["verdict"].values())]
    texts = [t for t in dict.fromkeys(texts) if t.strip() and any(c.isalpha() for c in t)]
    print(f"{len(texts)} fixed strings")
    old = json.loads((ROOT / "data" / "translations.json").read_text()) if (ROOT / "data" / "translations.json").exists() else {}
    result = {"_note": "Fixed StrayWatch messages, machine-translated with the BharatCode model API "
                       f"({ai.config.TEXT_MODEL}) on {date.today()}. Native speakers: please review and edit freely; "
                       "edits here win over live translations.", "languages": {}}
    for lang in LANGS:
        have = dict(old.get("languages", {}).get(lang, {}))
        todo = [t for t in texts if t not in have]
        for i in range(0, len(todo), 10):
            chunk = todo[i:i + 10]
            for src, tr in zip(chunk, ai.translate_batch(chunk, lang)):
                if tr and tr != src:
                    have[src] = tr
            print(f"  {lang}: {min(i + 10, len(todo))}/{len(todo)}", flush=True)
        result["languages"][lang] = {t: have[t] for t in texts if t in have}
        missing = [t for t in texts if t not in have]
        if missing:
            print(f"  {lang}: {len(missing)} not translated (left in English)")
        for other in LANGS:  # keep languages not rebuilt yet
            if other not in result["languages"] and other in old.get("languages", {}):
                result["languages"][other] = old["languages"][other]
        (ROOT / "data" / "translations.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  saved {lang}", flush=True)
    print("wrote data/translations.json")


if __name__ == "__main__":
    main()
