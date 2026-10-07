"""Build the configured photo provider from settings."""

from __future__ import annotations

from studio.config import Settings
from studio.photos.base import PhotoProvider
from studio.photos.cloudflare import CloudflarePhotoProvider
from studio.photos.fake import FakePhotoProvider


def get_photo_provider(settings: Settings) -> PhotoProvider | None:
    """The photo provider `settings.photo_mode` selects, or None for 'none'."""
    mode = settings.photo_mode
    if mode == "cloudflare":
        return CloudflarePhotoProvider(
            account_id=settings.cloudflare_account_id,
            api_token=settings.cloudflare_api_token,
            model=settings.cloudflare_image_model,
        )
    if mode == "fake":
        return FakePhotoProvider()
    return None
