"""The Cloudflare Workers AI photo provider, calling a hosted FLUX model.

As of 2026-10-05, the model's own documentation page
(https://developers.cloudflare.com/workers-ai/models/flux-2-klein-4b/, and
its sibling flux-2-dev) confirms only that the request body is
multipart/form-data and that a successful response looks like
``{"result": {"image": "<base64>"}, "success": true, "errors": [], "messages": []}``.
It does not publish the multipart field names or any width/height limits
(its "Parameters" section shows a single opaque ``multipart{} object
required`` entry, unlike plain-JSON models such as flux-1-schnell, whose page
lists ``prompt`` and ``steps`` explicitly). So the field names below
(``prompt``, ``width``, ``height``) and the nearest-multiple-of-32 rounding
are this provider's own choice, not a page-documented rule. The word "seed"
does not appear anywhere on the page either, so `generate`'s `seed`
parameter, added to match the `PhotoProvider` protocol, is accepted but
never sent. See the task report for the exact page contents this was built
against.

v6: the model comes from the catalogue entry (the one the provider was built with when no
entry is given, as v5 called it), every photo reports a cost of 0 on the free daily allowance,
a server error gets one more try, and the free key check lists the account's text-to-image
models. Every message passes through `redact`, so neither the token nor the account id can
reach a card or the log.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from studio.photos.base import (
    KEY_CHECK_FAILED,
    KEY_REFUSED,
    KeyCheck,
    PhotoResult,
    PhotoUnavailable,
    key_works,
)
from studio.photos.catalogue import ImageModel
from studio.photos.transport import client_for, send
from studio.secrets import redact

_URL_TEMPLATE = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
_MODELS_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/models/search"
_SIZE_MULTIPLE = 32
_WATERMARK_CLAUSE = "No text, no logos, no watermark."
_KEY_CHECK_TIMEOUT = 30.0
_LABEL = "Cloudflare"

CREDENTIALS_NOT_SET = "Cloudflare credentials are not set."


class CloudflarePhotoProvider:
    """Generates a photo with Cloudflare Workers AI's hosted FLUX model."""

    name = "cloudflare"

    def __init__(
        self,
        account_id: str,
        api_token: str,
        model: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 180.0,
    ) -> None:
        self.account_id = account_id
        self.api_token = api_token
        self.model = model  # the model id used when `generate` is given no catalogue entry
        self.client = client
        self.timeout = timeout

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
    ) -> PhotoResult:
        if not self.account_id or not self.api_token:
            raise PhotoUnavailable(CREDENTIALS_NOT_SET)

        model_id = model.id if model is not None else self.model
        full_prompt = _with_watermark_clause(prompt)
        url = _URL_TEMPLATE.format(account_id=self.account_id, model=model_id)
        # `seed` is not sent: this model's page documents no such field (see the
        # module docstring), so there is nothing to put it in. FLUX.2 klein has no options.
        fields = {
            "prompt": (None, full_prompt),
            "width": (None, str(_round_size(width))),
            "height": (None, str(_round_size(height))),
        }

        owns_client = self.client is None
        client = self.client or httpx.AsyncClient()
        try:
            response = await self._send(client, url, self._headers(), fields)
        finally:
            if owns_client:
                await client.aclose()

        self._raise_for_status(response)

        body = response.json()
        encoded = ((body or {}).get("result") or {}).get("image")
        if not encoded:
            raise PhotoUnavailable("Cloudflare returned no image.")

        image_bytes = base64.b64decode(encoded)
        image = Image.open(io.BytesIO(image_bytes))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(out_path, format="PNG")

        return PhotoResult(
            path=str(out_path),
            provider=f"{self.name}:{model_id.rsplit('/', 1)[-1]}",
            model_id=model_id,
            prompt=full_prompt,
            cost_usd=0.0,
            cost_basis="free_allowance",
        )

    async def test_key(self) -> KeyCheck:
        """The free check: the account's text-to-image models, as the token sees them."""
        if not self.account_id or not self.api_token:
            return KeyCheck(ok=False, message=CREDENTIALS_NOT_SET)
        url = _MODELS_URL.format(account_id=self.account_id)
        try:
            async with client_for(self.client) as client:
                response = await send(
                    lambda: client.get(
                        url,
                        params={"task": "Text-to-Image"},
                        headers=self._headers(),
                        timeout=_KEY_CHECK_TIMEOUT,
                    ),
                    provider=_LABEL,
                    timeout=_KEY_CHECK_TIMEOUT,
                    retry_rate_limit=lambda response: True,
                )
        except PhotoUnavailable as error:
            return KeyCheck(ok=False, message=self._redacted(str(error)))
        if response.status_code in (401, 403):
            return KeyCheck(ok=False, message=KEY_REFUSED.format(provider=_LABEL))
        if not response.is_success:
            message = KEY_CHECK_FAILED.format(provider=_LABEL, status=response.status_code)
            return KeyCheck(ok=False, message=message)
        try:
            listed: Any = response.json().get("result") or []
        except (ValueError, AttributeError):
            listed = []
        names = sorted(
            str(item["name"]) for item in listed if isinstance(item, dict) and item.get("name")
        )
        return KeyCheck(ok=True, message=key_works(len(names)), visible_models=names)

    async def _send(
        self, client: httpx.AsyncClient, url: str, headers: dict[str, str], fields: dict[str, Any]
    ) -> httpx.Response:
        """Send the request, and send it once more when the first attempt times out, and once
        more when the answer is a server error.

        Only a second timeout fails the photo. Any other network error, such as a refused
        connection or a failed DNS lookup, fails it at once.
        """

        async def post() -> httpx.Response:
            return await client.post(url, headers=headers, files=fields, timeout=self.timeout)

        try:
            try:
                response = await post()
            except httpx.TimeoutException:
                response = await post()
            if response.status_code >= 500:
                response = await post()
            return response
        except httpx.TimeoutException as exc:
            message = f"Cloudflare did not answer within {self.timeout:.0f} seconds."
            raise PhotoUnavailable(message) from exc
        except httpx.RequestError as exc:
            raise PhotoUnavailable("Cloudflare could not be reached.") from exc

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_token}"}

    def _redacted(self, message: str) -> str:
        return redact(message, [self.api_token, self.account_id])

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if status in (401, 403):
            raise PhotoUnavailable("Cloudflare rejected the credentials.")
        if status == 429:
            raise PhotoUnavailable("Cloudflare's free daily allowance is used up.")
        if not response.is_success:
            message = f"Cloudflare returned an error ({status})."
            detail = _first_error_message(response)
            if detail:
                message = f"{message} {detail}"
            raise PhotoUnavailable(self._redacted(message))


def _with_watermark_clause(prompt: str) -> str:
    if "watermark" in prompt.lower():
        return prompt
    return f"{prompt} {_WATERMARK_CLAUSE}"


def _round_size(value: int) -> int:
    """Round to the nearest multiple of 32.

    The model's page does not publish a size rule (see the module docstring),
    so this is a documented engineering default, not a page-confirmed one.
    """
    rounded = round(value / _SIZE_MULTIPLE) * _SIZE_MULTIPLE
    return max(rounded, _SIZE_MULTIPLE)


def _first_error_message(response: httpx.Response) -> str | None:
    try:
        body: Any = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    for error in body.get("errors") or []:
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str) and error:
            return error
    return None
