"""Build the configured web search provider from settings."""

from __future__ import annotations

from studio.config import Settings
from studio.research.base import SearchProvider, SearchUnavailable
from studio.research.fake import FakeSearchProvider
from studio.research.tavily import TavilySearchProvider

__all__ = [
    "FakeSearchProvider",
    "SearchProvider",
    "SearchUnavailable",
    "TavilySearchProvider",
    "get_search_provider",
]


def get_search_provider(settings: Settings) -> SearchProvider | None:
    """The search provider `settings.research_mode` selects, or None for 'none'."""
    mode = settings.research_mode
    if mode == "tavily":
        return TavilySearchProvider(api_key=settings.tavily_api_key)
    if mode == "fake":
        return FakeSearchProvider()
    return None
