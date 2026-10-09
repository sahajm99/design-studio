"""A deterministic fake photo provider: used in demo mode and in every test."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from studio.photos.base import DEMO_NO_IMAGE_CALLS, KeyCheck, PhotoResult
from studio.photos.catalogue import ImageModel

logger = logging.getLogger(__name__)

_HEX_COLOUR_RE = re.compile(r"#([0-9A-Fa-f]{6})")
# v6 Part B: the stand-in takes product photos for every model it stands in for, so demo mode
# shows the whole path; it draws the first one as an inset this wide, in the lower right.
MAX_INPUT_IMAGES = 3
_INSET_WIDTH = 0.26
_INSET_MARGIN = 0.04
_INSET_BORDER = 4


class FakePhotoProvider:
    """Draws a placeholder photo instead of calling a real image model.

    The same prompt and seed always draw the same photo, so demo-mode runs
    and tests are reproducible without a network call. A different seed
    draws a different photo for the same prompt.

    v6: in demo mode it stands in for every model in the catalogue. Its photos say which model
    was asked for, cost nothing and carry the `fake` provider id, and its key check calls no one.

    v6 Part B: it takes up to three product photos, whatever model it stands in for, and draws
    a small inset of the first one in the lower right, so the product photo's path is visible.
    """

    name = "fake"

    async def generate(
        self,
        prompt: str,
        width: int,
        height: int,
        out_path: Path,
        *,
        model: ImageModel | None = None,
        options: dict[str, str] | None = None,
        seed: int | None = None,
        images: Sequence[Path] = (),
    ) -> PhotoResult:
        image = _draw(prompt, width, height, seed)
        sent = list(images)[:MAX_INPUT_IMAGES]
        if sent:
            _draw_inset(image, sent[0])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(out_path, format="PNG")
        return PhotoResult(
            path=str(out_path),
            provider=self.name,
            model_id=model.id if model is not None else "",
            prompt=prompt,
            cost_usd=0.0,
            cost_basis="free_allowance",
            input_images=len(sent),
        )

    async def test_key(self) -> KeyCheck:
        return KeyCheck(ok=True, message=DEMO_NO_IMAGE_CALLS)


def _draw(prompt: str, width: int, height: int, seed: int | None = None) -> Image.Image:
    """The backdrop colour named in the prompt (edges and corners match it exactly, so the
    photo melts into a post of that colour), or, failing that, a dark vertical gradient.
    Either way, one soft, blurred ellipse sits near the centre, tinted from a hash of the
    prompt (and the seed, when one is given, so repeated calls for one prompt can still
    differ), in a tone that contrasts gently with the backdrop.
    """
    digest_source = f"{prompt}|{seed}" if seed is not None else prompt
    digest = hashlib.sha256(digest_source.encode("utf-8")).digest()
    backdrop = _backdrop_colour(prompt)

    if backdrop is None:
        dark = tuple(digest[i] % 30 for i in range(3))
        light = tuple(90 + digest[3 + i] % 120 for i in range(3))
        image = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(image)
        last_row = max(height - 1, 1)
        for y in range(height):
            fraction = y / last_row
            row_colour = tuple(round(dark[c] + (light[c] - dark[c]) * 0.3 * fraction) for c in range(3))
            draw.line([(0, y), (width, y)], fill=row_colour)
        glow_colour = light
    else:
        # A flat fill, not a gradient: every edge and corner must match the backdrop colour.
        image = Image.new("RGB", (width, height), backdrop)
        glow_colour = _contrast_tone(backdrop, digest)

    mask = Image.new("L", (width, height), 0)
    mask_draw = ImageDraw.Draw(mask)
    ellipse_width, ellipse_height = width * 0.5, height * 0.3
    centre_x, centre_y = width / 2, height / 2
    mask_draw.ellipse(
        (
            centre_x - ellipse_width / 2,
            centre_y - ellipse_height / 2,
            centre_x + ellipse_width / 2,
            centre_y + ellipse_height / 2,
        ),
        fill=200,
    )
    mask = mask.filter(ImageFilter.GaussianBlur(radius=max(width, height) * 0.05))

    glow = Image.new("RGB", (width, height), glow_colour)
    return Image.composite(glow, image, mask)


def _draw_inset(image: Image.Image, product: Path) -> None:
    """Paste the product photo, scaled to about a quarter of the width and framed in white, in
    the lower right of `image`. A product photo that cannot be read is left out quietly: the
    stand-in's photo is still made."""
    try:
        with Image.open(product) as opened:
            picture = opened.convert("RGB")
    except Exception as error:
        logger.warning("The stand-in could not read a product photo: %s", type(error).__name__)
        return
    width, height = image.size
    inner = max(8, round(width * _INSET_WIDTH))
    thumb = ImageOps.contain(picture, (inner, inner))
    framed = ImageOps.expand(thumb, border=_INSET_BORDER, fill=(255, 255, 255))
    margin = round(width * _INSET_MARGIN)
    x = max(0, width - framed.width - margin)
    y = max(0, height - framed.height - margin)
    image.paste(framed, (x, y))


def _backdrop_colour(prompt: str) -> tuple[int, int, int] | None:
    """The last `#RRGGBB` named in the prompt, or None when it names no colour."""
    matches = _HEX_COLOUR_RE.findall(prompt)
    if not matches:
        return None
    last = matches[-1]
    return tuple(int(last[i : i + 2], 16) for i in (0, 2, 4))


def _contrast_tone(backdrop: tuple[int, int, int], digest: bytes) -> tuple[int, int, int]:
    """A tone near `backdrop` but shifted lighter (on a dark backdrop) or darker (on a light
    one), so the centre ellipse reads against it. The shift's size is tinted from the
    prompt's hash so different prompts still draw different photos.
    """
    luma = 0.299 * backdrop[0] + 0.587 * backdrop[1] + 0.114 * backdrop[2]
    lighten = luma < 128
    tone = []
    for channel in range(3):
        shift = 60 + digest[3 + channel] % 60
        value = backdrop[channel] + shift if lighten else backdrop[channel] - shift
        tone.append(min(255, max(0, value)))
    return tuple(tone)
