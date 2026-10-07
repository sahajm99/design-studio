"""The interface every photo source implements."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel


class PhotoUnavailable(Exception):
    """No photo could be produced. The message is shown to the designer as it is,
    so it must be plain language and must never contain a key or token."""


class PhotoResult(BaseModel):
    path: str  # the file that was written
    provider: str  # for the run page, e.g. "cloudflare:flux-2-klein-4b" or "fake"
    prompt: str


class PhotoProvider(Protocol):
    name: str

    async def generate(
        self, prompt: str, width: int, height: int, out_path: Path, *, seed: int | None = None
    ) -> PhotoResult:
        """Write one photo for the prompt to out_path, or raise PhotoUnavailable.

        The photo may come back at any size; the renderer crops it to fit.
        A seed, when the provider supports one, makes repeated calls differ.
        """
        ...
