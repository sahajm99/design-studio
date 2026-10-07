"""The paced background job that asks a model to describe each pending reference."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from studio.contracts import Reference, StyleCard
from studio.store import Store

Analyse = Callable[[Reference, bytes, str], Awaitable[StyleCard]]

_MISSING_IMAGE_ERROR = "The cached image is missing."

_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class _ImageMissing(Exception):
    """Internal signal: the reference's cached image file is not on disk."""


class AnalysisProgress(BaseModel):
    total: int
    analysed: int
    pending: int
    failed: int
    running: bool


class AnalysisJob:
    """Describes pending references one at a time, no faster than `per_minute`."""

    def __init__(
        self,
        store: Store,
        analyse: Analyse,
        *,
        per_minute: int = 10,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._analyse = analyse
        self._per_minute = per_minute
        self._sleep = sleep
        self._task: asyncio.Task[AnalysisProgress] | None = None

    def start(self) -> bool:
        """Schedule run_once() in the background; False if one is already running."""
        if self._task is not None and not self._task.done():
            return False
        self._task = asyncio.create_task(self.run_once())
        return True

    async def run_once(self) -> AnalysisProgress:
        interval = 60 / self._per_minute
        is_first_call = True

        while True:
            pending = self._store.list_references(status="pending")
            if not pending:
                break
            for ref in pending:
                if not is_first_call:
                    await self._sleep(interval)
                is_first_call = False
                await self._analyse_one(ref)

        return self.progress()

    def progress(self) -> AnalysisProgress:
        pending = len(self._store.list_references(status="pending"))
        analysed = len(self._store.list_references(status="analysed"))
        failed = len(self._store.list_references(status="failed"))
        return AnalysisProgress(
            total=pending + analysed + failed,
            analysed=analysed,
            pending=pending,
            failed=failed,
            running=self._task is not None and not self._task.done(),
        )

    async def _analyse_one(self, ref: Reference) -> None:
        try:
            data, mime_type = self._read_image(ref)
            card = await self._analyse(ref, data, mime_type)
        except _ImageMissing:
            self._store.set_reference_status(ref.id, "failed", _MISSING_IMAGE_ERROR)
            return
        except Exception as error:
            self._store.set_reference_status(ref.id, "failed", str(error)[:300])
            return
        self._store.set_style_card(ref.id, card)

    def _read_image(self, ref: Reference) -> tuple[bytes, str]:
        if ref.image_path is None:
            raise _ImageMissing()
        path = self._store.media_path(ref.image_path)
        if not path.is_file():
            raise _ImageMissing()
        mime_type = _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")
        return path.read_bytes(), mime_type
