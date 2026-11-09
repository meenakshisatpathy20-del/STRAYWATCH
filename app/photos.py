"""Photo handling: shrink, strip metadata (incl. GPS in EXIF), store, and turn into data URLs for the model."""
from __future__ import annotations

import base64
import io

from PIL import Image, ImageOps
from sqlalchemy.orm import Session

from .db import Photo

MAX_SIDE = 896  # enough detail for the model, small enough to keep calls fast


def normalise(raw: bytes) -> bytes:
    """Rotate per EXIF, drop all metadata, resize, re-encode as JPEG. Raises ValueError if not an image."""
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as e:  # noqa: BLE001
        raise ValueError("not an image") from e
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=82, optimize=True)  # re-encoding drops EXIF, including GPS
    return out.getvalue()


def save(s: Session, raw: bytes) -> Photo:
    p = Photo(data=normalise(raw))
    s.add(p)
    s.flush()
    return p


def data_url(s: Session, photo_id: str | None, side: int | None = None) -> str | None:
    """Photo as a data: URL for the model. side shrinks it further (used for comparison candidates)."""
    if not photo_id:
        return None
    p = s.get(Photo, photo_id)
    if not p:
        return None
    data = p.data
    if side:
        img = Image.open(io.BytesIO(data))
        img.thumbnail((side, side))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=80)
        data = out.getvalue()
    return "data:image/jpeg;base64," + base64.b64encode(data).decode()


def contact_sheet(images: list[bytes], tile: int = 320) -> bytes:
    """Tile candidate photos into one labelled grid (A, B, C...). The model API accepts at most two images per
    request reliably, so comparisons send: [new photo, this sheet]."""
    from PIL import ImageDraw, ImageFont
    n = len(images)
    cols = 3 if n > 4 else 2 if n > 1 else 1
    rows = (n + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tile, rows * tile), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 46)
    except OSError:
        font = ImageFont.load_default(size=46)
    for i, raw in enumerate(images):
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((tile - 8, tile - 8))
        x, y = (i % cols) * tile, (i // cols) * tile
        sheet.paste(img, (x + (tile - img.width) // 2, y + (tile - img.height) // 2))
        label = chr(ord("A") + i)
        draw.rectangle([x + 4, y + 4, x + 62, y + 62], fill="#FFD23F", outline="black", width=3)
        draw.text((x + 14, y + 8), label, fill="black", font=font)
        draw.rectangle([x, y, x + tile - 1, y + tile - 1], outline="black", width=2)
    out = io.BytesIO()
    sheet.save(out, "JPEG", quality=85)
    return out.getvalue()


def raw(s: Session, photo_id: str | None) -> bytes | None:
    p = s.get(Photo, photo_id) if photo_id else None
    return p.data if p else None


def to_data_url(data: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(data).decode()
