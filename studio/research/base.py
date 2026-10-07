"""The interface every web search source implements."""

from __future__ import annotations

from typing import Protocol

from studio.contracts import SearchResult

__all__ = ["SearchProvider", "SearchResult", "SearchUnavailable"]


class SearchUnavailable(Exception):
    """The search could not be made; the message is what the designer sees.

    It is shown as it is, so it must be plain language and must never contain a key.
    """


class SearchProvider(Protocol):
    name: str

    async def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        """At most `limit` results for the query, in the provider's order, or raise
        SearchUnavailable. An empty list means the search ran and found nothing."""
        ...
