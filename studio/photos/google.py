"""The Google photo provider (v6): Nano Banana models through the Gemini API, on the image key.

One photo per request: `POST /v1beta/models/{id}:generateContent`, the key in the
`x-goog-api-key` header, asking for `responseModalities: ["IMAGE"]` with
`imageConfig: {aspectRatio, imageSize}`. The aspect ratio is the supported one nearest the
post's (1080 x 1350 asks for 4:5), and the image size is the model's preset ("2K"). The first
`inlineData` part of the answer is the photo; a `finishReason` of SAFETY or IMAGE_SAFETY, or a
blocked prompt, is the model declining it. The cost is the catalogue's list price.

The key is `GOOGLE_IMAGE_API_KEY`, not the text key: Google's image models have no free tier,
and billing applies to a whole Google project, so image generation lives in a project of its
own. The key check is free: `GET /v1beta/models`, counting the image models the key can see.

v6 Part B: with product photos, each goes first in `contents.parts` as an `inlineData` part
(PNG), then the text. The cost then adds the input tokens the answer's `usageMetadata`
reports, at the catalogue's input token price, and a prompt Google blocks, the product photos
being part of it, reads "The model declined the product photo."
"""

from __future__ import annotations

import base64
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from studio.photos.base import (
    DECLINED,
    GOOGLE_BILLING,
    KEY_CHECK_FAILED,
    KEY_NOT_SET,
    KEY_REFUSED,
    MODEL_NOT_ALLOWED,
    NO_PHOTO_IN_ANSWER,
    PRODUCT_DECLINED,
    PROVIDER_BUSY,
    PROVIDER_PROBLEM,
    KeyCheck,
    PhotoResult,
    PhotoUnavailable,
    key_works,
    write_photo,
)
from studio.photos.catalogue import ImageModel
from studio.photos.transport import client_for, provider_error, send, with_detail
from studio.secrets import redact

_GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_KEY_CHECK_TIMEOUT = 30.0
_LABEL = "Google"
# The aspect ratios Gemini's image models take.
_ASPECT_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
# The reasons Google gives for a prompt or a photo its safety rules stopped.
_SAFETY_REASONS = frozenset(
    {
        "SAFETY",
        "IMAGE_SAFETY",
        "PROHIBITED_CONTENT",
        "IMAGE_PROHIBITED_CONTENT",
        "BLOCKLIST",
        "SPII",
    }
)
# Signs that a 429 is the free tier's zero allowance, which billing fixes and waiting does not.
_FREE_TIER_SIGNS = ("free_tier", "limit: 0")


class GooglePhotoProvider:
    """Makes a photo with one of Google's Gemini image models."""

    name = "google"

    def __init__(
        self, api_key: str, *, client: httpx.AsyncClient | None = None, timeout: float = 180.0
    ) -> None:
        self.api_key = api_key
        self.client = client
        self.timeout = timeout

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
        # `seed` is not sent: one photo per request, and a new request draws a new photo.
        if not self.api_key:
            raise self._unavailable(KEY_NOT_SET.format(provider=_LABEL))
        image_config = {"aspectRatio": nearest_aspect_ratio(width, height)}
        if options.get("image_size"):
            image_config["imageSize"] = options["image_size"]
        sent = list(images)[: model.max_input_images] if model.max_input_images else []
        # The product photos first, each as inline PNG data, then the words.
        parts: list[dict[str, Any]] = [
            {
                "inlineData": {
                    "mimeType": "image/png",
                    "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                }
            }
            for path in sent
        ]
        parts.append({"text": prompt})
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": image_config},
        }
        url = _GENERATE_URL.format(model=model.id)

        async with client_for(self.client) as client:
            # No second try on a 5xx: a gateway error may come after the photo was billed.
            response = await send(
                lambda: client.post(url, json=body, headers=self._headers(), timeout=self.timeout),
                provider=_LABEL,
                timeout=self.timeout,
                retry_rate_limit=_passing_rate_limit,
                retry_server_error=False,
            )
        if not response.is_success:
            raise self._unavailable(_failure_message(response, model, with_images=bool(sent)))

        try:
            answer: Any = response.json()
        except ValueError:
            answer = {}
        answer = answer if isinstance(answer, dict) else {}
        encoded = self._photo_in(answer, with_images=bool(sent))
        write_photo(encoded, out_path, _LABEL)
        usage_metadata = answer.get("usageMetadata")
        usage = {
            key: value
            for key, value in (usage_metadata if isinstance(usage_metadata, dict) else {}).items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
        return PhotoResult(
            path=str(out_path),
            provider=f"{self.name}:{model.short_id}",
            model_id=model.id,
            prompt=prompt,
            cost_usd=_cost(model, options, usage, input_images=len(sent)),
            cost_basis="list_price",
            usage=usage,
            input_images=len(sent),
        )

    async def test_key(self) -> KeyCheck:
        if not self.api_key:
            return KeyCheck(ok=False, message=KEY_NOT_SET.format(provider=_LABEL))
        try:
            async with client_for(self.client) as client:
                response = await send(
                    lambda: client.get(
                        _MODELS_URL,
                        params={"pageSize": 1000},
                        headers=self._headers(),
                        timeout=_KEY_CHECK_TIMEOUT,
                    ),
                    provider=_LABEL,
                    timeout=_KEY_CHECK_TIMEOUT,
                    retry_rate_limit=_passing_rate_limit,
                )
        except PhotoUnavailable as error:
            return KeyCheck(ok=False, message=self._redacted(str(error)))
        if not response.is_success:
            if _key_refused(response):
                return KeyCheck(ok=False, message=KEY_REFUSED.format(provider=_LABEL))
            message = KEY_CHECK_FAILED.format(provider=_LABEL, status=response.status_code)
            return KeyCheck(ok=False, message=message)
        try:
            listed: Any = response.json().get("models") or []
        except (ValueError, AttributeError):
            listed = []
        names = [
            str(item.get("name", "")).removeprefix("models/")
            for item in listed
            if isinstance(item, dict)
        ]
        visible = sorted(name for name in names if "image" in name or name.startswith("imagen"))
        return KeyCheck(ok=True, message=key_works(len(visible)), visible_models=visible)

    def _photo_in(self, answer: dict[str, Any], *, with_images: bool = False) -> str:
        """The answer's first inline image, or the plain message for why it holds none. A
        blocked prompt that carried product photos was refused on its input, the photos
        included (spec v6, B7)."""
        feedback = answer.get("promptFeedback") or {}
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            reason = str(feedback.get("blockReasonMessage") or "")
            raise self._unavailable(
                with_detail(PRODUCT_DECLINED if with_images else DECLINED, reason)
            )
        candidates = answer.get("candidates") or []
        candidate = candidates[0] if isinstance(candidates, list) and candidates else {}
        candidate = candidate if isinstance(candidate, dict) else {}
        content = candidate.get("content") or {}
        parts = content.get("parts") if isinstance(content, dict) else None
        for part in parts if isinstance(parts, list) else []:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                return str(inline["data"])
        if str(candidate.get("finishReason", "")) in _SAFETY_REASONS:
            reason = str(candidate.get("finishMessage") or "")
            raise self._unavailable(with_detail(DECLINED, reason))
        raise self._unavailable(NO_PHOTO_IN_ANSWER.format(provider=_LABEL))

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self.api_key}

    def _redacted(self, message: str) -> str:
        return redact(message, [self.api_key])

    def _unavailable(self, message: str) -> PhotoUnavailable:
        return PhotoUnavailable(self._redacted(message))


