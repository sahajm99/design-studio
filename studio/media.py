"""What the models are sent of the designer's uploads: an agent copy beside each one.

An upload can be 15 MB, and the models see the designer's pictures often: each reference in
several calls of a session, and an Ask's file beside the layout's own picture. So the file a
model is sent is an agent copy, turned upright and kept to 2048 pixels on its longer side, as
the quality bar is prepared, written beside the upload as `uploads/<brand>/<id>.agent.jpg`
(`.agent.png` for a picture with transparency). The shelf, the pages and the editor keep the
original, and removing an upload removes its copies too. The module sits beside the store,
so the web layer and the workflows share it without either reaching into the other.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path, PurePosixPath

from PIL import Image, ImageOps

from studio.store import Store

logger = logging.getLogger(__name__)

# An agent copy is kept to this many pixels on its longer side, as the quality bar is.
MAX_AGENT_SIDE = 2048
_AGENT_JPEG_QUALITY = 85


def agent_copy_path(image_path: str, *, transparent: bool = False) -> str:
    """The media path of an upload's agent copy, beside it: `uploads/<brand>/<id>.agent.jpg`,
    or `.agent.png` for a picture with transparency. It is derived from the upload's own path
    and never stored."""
    path = PurePosixPath(image_path)
    extension = "png" if transparent else "jpg"
    return str(path.with_name(f"{path.stem}.agent.{extension}"))


def write_agent_copy(store: Store, image_path: str, data: bytes) -> str:
    """Write the agent copy of the upload at `image_path`, whose bytes are `data`, and give back
    its media path. It is turned upright and kept to 2048 pixels on its longer side, as the
    quality bar is prepared: a JPEG, or a PNG when the picture shows transparency.

    Decoding is blocking work, so callers in a request run this off the event loop. Pillow's
    errors are passed on.
    """
    with Image.open(io.BytesIO(data)) as image:
        picture = ImageOps.exif_transpose(image)
    picture.thumbnail((MAX_AGENT_SIDE, MAX_AGENT_SIDE))
    has_alpha = picture.mode in ("RGBA", "LA", "PA") or "transparency" in picture.info
    with_alpha = picture.convert("RGBA") if has_alpha else None
    # Many PNGs carry an alpha channel that is opaque throughout; those are photos, kept small.
    transparent = with_alpha is not None and with_alpha.getchannel("A").getextrema()[0] < 255
    relative = agent_copy_path(image_path, transparent=transparent)
    path = store.media_path(relative)
    if transparent:
        with_alpha.save(path, format="PNG")
    else:
        picture.convert("RGB").save(path, format="JPEG", quality=_AGENT_JPEG_QUALITY)
    return relative


def agent_picture(store: Store, image_path: str) -> Path:
    """The file the models are sent for the upload at `image_path`: its agent copy when it has
    one, else a copy written now from the upload, else, when none can be written, the upload
    itself.

    Writing a copy decodes the picture, which is blocking work, so callers in a request run
    this off the event loop. It never fails for want of a copy: a copy that cannot be written
    is logged by its error's type and costs the models only a smaller picture.
    """
    for transparent in (False, True):
        copy = _file_at(store, agent_copy_path(image_path, transparent=transparent))
        if copy is not None:
            return copy
    original = store.media_path(image_path)
    try:
        return store.media_path(write_agent_copy(store, image_path, original.read_bytes()))
    except Exception as error:
        # Pillow's errors and the disk's alike; only the type, as every error is logged here.
        logger.warning("The agent copy of an upload was not written: %s", type(error).__name__)
        return original


def _file_at(store: Store, relative: str) -> Path | None:
    """The file at a media path in the data folder, or None when there is none there."""
    try:
        path = store.media_path(relative)
    except ValueError:
        return None
    return path if path.is_file() else None
