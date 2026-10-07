"""A deterministic fake search provider: used in demo mode and in tests."""

from __future__ import annotations

import hashlib
import re

from studio.contracts import SearchResult

_RESULT_COUNT = 3
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SNIPPETS = (
    "A demo result about {query}, made without a web search.",
    "Demo mode: this page about {query} was made up, not found on the web.",
    "A made-up example of how {query} might be shown, for demo mode.",
)


class FakeSearchProvider:
    """Answers every query with three made-up results on example.com.

    The same query always gives the same results, built from its SHA-256, so demo-mode
    runs and tests are reproducible without a network call. Different queries give
    different urls, so results from two queries never collapse into one.
    """

    name = "fake"

    async def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
        slug = _slug(query, digest)
        results = []
        for n in range(1, _RESULT_COUNT + 1):
            snippet = _SNIPPETS[int(digest[n * 2 : n * 2 + 2], 16) % len(_SNIPPETS)]
            results.append(
                SearchResult(
                    title=f"How clinics present {query}: ideas {n}",
                    url=f"https://example.com/{slug}-{n}",
                    domain="example.com",
                    snippet=snippet.format(query=query),
                )
            )
        return results[: max(limit, 0)]


def _slug(query: str, digest: str) -> str:
    """The query's words in lower case joined by dashes (at most 40 characters), then the
    first eight hex digits of its hash, so two long queries that share a start still differ."""
    words = _SLUG_RE.sub("-", query.lower()).strip("-")[:40].strip("-")
    return f"{words}-{digest[:8]}" if words else digest[:8]