def nearest_aspect_ratio(width: int, height: int) -> str:
    """The supported aspect ratio closest to width by height: 1080 x 1350 gives "4:5"."""
    target = math.log(max(width, 1) / max(height, 1))

    def distance(ratio: str) -> float:
        across, down = (int(part) for part in ratio.split(":"))
        return abs(math.log(across / down) - target)

    return min(_ASPECT_RATIOS, key=distance)


def _cost(
    model: ImageModel, options: dict[str, str], usage: dict[str, int], *, input_images: int = 0
) -> float | None:
    """The photo's cost: the catalogue's list price for the options; with product photos sent
    (v6 Part B), plus the input tokens the answer reported (`promptTokenCount`) at the
    catalogue's input token price, or the catalogue's estimate when it reported none."""
    price = model.price_for(options)
    if price is None or not input_images:
        return price
    tokens = usage.get("promptTokenCount")
    if tokens is not None:
        return price + tokens * model.input_token_price_usd
    return price + model.input_cost(input_images)


def _failure_message(
    response: httpx.Response, model: ImageModel, *, with_images: bool = False
) -> str:
    """The plain message for a refused request, by what Google answered. With product photos
    sent, a safety refusal is on the input they are part of (spec v6, B7)."""
    status = response.status_code
    error = provider_error(response)
    text = error.text
    if _key_refused(response):
        return KEY_REFUSED.format(provider=_LABEL)
    if status == 429 and any(sign in text for sign in _FREE_TIER_SIGNS):
        return GOOGLE_BILLING
    if status == 429:
        return PROVIDER_BUSY.format(provider=_LABEL)
    if status == 403 or "billing" in text:
        return GOOGLE_BILLING
    if status == 404:
        return MODEL_NOT_ALLOWED.format(label=model.label)
    if any(reason.lower() in text for reason in _SAFETY_REASONS):
        return with_detail(PRODUCT_DECLINED if with_images else DECLINED, error.message)
    if status >= 500:
        return PROVIDER_PROBLEM.format(provider=_LABEL)
    return with_detail(PROVIDER_PROBLEM.format(provider=_LABEL), error.message)


def _key_refused(response: httpx.Response) -> bool:
    """Whether Google refused the key itself: a 401, or its "API key not valid" answer, which
    comes as a 400 or a 403."""
    text = provider_error(response).text
    return (
        response.status_code == 401
        or "api_key_invalid" in text
        or "api key not valid" in text
        or "api key expired" in text
    )


def _passing_rate_limit(response: httpx.Response) -> bool:
    """Whether a 429 is a passing rate limit, worth one wait; the free tier's zero is not."""
    text = provider_error(response).text
    return not any(sign in text for sign in _FREE_TIER_SIGNS)
