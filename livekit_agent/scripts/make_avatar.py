"""Generates Jarvis's realistic HUD avatar (data/avatar/) with OpenAI image models.

    python scripts/make_avatar.py            # everything that's missing
    python scripts/make_avatar.py --redo     # start over with a new face

1. base.png      -- a photorealistic head-and-shoulders portrait, transparent
                    background, mouth closed. An original fictional person.
2. meta.json     -- where the mouth and eyes are (found by a vision model).
3. viseme_*.png  -- the same portrait with only the mouth/jaw area redrawn
   blink.png        (images.edit with a mask), then pasted back onto the base
                    through a feathered mask, so identity, light and framing
                    stay pixel-identical everywhere else. hud_face.py blends
                    between them by the audio that is playing.

Costs a handful of image generations on your OPENAI_API_KEY (about 7 calls).
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

OUT = config.DATA_DIR / "avatar"
MODEL = "gpt-image-2"
SIZE = "1024x1024"

BASE_PROMPT = (
    "Ultra-photorealistic head-and-shoulders portrait photograph of an original fictional man "
    "(not resembling any real actor or celebrity), late 30s, sharp intelligent features, short neatly "
    "styled dark hair, light stubble, calm confident gaze straight into the camera, relaxed neutral "
    "expression with lips gently closed, wearing a dark charcoal high-collar jacket. Soft frontal key "
    "light, cool cyan rim light on both sides of the face and shoulders like a hologram. Symmetrical, "
    "centered, face in the upper-middle of the frame, shoulders cut by the bottom edge. Real skin "
    "texture, 85mm lens, studio photo. Fully transparent background, no backdrop, no text."
)

VISEMES = {
    # name: prompt for the masked mouth/jaw region
    "open": "the same man speaking, mouth open as if saying 'ah', jaw dropped naturally, upper teeth and "
            "a bit of tongue visible, realistic lips and skin, same lighting",
    "half": "the same man speaking, mouth slightly open mid-word as if saying 'eh', a hint of upper teeth, "
            "realistic lips, same lighting",
    "round": "the same man speaking, lips rounded and pushed slightly forward as if saying 'oo', small round "
             "opening, realistic lips, same lighting",
    "wide": "the same man speaking, lips stretched wide with teeth close together and visible as if saying "
            "'ee' or 's', realistic lips, keep the exact same sharp stubble beard texture on the chin and jaw, crisp photographic detail, same lighting",
}
BLINK = "the same man with both eyes gently closed mid-blink, relaxed eyelids, realistic lashes, same lighting"


def client():
    from openai import OpenAI

    if not config.OPENAI_API_KEY:
        sys.exit("OPENAI_API_KEY is empty")
    return OpenAI(api_key=config.OPENAI_API_KEY, timeout=300)


def _decode(resp) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(resp.data[0].b64_json))).convert("RGBA")


def _png_bytes(img: Image.Image, name: str) -> tuple[str, bytes, str]:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return name, buf.getvalue(), "image/png"


def make_base(c) -> Image.Image:
    resp = c.images.generate(model=MODEL, prompt=BASE_PROMPT, size=SIZE, quality="high",
                             background="transparent", output_format="png", n=1)
    return _decode(resp)


def locate_features(c, base: Image.Image) -> dict:
    """Vision model -> pixel boxes of the mouth and each eye."""
    flat = Image.new("RGB", base.size, (40, 40, 40))
    flat.paste(base, mask=base.split()[3])
    buf = io.BytesIO()
    flat.save(buf, format="JPEG", quality=90)
    url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    w, h = base.size
    resp = c.responses.create(
        model=config.OPENAI_MODEL,
        input=[{"role": "user", "content": [
            {"type": "input_text", "text": (
                f"This portrait is {w}x{h} pixels. Return ONLY JSON with tight pixel bounding boxes "
                '[x0, y0, x1, y1] (top-left origin): {"mouth": [...], "left_eye": [...], "right_eye": [...]}. '
                "mouth = the lips only; eyes = each eye opening including lids, not brows.")},
            {"type": "input_image", "image_url": url, "detail": "high"},
        ]}],
    )
    text = resp.output_text
    data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    return {k: [int(round(v)) for v in data[k]] for k in ("mouth", "left_eye", "right_eye")}


def _region(box, pad_x, pad_top, pad_bottom, size) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    return (max(0, int(x0 - bw * pad_x)), max(0, int(y0 - bh * pad_top)),
            min(size[0], int(x1 + bw * pad_x)), min(size[1], int(y1 + bh * pad_bottom)))


def mouth_region(meta, size):
    # the jaw moves when the mouth opens: give the edit room below the lips
    return _region(meta["mouth"], 0.45, 0.6, 2.2, size)


def eyes_region(meta, size):
    lx0, ly0, lx1, ly1 = meta["left_eye"]
    rx0, ry0, rx1, ry1 = meta["right_eye"]
    return _region((min(lx0, rx0), min(ly0, ry0), max(lx1, rx1), max(ly1, ry1)), 0.12, 0.7, 0.6, size)


def _edit_mask(size, region) -> Image.Image:
    """OpenAI edit mask: fully transparent where the model may paint."""
    mask = Image.new("RGBA", size, (0, 0, 0, 255))
    ImageDraw.Draw(mask).ellipse(region, fill=(0, 0, 0, 0))
    return mask


def feather_alpha(size, region, blur: int) -> Image.Image:
    a = Image.new("L", size, 0)
    x0, y0, x1, y1 = region
    shrink = blur
    ImageDraw.Draw(a).ellipse((x0 + shrink, y0 + shrink, x1 - shrink, y1 - shrink), fill=255)
    return a.filter(ImageFilter.GaussianBlur(blur))


def edit_region(c, base: Image.Image, region, prompt: str) -> Image.Image:
    resp = c.images.edit(
        model=MODEL, image=_png_bytes(base, "base.png"), mask=_png_bytes(_edit_mask(base.size, region), "mask.png"),
        prompt=prompt + ". Change nothing outside the masked area.", size=SIZE, quality="high",
        background="transparent", output_format="png", n=1,
    )
    edited = _decode(resp)
    if edited.size != base.size:
        edited = edited.resize(base.size, Image.LANCZOS)
    # keep everything but the region pixel-identical to the base
    out = base.copy()
    out.paste(edited, (0, 0), feather_alpha(base.size, region, blur=max(6, (region[3] - region[1]) // 10)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--redo", action="store_true", help="delete the current avatar and make a new face")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.redo:
        for f in OUT.glob("*"):
            f.unlink()
    c = client()

    base_path = OUT / "base.png"
    if not base_path.exists():
        print("generating base portrait ...")
        make_base(c).save(base_path)
    base = Image.open(base_path).convert("RGBA")

    meta_path = OUT / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    else:
        print("locating mouth and eyes ...")
        meta = locate_features(c, base)
        meta["mouth_region"] = mouth_region(meta, base.size)
        meta["eyes_region"] = eyes_region(meta, base.size)
        meta["size"] = list(base.size)
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    for name, prompt in VISEMES.items():
        path = OUT / f"viseme_{name}.png"
        if not path.exists():
            print(f"viseme {name} ...")
            edit_region(c, base, meta["mouth_region"], prompt).save(path)
    blink_path = OUT / "blink.png"
    if not blink_path.exists():
        print("blink ...")
        edit_region(c, base, meta["eyes_region"], BLINK).save(blink_path)

    # contact sheet for a quick visual check
    frames = [base] + [Image.open(OUT / f"viseme_{n}.png") for n in VISEMES] + [Image.open(blink_path)]
    thumb = 320
    sheet = Image.new("RGB", (thumb * len(frames), thumb), (20, 24, 30))
    for i, f in enumerate(frames):
        t = f.copy()
        t.thumbnail((thumb, thumb))
        sheet.paste(t, (i * thumb, 0), t)
    sheet.save(OUT / "_preview.jpg", quality=88)
    print("done:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
