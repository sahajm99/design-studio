"""Imports a source's items into the library store, fetching each one once."""

from __future__ import annotations

import asyncio
import hashlib
import logging

from pydantic import BaseModel, Field

from studio.contracts import Reference, RefStatus
from studio.library.base import FetchedImage, FetchError, ImageFetcher, ReferenceSource, SourceItem
from studio.store import Store

logger = logging.getLogger(__name__)

# What an import says about an item whose fetch failed in a way the fetcher did not explain.
NOT_FETCHED = "The link could not be fetched."

# A reference already at one of these statuses has already been fetched once;
# only "unavailable" (or no reference at all) is fetched again.
_ALREADY_IMPORTED: tuple[RefStatus, ...] = ("pending", "analysed", "failed")

_EXTENSION_BY_MIME = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


class SkippedItem(BaseModel):
    label: str
    source_url: str | None
    reason: str


class ImportSummary(BaseModel):
    added: int = 0
    already_in_library: int = 0
    duplicates: int = 0
    unavailable: list[SkippedItem] = Field(default_factory=list)


def reference_id(item: SourceItem) -> str:
    """A stable short id for an item, derived from its address."""
    address = item.source_url if item.source_url is not None else (item.local_path or "")
    return hashlib.sha256(address.encode("utf-8")).hexdigest()[:16]


async def import_references(
    source: ReferenceSource, store: Store, fetcher: ImageFetcher, *, concurrency: int = 6
) -> ImportSummary:
    """Fetch every new item from `source` and record the outcome in `store`.

    Fetches run with at most `concurrency` requests in flight, but the results
    are applied to the store in the source's own item order, so the summary
    and the stored data never depend on which fetch happened to finish first.
    """
    items = source.items()
    summary = ImportSummary()

    to_fetch: list[SourceItem] = []
    for item in items:
        existing = store.get_reference(reference_id(item))
        if existing is not None and existing.status in _ALREADY_IMPORTED:
            summary.already_in_library += 1
        else:
            to_fetch.append(item)

    outcomes = await _fetch_all(to_fetch, fetcher, concurrency)

    for item, outcome in zip(to_fetch, outcomes):
        if isinstance(outcome, FetchError):
            _store_unavailable(store, item, outcome)
            summary.unavailable.append(
                SkippedItem(label=item.label, source_url=item.source_url, reason=str(outcome))
            )
            continue

        content_hash = hashlib.sha256(outcome.data).hexdigest()
        if store.find_reference_by_hash(content_hash) is not None:
            summary.duplicates += 1
            continue

        _store_added(store, item, outcome.data, outcome.mime_type, content_hash)
        summary.added += 1

    return summary


async def _fetch_all(
    items: list[SourceItem], fetcher: ImageFetcher, concurrency: int
) -> list[FetchedImage | FetchError]:
    semaphore = asyncio.Semaphore(concurrency)

    async def fetch_one(item: SourceItem) -> FetchedImage | FetchError:
        async with semaphore:
            try:
                return await fetcher.fetch(item)
            except FetchError as error:
                return error
            except Exception as error:
                # Any other failure is that one item's, so the rest of the import goes on. Only
                # the type is logged: the text of an unexpected error may hold a request address.
                logger.warning("A reference fetch failed with %s", type(error).__name__)
                return FetchError(NOT_FETCHED)

    return await asyncio.gather(*(fetch_one(item) for item in items))


def _store_unavailable(store: Store, item: SourceItem, error: FetchError) -> None:
    store.upsert_reference(
        Reference(
            id=reference_id(item),
            source=item.source,
            source_url=item.source_url,
            label=item.label,
            category=item.category,
            status="unavailable",
            error=str(error),
        )
    )


def _store_added(store: Store, item: SourceItem, data: bytes, mime_type: str, content_hash: str) -> None:
    extension = _EXTENSION_BY_MIME[mime_type]
    path = store.references_dir / f"{reference_id(item)}.{extension}"
    path.write_bytes(data)
    store.upsert_reference(
        Reference(
            id=reference_id(item),
            source=item.source,
            source_url=item.source_url,
            label=item.label,
            category=item.category,
            image_path=store.relative(path),
            content_hash=content_hash,
            status="pending",
        )
    )
