"""The interfaces for collecting references."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class SourceItem(BaseModel):
    """One reference a source knows about, before its image is fetched."""

    source: str  # the source's name, e.g. "board" or "folder"
    source_url: str | None = None  # where the image lives on the web
    local_path: str | None = None  # or where it lives on disk
    label: str = ""
    category: str = ""


class ReferenceSource(Protocol):
    name: str

    def items(self) -> list[SourceItem]: ...


class FetchedImage(BaseModel):
    data: bytes
    mime_type: str  # e.g. "image/jpeg"


class FetchError(Exception):
    """The image could not be fetched. The message is shown to the designer."""


class ImageFetcher(Protocol):
    async def fetch(self, item: SourceItem) -> FetchedImage:
        """Return the image bytes for an item, or raise FetchError."""
        ...
