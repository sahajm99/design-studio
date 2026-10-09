"""Where photos come from.

v6: the app asks `studio.photos.registry.PhotoRegistry` for a model's adapter; the models are
data in `catalogue.yaml`. `get_photo_provider`, v5's one-provider choice, stays for callers
that want the single provider the settings name.
"""

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
