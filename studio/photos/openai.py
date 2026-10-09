"""The OpenAI photo provider (v6): GPT Image models through the Images API, on the designer's key.

One photo per request: `POST /v1/images/generations` with JSON `model`, `prompt`, `size`,
`quality`, `n: 1` and `output_format: png`, the key in the Authorization header. The size is
the post's, rounded up to multiples of 16 (1080 x 1350 asks for 1088 x 1360, which GPT Image
2.5 Flare was seen to accept on 2026-10-08), unless the model's preset names a size of its
own. The answer's `data[0].b64_json` is the photo and `usage.output_tokens` prices it, at the
catalogue's price a token; an answer without usage is priced at the list price instead.

The key check is free: `GET /v1/models`, counting the image models the key can see.

v6 Part B: with product photos, the request goes to `POST /v1/images/edits` instead, as
multipart: `model`, one `image[]` file for each product photo (PNG), `prompt`, `size`,
`quality`, `n: 1` and `output_format: png`. `input_fidelity` is not sent: OpenAI's guide does not
document it for GPT Image 2.5. The cost then adds the input image tokens the usage reports, at
the catalogue's input token price, and a safety refusal reads "The model declined the product
photo."
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from studio.photos.base import (
    DECLINED,
    KEY_CHECK_FAILED,
    KEY_NOT_SET,
    KEY_REFUSED,
    MODEL_NOT_ALLOWED,
    NO_PHOTO_IN_ANSWER,
    OPENAI_CREDIT,
    OPENAI_VERIFY,
    PRODUCT_DECLINED,
    PROVIDER_BUSY,
    PROVIDER_PROBLEM,
    KeyCheck,
    PhotoResult,
    PhotoUnavailable,
    key_works,
    write_photo,
)
from studio.photos.catalogue import ImageModel, PriceBasis
from studio.photos.transport import (
    ProviderError,
    client_for,
    provider_error,
    send,
    with_detail,
)
from studio.secrets import redact

_GENERATIONS_URL = "https://api.openai.com/v1/images/generations"
_EDITS_URL = "https://api.openai.com/v1/images/edits"
_MODELS_URL = "https://api.openai.com/v1/models"
_SIZE_MULTIPLE = 16
_KEY_CHECK_TIMEOUT = 30.0
_LABEL = "OpenAI"
# Error codes OpenAI gives a prompt its safety system refused.
_SAFETY_CODES = ("moderation_blocked", "content_policy_violation")


class OpenAIPhotoProvider:
    """Makes a photo with one of OpenAI's GPT Image models."""

    name = "openai"

    def __init__(
        self, api_key: str, *, client: httpx.AsyncClient | None = None, timeout: float = 300.0
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
        # `seed` is not sent: the Images API has no such field.
        if not self.api_key:
            raise self._unavailable(KEY_NOT_SET.format(provider=_LABEL))
        body: dict[str, Any] = {
            "model": model.id,
            "prompt": prompt,
            "size": options.get("size") or f"{_round_up(width)}x{_round_up(height)}",
            "n": 1,
            "output_format": "png",
        }
        if options.get("quality"):
            body["quality"] = options["quality"]
        sent = list(images)[: model.max_input_images] if model.max_input_images else []
        # Read once, so a retry sends the same bytes again.
        files = [
            ("image[]", (f"product-{number}.png", path.read_bytes(), "image/png"))
            for number, path in enumerate(sent, start=1)
        ]
        fields = {name: str(value) for name, value in body.items()}

        async with client_for(self.client) as client:

            def request() -> Any:
                if files:
                    return client.post(
                        _EDITS_URL,
                        data=fields,
                        files=files,
                        headers=self._headers(),
                        timeout=self.timeout,
                    )
                return client.post(
                    _GENERATIONS_URL, json=body, headers=self._headers(), timeout=self.timeout
                )

            response = await send(
                request,
                provider=_LABEL,
                timeout=self.timeout,
                retry_rate_limit=_passing_rate_limit,
            )
        if not response.is_success:
            raise self._unavailable(_failure_message(response, model, with_images=bool(files)))

        try:
            answer: Any = response.json()
        except ValueError:
            answer = None
        items = answer.get("data") if isinstance(answer, dict) else None
        first = items[0] if isinstance(items, list) and items else {}
        encoded = first.get("b64_json") if isinstance(first, dict) else None
        if not encoded:
            raise self._unavailable(NO_PHOTO_IN_ANSWER.format(provider=_LABEL))
        write_photo(encoded, out_path, _LABEL)

        raw_usage = answer.get("usage") if isinstance(answer, dict) else None
        usage = _counts(raw_usage)
        cost, basis = _cost(model, options, usage, input_images=len(files))
        return PhotoResult(
            path=str(out_path),
            provider=f"{self.name}:{model.short_id}",
            model_id=model.id,
            prompt=prompt,
            cost_usd=cost,
            cost_basis=basis,
            usage=usage,
            input_images=len(files),
        )

    async def test_key(self) -> KeyCheck:
        if not self.api_key:
            return KeyCheck(ok=False, message=KEY_NOT_SET.format(provider=_LABEL))
        try:
            async with client_for(self.client) as client:
                response = await send(
                    lambda: client.get(
                        _MODELS_URL, headers=self._headers(), timeout=_KEY_CHECK_TIMEOUT
                    ),
                    provider=_LABEL,
                    timeout=_KEY_CHECK_TIMEOUT,
                    retry_rate_limit=_passing_rate_limit,
                )
        except PhotoUnavailable as error:
            return KeyCheck(ok=False, message=self._redacted(str(error)))
        if response.status_code == 401:
            return KeyCheck(ok=False, message=KEY_REFUSED.format(provider=_LABEL))
        if not response.is_success:
            message = KEY_CHECK_FAILED.format(provider=_LABEL, status=response.status_code)
            return KeyCheck(ok=False, message=message)
        try:
            listed: Any = response.json().get("data") or []
        except (ValueError, AttributeError):
            listed = []
        visible = sorted(
            str(item["id"])
            for item in listed
            if isinstance(item, dict) and _is_image_model(str(item.get("id", "")))
        )
        return KeyCheck(ok=True, message=key_works(len(visible)), visible_models=visible)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _redacted(self, message: str) -> str:
        return redact(message, [self.api_key])

    def _unavailable(self, message: str) -> PhotoUnavailable:
        return PhotoUnavailable(self._redacted(message))


def _failure_message(
    response: httpx.Response, model: ImageModel, *, with_images: bool = False
) -> str:
    """The plain message for a refused request, by what OpenAI answered. With product photos
    sent, a safety refusal is on the input they are part of (spec v6, B7)."""
    status = response.status_code
    error = provider_error(response)
    text = error.text
    if status == 401 or "invalid_api_key" in text or "incorrect api key" in text:
        return KEY_REFUSED.format(provider=_LABEL)
    if status == 403 and "verif" in text:
        return OPENAI_VERIFY
    if status == 429 and "insufficient_quota" in text:
        return OPENAI_CREDIT
    if status == 429:
        return PROVIDER_BUSY.format(provider=_LABEL)
    if _is_safety_refusal(error):
        return with_detail(PRODUCT_DECLINED if with_images else DECLINED, error.message)
    if status in (403, 404) or "model_not_found" in text or "does not exist" in text:
        return MODEL_NOT_ALLOWED.format(label=model.label)
    if status >= 500:
        return PROVIDER_PROBLEM.format(provider=_LABEL)
    return with_detail(PROVIDER_PROBLEM.format(provider=_LABEL), error.message)


def _passing_rate_limit(response: httpx.Response) -> bool:
    """Whether a 429 is a passing rate limit, worth one wait; used-up credit is not."""
    return "insufficient_quota" not in provider_error(response).text


def _is_safety_refusal(error: ProviderError) -> bool:
    return error.code in _SAFETY_CODES or "safety system" in error.text


def _is_image_model(model_id: str) -> bool:
    return "image" in model_id or model_id.startswith("dall-e")


def _round_up(value: int) -> int:
    """The size rounded up to the next multiple of 16, as GPT Image sizes must be."""
    return max(_SIZE_MULTIPLE, -(-value // _SIZE_MULTIPLE) * _SIZE_MULTIPLE)


def _counts(raw: Any) -> dict[str, int]:
    """The usage's top-level counts: `input_tokens`, `output_tokens`, `total_tokens`; and, from
    `input_tokens_details` (v6 Part B), `input_image_tokens` and `input_text_tokens`."""
    if not isinstance(raw, dict):
        return {}
    counts = {
        key: value
        for key, value in raw.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }
    details = raw.get("input_tokens_details")
    if isinstance(details, dict):
        for name in ("image_tokens", "text_tokens"):
            value = details.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                counts[f"input_{name}"] = value
    return counts


def _cost(
    model: ImageModel, options: dict[str, str], usage: dict[str, int], *, input_images: int = 0
) -> tuple[float | None, PriceBasis]:
    """The photo's cost: its output tokens at the catalogue's price a token when OpenAI
    reported them, else the list price for the options (None when neither is known). With
    product photos sent (v6 Part B), their input image tokens are added at the catalogue's input
    token price: as reported, else all the input tokens reported, else the catalogue's estimate."""
    tokens = usage.get("output_tokens")
    if tokens is not None and model.output_token_price_usd > 0:
        cost = tokens * model.output_token_price_usd
        if input_images:
            image_tokens = usage.get("input_image_tokens", usage.get("input_tokens"))
            cost += (
                image_tokens * model.input_token_price_usd
                if image_tokens is not None
                else model.input_cost(input_images)
            )
        return cost, "usage"
    price = model.price_for(options)
    if price is not None and input_images:
        price += model.input_cost(input_images)
    return price, "list_price"
