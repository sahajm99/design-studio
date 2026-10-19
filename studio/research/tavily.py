"""The Tavily search provider (https://docs.tavily.com/documentation/api-reference/endpoint/search).

Tavily's free plan gives 1,000 search credits a month with no card; one basic search
spends one credit. The key goes in the request body, as Tavily's API accepts it, and is
never put in a message, an exception or a log line.

v7: the Settings page's key check reads the key's usage (`GET /usage`, the key as a bearer
token), which makes no search; Tavily's documentation gives that endpoint no credit cost.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from studio.contracts import SearchResult
from studio.photos.base import KEY_CHECK_FAILED, KEY_NOT_SET, KEY_REFUSED, KeyCheck
from studio.research.base import SearchUnavailable

_URL = "https://api.tavily.com/search"
_USAGE_URL = "https://api.tavily.com/usage"
_LABEL = "Tavily"
# What the key check says when it worked: with the plan's credits when Tavily gives them.
KEY_WORKS = "The key works."
KEY_WORKS_CREDITS = "The key works. {used} of {limit} credits used."
_BUSY = "Tavily is busy. Try again in a minute."
_UNREACHABLE = "Tavily could not be reached."


class TavilySearchProvider:
    """Searches the web through Tavily's search endpoint."""

    name = "tavily"

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.client = client
        self.timeout = timeout

    def __repr__(self) -> str:  # never show the key, even in a debugger or a traceback
        return f"TavilySearchProvider(timeout={self.timeout!r})"

    async def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        if not self.api_key:
            raise SearchUnavailable("No Tavily key is set.")

        payload = {
            "api_key": self.api_key,
            "query": query,
            "max_results": limit,
            "search_depth": "basic",
            "include_answer": False,
        }

        owns_client = self.client is None
        client = self.client or httpx.AsyncClient()
        try:
            response = await client.post(_URL, json=payload, timeout=self.timeout)
        except httpx.TimeoutException:
            # `from None`: the request (and with it the key) stays out of the traceback.
            message = f"Tavily did not answer within {self.timeout:.0f} seconds."
            raise SearchUnavailable(message) from None
        except httpx.RequestError:
            raise SearchUnavailable("Tavily could not be reached.") from None
        finally:
            if owns_client:
                await client.aclose()

        _raise_for_status(response)

        try:
            body: Any = response.json()
        except ValueError:
            raise SearchUnavailable(f"Tavily returned an error ({response.status_code}).") from None
        return _results(body)[:limit]

    async def test_key(self) -> KeyCheck:
        """The free key check (v7): the key's usage, no search. Plain words, never the key."""
        if not self.api_key:
            return KeyCheck(ok=False, message=KEY_NOT_SET.format(provider=_LABEL))
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient()
        try:
            response = await client.get(
                _USAGE_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
        except httpx.RequestError:
            # A timeout is a RequestError too. Nothing of the request is kept, nor its key.
            return KeyCheck(ok=False, message=_UNREACHABLE)
        finally:
            if owns_client:
                await client.aclose()
        status = response.status_code
        if status in (401, 403):
            return KeyCheck(ok=False, message=KEY_REFUSED.format(provider=_LABEL))
        if status == 429:
            return KeyCheck(ok=False, message=_BUSY)
        if not response.is_success:
            message = KEY_CHECK_FAILED.format(provider=_LABEL, status=status)
            return KeyCheck(ok=False, message=message)
        return KeyCheck(ok=True, message=_credits_line(response))


def _raise_for_status(response: httpx.Response) -> None:
    status = response.status_code
    if status in (401, 403):
        raise SearchUnavailable("Tavily rejected the key.")
    if status in (429, 432):
        raise SearchUnavailable("Tavily's free monthly credits are used up.")
    if not response.is_success:
        raise SearchUnavailable(f"Tavily returned an error ({status}).")


def _credits_line(response: httpx.Response) -> str:
    """The key check's line: the plan's credits used of its limit when the usage answer gives
    both as numbers (the account's plan first, then the key's own limit), else only that the
    key works."""
    try:
        body: Any = response.json()
    except ValueError:
        return KEY_WORKS
    if not isinstance(body, dict):
        return KEY_WORKS
    for part, used_field, limit_field in (
        ("account", "plan_usage", "plan_limit"),
        ("key", "usage", "limit"),
    ):
        block = body.get(part)
        if not isinstance(block, dict):
            continue
        used, limit = block.get(used_field), block.get(limit_field)
        if _is_count(used) and _is_count(limit):
            return KEY_WORKS_CREDITS.format(used=f"{used:,.0f}", limit=f"{limit:,.0f}")
    return KEY_WORKS


def _is_count(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _results(body: Any) -> list[SearchResult]:
    """`results[]` as SearchResults: title, url, the url's host as the domain, content as
    the snippet. An entry without a url, or with a url that does not parse, is skipped; one
    without a title uses its url."""
    if not isinstance(body, dict):
        return []
    results: list[SearchResult] = []
    for item in body.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        try:
            domain = urlparse(url).hostname or ""
        except ValueError:
            # A malformed address (an unclosed IPv6 bracket, say) costs that one result only.
            continue
        title = str(item.get("title") or "").strip() or url
        results.append(
            SearchResult(
                title=title,
                url=url,
                domain=domain,
                snippet=str(item.get("content") or "").strip(),
            )
        )
    return results
