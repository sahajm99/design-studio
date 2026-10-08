"""What the photo adapters share on the wire (v6): one request with the one retry each kind of
failure allows, and a provider's error answer read into plain parts.

The rules, for every provider: a rate limit (429) gets one wait, as long as the answer asks
and at most 20 seconds, and one more try; a server error (5xx) gets one more try at once; a
timeout or a network error fails the photo with a plain message. Keys travel in headers only,
so no address or message built here ever holds one.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from studio.photos.base import NO_ANSWER_IN_TIME, UNREACHABLE, PhotoUnavailable

MAX_RATE_LIMIT_WAIT = 20.0  # seconds
DEFAULT_RATE_LIMIT_WAIT = 5.0  # seconds, when the answer does not say
_MAX_DETAIL_CHARS = 160
# Google's "retry in" hint inside an error's details: "retryDelay": "17s".
_RETRY_DELAY_RE = re.compile(r"^(\d+(?:\.\d+)?)s$")
# The end of a provider's first sentence: a full stop and a space, or a line break.
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s|\n")

# Waits for the rate limit; a test can put an instant one in its place.
sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


@dataclass(frozen=True)
class ProviderError:
    """A provider's error answer: its code, its kind, its message, and all of them lowercased
    in one text for keyword checks. Each part is "" when the answer does not give it."""

    code: str
    kind: str
    message: str
    reasons: tuple[str, ...]

    @property
    def text(self) -> str:
        return " ".join([self.code, self.kind, self.message, *self.reasons]).lower()


@asynccontextmanager
async def client_for(client: httpx.AsyncClient | None) -> AsyncIterator[httpx.AsyncClient]:
    """The adapter's own client when it was given one, else a client for this request only."""
    if client is not None:
        yield client
        return
    async with httpx.AsyncClient() as own:
        yield own


async def send(
    request: Callable[[], Awaitable[httpx.Response]],
    *,
    provider: str,
    timeout: float,
    retry_rate_limit: Callable[[httpx.Response], bool],
) -> httpx.Response:
    """The provider's answer to `request`, after the one retry its failure allows: a 429 that
    `retry_rate_limit` says is a passing rate limit (not used-up credit) waits once and tries
    again; a 5xx tries again at once. Raises PhotoUnavailable on a timeout or a network error."""
    response = await _once(request, provider, timeout)
    if response.status_code == 429 and retry_rate_limit(response):
        await sleep(rate_limit_wait(response))
        response = await _once(request, provider, timeout)
    elif response.status_code >= 500:
        response = await _once(request, provider, timeout)
    return response


async def _once(
    request: Callable[[], Awaitable[httpx.Response]], provider: str, timeout: float
) -> httpx.Response:
    try:
        return await request()
    except httpx.TimeoutException as error:
        message = NO_ANSWER_IN_TIME.format(provider=provider, seconds=f"{timeout:.0f}")
        raise PhotoUnavailable(message) from error
    except httpx.RequestError as error:
        raise PhotoUnavailable(UNREACHABLE.format(provider=provider)) from error


def rate_limit_wait(response: httpx.Response) -> float:
    """How long a rate-limited answer asks to wait: its Retry-After header, else Google's
    retryDelay, else five seconds; never more than twenty."""
    wait = DEFAULT_RATE_LIMIT_WAIT
    header = response.headers.get("retry-after", "").strip()
    try:
        wait = float(header) if header else _retry_delay(response) or wait
    except ValueError:
        wait = _retry_delay(response) or wait
    return max(0.0, min(wait, MAX_RATE_LIMIT_WAIT))


def _retry_delay(response: httpx.Response) -> float | None:
    for detail in _error_object(response).get("details") or []:
        if isinstance(detail, dict):
            match = _RETRY_DELAY_RE.match(str(detail.get("retryDelay", "")))
            if match:
                return float(match.group(1))
    return None


def provider_error(response: httpx.Response) -> ProviderError:
    """The error in a provider's answer, as OpenAI and Google both shape it:
    `{"error": {"code", "type" or "status", "message", "details": [{"reason"}]}}`."""
    error = _error_object(response)
    reasons = tuple(
        str(detail["reason"])
        for detail in error.get("details") or []
        if isinstance(detail, dict) and detail.get("reason")
    )
    return ProviderError(
        code=str(error.get("code") or ""),
        kind=str(error.get("type") or error.get("status") or ""),
        message=str(error.get("message") or ""),
        reasons=reasons,
    )


def _error_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body: Any = response.json()
    except ValueError:
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, list) and error:  # Google sometimes answers with a list
        error = error[0]
    return error if isinstance(error, dict) else {}


def one_line(text: str) -> str:
    """A provider's message as one short line for a card: its first sentence, at most 160
    characters. "" for no message."""
    first = _SENTENCE_END_RE.split(text.strip(), maxsplit=1)[0].strip()
    if len(first) > _MAX_DETAIL_CHARS:
        first = first[: _MAX_DETAIL_CHARS - 1].rstrip() + "…"
    return first


def with_detail(message: str, detail: str) -> str:
    """The plain message, then the provider's own first sentence when it gave one."""
    line = one_line(detail)
    return f"{message} {line}" if line else message
