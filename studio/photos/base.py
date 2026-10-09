"""The interface every photo source implements, and the plain messages its failures carry.

v6: a provider is one adapter, and each of its models is one catalogue entry, so `generate`
takes the model and its options. Every result says which model made the photo and what it
cost, and `test_key` checks a key for free, without making a photo.

v6 Part B: `generate` also takes the product photos the model is to keep (`images`, prepared
PNG files), and the result says how many it was sent.
"""

from __future__ import annotations

import base64
import binascii
import io
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from PIL import Image
from pydantic import BaseModel, Field

from studio.photos.catalogue import ImageModel, PriceBasis, ProviderId

# Each provider's name in a person-facing line.
PROVIDER_LABELS: dict[str, str] = {
    "cloudflare": "Cloudflare",
    "openai": "OpenAI",
    "google": "Google",
    "fake": "The stand-in",
}

# What a failed photo's card says, by what the provider answered (spec v6, section 5).
KEY_REFUSED = "The {provider} key was refused. Check it in Settings."
OPENAI_VERIFY = (
    "OpenAI asks your organisation to verify itself before this model can be used. "
    "Settings in your OpenAI account, Organization, Verify."
)
GOOGLE_BILLING = (
    "This model needs billing on your Google project. Google's image models have no free tier."
)
OPENAI_CREDIT = "Your OpenAI credit is used up."
PROVIDER_BUSY = "{provider} is busy. Try again in a minute."
DECLINED = "The model declined this prompt."
MODEL_NOT_ALLOWED = "This key cannot use {label}. Untick it in Settings, or use another key."
PROVIDER_PROBLEM = "{provider} had a problem making this photo."
NO_PHOTO_IN_ANSWER = "{provider} answered without a photo."
# The same kinds of failure the table does not name.
NO_ANSWER_IN_TIME = "{provider} did not answer within {seconds} seconds."
UNREACHABLE = "{provider} could not be reached."
KEY_NOT_SET = "The {provider} key is not set. Add it in Settings or in .env."
KEY_CHECK_FAILED = "{provider} could not check the key ({status})."
# What a free key check says when it worked.
KEY_WORKS = "The key works. {count} image models are visible."
KEY_WORKS_ONE = "The key works. 1 image model is visible."
DEMO_KEY_CHECK = "Demo mode: no image model is called, and keys are not used."

# v6 Part B: what the designer reads when the product photos did not all reach the model
# (spec v6, B7), and the sentence code puts before the prompt by the session's fidelity (B3).
PRODUCT_DECLINED = "The model declined the product photo."
PRODUCT_UNREADABLE = "Product photo {number} could not be read and was left out."
WORDS_ONLY = "This model cannot see product photos; the photo was made from the words alone."
TOO_MANY_PRODUCTS = "{label} takes {photos}; the first {count} were sent."
TOO_MANY_PRODUCTS_ONE = "{label} takes 1 product photo; the first was sent."
FIDELITY_EXACT = (
    "Use the product in the reference image exactly as it is: its shape, its tooth shade, its "
    "gum colour and every titanium sleeve. Change only its setting, its light and its framing."
)
FIDELITY_GUIDE = (
    "Use the product in the reference image as the subject. Keep its design and its colours, "
    "and turn it to suit the scene."
)


class PhotoUnavailable(Exception):
    """No photo could be produced. The message is shown to the designer as it is,
    so it must be plain language and must never contain a key or token."""


class LimitReached(PhotoUnavailable):
    """A daily limit stops a paid photo before it is asked for; the message names the limit."""


class PhotoResult(BaseModel):
    path: str  # the file that was written
    provider: str  # for the run page: "openai:gpt-image-2.5-flare", "cloudflare:flux-2-klein-4b"
    model_id: str  # the catalogue model that made it; "" from a source without one
    prompt: str
    cost_usd: float | None  # None when the cost is not known
    cost_basis: PriceBasis
    usage: dict[str, int] = Field(default_factory=dict)  # the provider's own counts, if any
    input_images: int = 0  # v6 Part B: how many product photos the model was sent


class KeyCheck(BaseModel):
    """What a free key check found."""

    ok: bool
    message: str  # plain words, never the key
    visible_models: list[str] = Field(default_factory=list)


class PhotoProvider(Protocol):
    name: ProviderId

    async def generate(
        self,
        prompt: str,
        width: int,
        height: int,
        out_path: Path,
        *,
        model: ImageModel,
        options: dict[str, str],
        seed: int | None = None,
        images: Sequence[Path] = (),
    ) -> PhotoResult:
        """Write one photo for the prompt to out_path, or raise PhotoUnavailable.

        The photo may come back at any size; the renderer crops it to fit. `options` is the
        model's preset with the studio's overrides. A seed, when the provider supports one,
        makes repeated calls differ. `images` (v6 Part B) are the product photos the model
        keeps, prepared as PNG files, at most the model's `max_input_images`; a provider that
        takes none is never given any.
        """
        ...

    async def test_key(self) -> KeyCheck:
        """Check the key for free: list the models it can see, make no photo."""
        ...


def key_works(count: int) -> str:
    """The key check's line for a key that sees `count` image models."""
    return KEY_WORKS_ONE if count == 1 else KEY_WORKS.format(count=count)


def fidelity_sentence(fidelity: str) -> str:
    """The sentence code puts before the photo prompt when product photos go with it: keep the
    product exactly ("exact"), or use it as a guide ("guide")."""
    return FIDELITY_GUIDE if fidelity == "guide" else FIDELITY_EXACT


def too_many_products(label: str, limit: int) -> str:
    """The line for a round with more product photos than the model takes: "Nano Banana 2.1
    takes 3 product photos; the first 3 were sent." """
    if limit == 1:
        return TOO_MANY_PRODUCTS_ONE.format(label=label)
    return TOO_MANY_PRODUCTS.format(label=label, photos=f"{limit} product photos", count=limit)


def write_photo(encoded: str, out_path: Path, provider: str) -> None:
    """Decode a base64 image from a provider's answer and save it as a PNG at out_path.
    Raises PhotoUnavailable, saying the answer held no photo, when it cannot be decoded."""
    try:
        data = base64.b64decode(encoded)
        image = Image.open(io.BytesIO(data))
        image.load()
    except (binascii.Error, ValueError, OSError) as error:
        raise PhotoUnavailable(NO_PHOTO_IN_ANSWER.format(provider=provider)) from error
    if image.mode not in ("RGB", "RGBA", "L", "LA", "P"):
        image = image.convert("RGB")  # a CMYK JPEG, say, which PNG cannot hold
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path, format="PNG")
