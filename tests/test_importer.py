"""Tests for HttpImageFetcher and import_references.

No network: the fetcher tests run against httpx.MockTransport; the importer
tests use a small fake fetcher, so the outcome never depends on timing.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from PIL import Image

from studio.library.base import FetchedImage, FetchError, SourceItem
from studio.library.fetch import HttpImageFetcher
from studio.library.importer import SkippedItem, import_references, reference_id
from studio.store import Store


def _image_bytes(
    fmt: str = "PNG", colour: tuple[int, int, int] = (10, 20, 30), size: tuple[int, int] = (6, 6)
) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format=fmt)
    return buffer.getvalue()


class _ListSource:
    """A ReferenceSource over a fixed, in-memory list of items."""

    name = "test"

    def __init__(self, items: list[SourceItem]) -> None:
        self._items = items

    def items(self) -> list[SourceItem]:
        return self._items


class _FakeFetcher:
    """Returns a canned FetchedImage or raises a canned FetchError, keyed by address."""

    def __init__(self, outcomes: dict[str, FetchedImage | FetchError]) -> None:
        self.outcomes = outcomes
        self.fetched: list[str] = []

    async def fetch(self, item: SourceItem) -> FetchedImage:
        key = item.source_url or item.local_path or ""
        self.fetched.append(key)
        outcome = self.outcomes[key]
        if isinstance(outcome, FetchError):
            raise outcome
        return outcome


# --------------------------------------------------------------------- HttpImageFetcher


async def test_fetcher_success() -> None:
    data = _image_bytes("JPEG")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=data, headers={"content-type": "image/jpeg"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = HttpImageFetcher(client=client)
        item = SourceItem(source="board", source_url="https://example.com/photo.jpg")

        result = await fetcher.fetch(item)
        await fetcher.aclose()

        assert not client.is_closed  # aclose() must not close a client it did not create

    assert result.data == data
    assert result.mime_type == "image/jpeg"
    assert len(seen) == 1
    assert str(seen[0].url) == "https://example.com/photo.jpg"
    assert seen[0].headers["user-agent"]
    assert "Mozilla" in seen[0].headers["user-agent"]


async def test_fetcher_not_found(tmp_path: Path) -> None:
    fetcher = HttpImageFetcher()
    item = SourceItem(source="folder", local_path=str(tmp_path / "missing.jpg"))

    with pytest.raises(FetchError, match=r"^File not found\.$"):
        await fetcher.fetch(item)

    await fetcher.aclose()


async def test_fetcher_timeout() -> None:
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler)) as client:
        fetcher = HttpImageFetcher(client=client)
        item = SourceItem(source="board", source_url="https://example.com/slow.jpg")

        with pytest.raises(FetchError, match=r"^The image host did not respond\.$"):
            await fetcher.fetch(item)

    def connect_error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(connect_error_handler)) as client:
        fetcher = HttpImageFetcher(client=client)
        item = SourceItem(source="board", source_url="https://example.com/unreachable.jpg")

        with pytest.raises(FetchError, match=r"^The image host did not respond\.$"):
            await fetcher.fetch(item)


async def test_fetcher_rejects_non_image() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not an image</html>", headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = HttpImageFetcher(client=client)
        item = SourceItem(source="board", source_url="https://example.com/fake.jpg")

        with pytest.raises(FetchError, match=r"^The link did not return an image\.$"):
            await fetcher.fetch(item)


async def test_fetcher_rejects_oversized() -> None:
    data = _image_bytes("PNG", size=(64, 64))
    assert len(data) > 100

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = HttpImageFetcher(client=client, max_bytes=100)
        item = SourceItem(source="board", source_url="https://example.com/big.png")

        # Below one megabyte, the limit reads in bytes, not "0 MB".
        with pytest.raises(FetchError, match=r"^The image is larger than 100 bytes\.$"):
            await fetcher.fetch(item)

    # Declared oversized by Content-Length alone; the real body here is tiny,
    # so this only fails if the header itself is checked.
    def lying_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"short", headers={"content-length": "999999999"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(lying_handler)) as client:
        fetcher = HttpImageFetcher(client=client, max_bytes=100)
        item = SourceItem(source="board", source_url="https://example.com/lying.png")

        with pytest.raises(FetchError, match=r"^The image is larger than 100 bytes\.$"):
            await fetcher.fetch(item)


async def test_fetcher_stops_reading_an_oversized_body() -> None:
    chunk = b"x" * 40
    produced: list[bytes] = []

    async def body_chunks() -> AsyncIterator[bytes]:
        for _ in range(10_000):  # 400,000 bytes if ever drained: far larger than max_bytes
            produced.append(chunk)
            yield chunk

    def handler(request: httpx.Request) -> httpx.Response:
        # No Content-Length at all, so only a running count of the bytes
        # actually read can catch this.
        return httpx.Response(200, content=body_chunks())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = HttpImageFetcher(client=client, max_bytes=100)
        item = SourceItem(source="board", source_url="https://example.com/huge.jpg")

        with pytest.raises(FetchError, match=r"^The image is larger than 100 bytes\.$"):
            await fetcher.fetch(item)

    # The fetcher must have stopped reading long before the body generator
    # was drained, instead of buffering the whole thing first.
    assert 0 < len(produced) < 10_000


async def test_fetcher_reads_local_file(tmp_path: Path) -> None:
    data = _image_bytes("JPEG")
    path = tmp_path / "local.jpg"
    path.write_bytes(data)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a local file must never cause a network request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = HttpImageFetcher(client=client)
        item = SourceItem(source="folder", local_path=str(path))

        result = await fetcher.fetch(item)

    assert result.data == data
    assert result.mime_type == "image/jpeg"


async def test_fetcher_rejects_unsupported_format() -> None:
    bmp_bytes = _image_bytes("BMP")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=bmp_bytes)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = HttpImageFetcher(client=client)
        item = SourceItem(source="board", source_url="https://example.com/old.bmp")

        with pytest.raises(FetchError, match=r"^The image format is not supported\.$"):
            await fetcher.fetch(item)


# ----------------------------------------------------------------------- import_references


def test_reference_id_is_stable() -> None:
    by_url = SourceItem(source="board", source_url="https://example.com/a.jpg")
    same_url = SourceItem(source="board", source_url="https://example.com/a.jpg", label="different label")
    other_url = SourceItem(source="board", source_url="https://example.com/b.jpg")
    by_local_path = SourceItem(source="folder", local_path="/pics/a.jpg")

    assert reference_id(by_url) == reference_id(same_url)
    assert reference_id(by_url) != reference_id(other_url)
    assert reference_id(by_url) != reference_id(by_local_path)

    id_value = reference_id(by_url)
    assert len(id_value) == 16
    assert all(char in "0123456789abcdef" for char in id_value)

    assert id_value == hashlib.sha256(b"https://example.com/a.jpg").hexdigest()[:16]
    assert reference_id(by_local_path) == hashlib.sha256(b"/pics/a.jpg").hexdigest()[:16]


async def test_import_counts_and_files(store: Store) -> None:
    added_jpeg = SourceItem(source="board", source_url="https://example.com/added.jpg", label="Added", category="Cat")
    added_png = SourceItem(source="board", source_url="https://example.com/added.png", label="Added 2", category="Cat")
    dup = SourceItem(source="board", source_url="https://example.com/dup.jpg", label="Dup", category="Cat")
    broken = SourceItem(source="board", source_url="https://example.com/broken.jpg", label="Broken", category="Cat")

    jpeg_bytes = _image_bytes("JPEG", colour=(1, 2, 3))
    png_bytes = _image_bytes("PNG", colour=(4, 5, 6))

    fetcher = _FakeFetcher(
        {
            "https://example.com/added.jpg": FetchedImage(data=jpeg_bytes, mime_type="image/jpeg"),
            "https://example.com/added.png": FetchedImage(data=png_bytes, mime_type="image/png"),
            # Same bytes as added.jpg: a duplicate by content, not by address.
            "https://example.com/dup.jpg": FetchedImage(data=jpeg_bytes, mime_type="image/jpeg"),
            "https://example.com/broken.jpg": FetchError("The image host returned 404."),
        }
    )
    source = _ListSource([added_jpeg, added_png, dup, broken])

    summary = await import_references(source, store, fetcher)

    assert summary.added == 2
    assert summary.duplicates == 1
    assert summary.already_in_library == 0
    assert summary.unavailable == [
        SkippedItem(label="Broken", source_url="https://example.com/broken.jpg", reason="The image host returned 404.")
    ]

    added_ref = store.get_reference(reference_id(added_jpeg))
    assert added_ref is not None
    assert added_ref.status == "pending"
    assert added_ref.label == "Added"
    assert added_ref.category == "Cat"
    assert added_ref.content_hash is not None
    assert added_ref.source_url == "https://example.com/added.jpg"
    assert store.media_path(added_ref.image_path).name == f"{reference_id(added_jpeg)}.jpg"
    assert store.media_path(added_ref.image_path).read_bytes() == jpeg_bytes

    added_png_ref = store.get_reference(reference_id(added_png))
    assert added_png_ref is not None
    assert added_png_ref.source_url == "https://example.com/added.png"
    assert store.media_path(added_png_ref.image_path).name == f"{reference_id(added_png)}.png"

    # The duplicate's bytes matched another reference's, so nothing was ever stored for it.
    assert store.get_reference(reference_id(dup)) is None

    broken_ref = store.get_reference(reference_id(broken))
    assert broken_ref is not None
    assert broken_ref.status == "unavailable"
    assert broken_ref.error == "The image host returned 404."
    assert broken_ref.source_url == "https://example.com/broken.jpg"
    assert broken_ref.label == "Broken"
    assert broken_ref.category == "Cat"


async def test_reimport_is_idempotent(store: Store) -> None:
    item = SourceItem(source="board", source_url="https://example.com/once.jpg", label="Once")
    fetcher = _FakeFetcher({"https://example.com/once.jpg": FetchedImage(data=_image_bytes(), mime_type="image/png")})
    source = _ListSource([item])

    first = await import_references(source, store, fetcher)
    assert first.added == 1
    assert fetcher.fetched == ["https://example.com/once.jpg"]

    second = await import_references(source, store, fetcher)

    assert second.added == 0
    assert second.duplicates == 0
    assert second.already_in_library == 1
    # The second run never re-fetched the item already in the library.
    assert fetcher.fetched == ["https://example.com/once.jpg"]


async def test_unavailable_is_stored_and_retried(store: Store) -> None:
    item = SourceItem(source="board", source_url="https://example.com/flaky.jpg", label="Flaky")
    source = _ListSource([item])

    failing_fetcher = _FakeFetcher({"https://example.com/flaky.jpg": FetchError("The image host returned 503.")})
    first = await import_references(source, store, failing_fetcher)

    assert first.added == 0
    assert first.already_in_library == 0
    assert len(first.unavailable) == 1
    stored = store.get_reference(reference_id(item))
    assert stored is not None
    assert stored.status == "unavailable"
    assert stored.error == "The image host returned 503."

    succeeding_fetcher = _FakeFetcher(
        {"https://example.com/flaky.jpg": FetchedImage(data=_image_bytes(), mime_type="image/png")}
    )
    second = await import_references(source, store, succeeding_fetcher)

    # Unlike pending/analysed/failed, an unavailable reference is tried again.
    assert succeeding_fetcher.fetched == ["https://example.com/flaky.jpg"]
    assert second.added == 1
    assert second.already_in_library == 0
    retried = store.get_reference(reference_id(item))
    assert retried is not None
    assert retried.status == "pending"
    assert retried.error is None
