"""Fetches one reference image's bytes, either from the web or from disk."""

from __future__ import annotations

import io
from pathlib import Path

import httpx
from PIL import Image

from studio.library.base import FetchedImage, FetchError, SourceItem

# A plain desktop-browser User-Agent; some image hosts reject a bare httpx client.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_MIME_BY_FORMAT = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}


class HttpImageFetcher:
    """The one ImageFetcher the studio uses: HTTP for a web address, disk for a local one."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        max_bytes: int = 15_000_000,
        timeout: float = 20.0,
    ) -> None:
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient()
        self.max_bytes = max_bytes
        self.timeout = timeout

    async def fetch(self, item: SourceItem) -> FetchedImage:
        if item.local_path:
            return self._read_local(item.local_path)
        return await self._fetch_remote(item.source_url or "")

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    # ------------------------------------------------------------------ local

    def _read_local(self, local_path: str) -> FetchedImage:
        path = Path(local_path)
        if not path.is_file():
            raise FetchError("File not found.")
        data = path.read_bytes()
        return FetchedImage(data=data, mime_type=self._identify(data))

    # ----------------------------------------------------------------- remote

    async def _fetch_remote(self, url: str) -> FetchedImage:
        try:
            async with self.client.stream(
                "GET",
                url,
                headers={"User-Agent": _USER_AGENT},
                follow_redirects=True,
                timeout=self.timeout,
            ) as response:
                if response.status_code >= 400:
                    raise FetchError(f"The image host returned {response.status_code}.")

                self._reject_if_declared_oversized(response.headers.get("content-length"))

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    self._reject_if_too_large(total)
                    chunks.append(chunk)
                data = b"".join(chunks)
        except (httpx.RequestError, httpx.InvalidURL) as error:
            # A pasted line can look like a web link and still not be one (no host, a broken
            # port); httpx says so before any request goes out.
            if isinstance(error, (httpx.InvalidURL, httpx.UnsupportedProtocol)):
                raise FetchError("The link is not a valid web address.") from error
            raise FetchError("The image host did not respond.") from error

        return FetchedImage(data=data, mime_type=self._identify(data))

    # ---------------------------------------------------------------- shared

    def _reject_if_declared_oversized(self, content_length: str | None) -> None:
        if content_length is None:
            return
        try:
            declared = int(content_length)
        except ValueError:
            return
        self._reject_if_too_large(declared)

    def _reject_if_too_large(self, size: int) -> None:
        if size > self.max_bytes:
            raise FetchError(f"The image is larger than {self._max_bytes_label()}.")

    def _max_bytes_label(self) -> str:
        """`max_bytes` as a reader-friendly limit: megabytes once it's at least one, else bytes."""
        if self.max_bytes >= 1_000_000:
            return f"{self.max_bytes // 1_000_000} MB"
        return f"{self.max_bytes} bytes"

    def _identify(self, data: bytes) -> str:
        try:
            image = Image.open(io.BytesIO(data))
            image.verify()
        except Exception as error:
            raise FetchError("The link did not return an image.") from error

        mime_type = _MIME_BY_FORMAT.get(image.format or "")
        if mime_type is None:
            raise FetchError("The image format is not supported.")
        return mime_type
