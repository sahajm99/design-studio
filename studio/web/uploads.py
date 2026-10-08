"""The designer's image uploads: the checks every upload passes, and the brand's shelf.

Every upload is a PNG or a JPEG of at most 15 MB and 40 million pixels that Pillow can
decode, and it is saved under its real extension, never under the name it was sent with.
The quality bar, the library and a session's own photo keep their own routes and only share
the checks. The images the editor places (v5), and the references a session carries, are
kept under the brand in `data/uploads/<brand_id>/<id>.<ext>` and recorded in the store, so
an image uploaded once can be placed again.
"""

from __future__ import annotations

import asyncio
import io

from fastapi import UploadFile
from PIL import Image

from studio.contracts import Upload, UploadSource, new_id
from studio.store import Store

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_UPLOAD_PIXELS = 40_000_000
UPLOAD_REFUSED = "That file is not an image the studio can use."
# The most of an uploaded file's name the picker and the Library show.
MAX_UPLOAD_NAME_CHARS = 80

# The formats an upload may be, as Pillow names them, and the extension each is saved with.
# A phone's multi-picture JPEG opens as "MPO"; it is a JPEG file all the same.
_UPLOAD_EXTENSIONS: dict[str, str] = {"PNG": "png", "JPEG": "jpg", "MPO": "jpg"}
# The EXIF orientations that turn a picture a quarter on screen, which swaps its width and
# height as a browser shows it.
_ORIENTATION_TAG = 0x0112
_QUARTER_TURNS = frozenset({5, 6, 7, 8})


class UploadRejected(ValueError):
    """The file is not an image the studio takes. The message is shown to the designer."""


def upload_extension(data: bytes) -> str | None:
    """The extension an uploaded image is saved with, or None when the studio cannot use it.

    A file over the size limit is refused, and so is anything Pillow does not open as a
    PNG or a JPEG, a picture of more than 40 million pixels, and a file whose data fails
    Pillow's check.
    """
    facts = _image_facts(data)
    return facts[0] if facts is not None else None


async def read_image_upload(file: UploadFile) -> tuple[bytes, str, int, int]:
    """The file's bytes, its real extension ("png" or "jpg"), width and height; raises
    UploadRejected with one of the existing messages for a wrong type, a size over the limit
    or too many pixels."""
    # Reading one byte past the limit is enough to tell a file that is too large.
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    # Its pixels are decoded in full too, so a file cut short is refused instead of drawing
    # garbled; that is blocking work, so it runs off the event loop.
    facts = await asyncio.to_thread(_image_facts, data, decode=True)
    if facts is None:
        raise UploadRejected(UPLOAD_REFUSED)
    extension, width, height = facts
    return data, extension, width, height


def save_brand_upload(
    store: Store, brand_id: str, *, name: str, data: bytes, extension: str, width: int,
    height: int, source: UploadSource,
) -> Upload:
    """Write the file under the brand's uploads folder and record it; returns the Upload."""
    upload_id = new_id()
    path = store.uploads_dir / brand_id / f"{upload_id}.{extension}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    upload = Upload(
        id=upload_id,
        brand_id=brand_id,
        image_path=store.relative(path),
        name=name.strip()[:MAX_UPLOAD_NAME_CHARS],
        width=width,
        height=height,
        source=source,
    )
    store.add_upload(upload)
    return upload


def _image_facts(data: bytes, *, decode: bool = False) -> tuple[str, int, int] | None:
    """The extension an image is saved with, and its width and height as a browser shows it,
    or None when the studio cannot use the file (see `upload_extension`).

    With `decode`, the pixels are also decoded in full, which Pillow's check skips for a
    JPEG, so a file cut short is refused as well. The older upload routes keep the check
    without it.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            extension = _UPLOAD_EXTENSIONS.get(image.format or "")
            width, height = image.size
            turned = _quarter_turned(image)
        if extension is None or width * height > MAX_UPLOAD_PIXELS:
            return None
        # Pillow checks a file's data on an image opened for that alone.
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        if decode:
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except Exception:
        # Pillow raises many kinds of error for a file it cannot read, a decompression bomb
        # among them; each means the same here.
        return None
    # The editor sizes an image's box from these, so they follow the picture as it is shown.
    return (extension, height, width) if turned else (extension, width, height)


def _quarter_turned(image: Image.Image) -> bool:
    """True when the picture's EXIF orientation turns it a quarter on screen. A file whose
    EXIF cannot be read is taken as upright, so the orientation never refuses an upload."""
    try:
        return image.getexif().get(_ORIENTATION_TAG) in _QUARTER_TURNS
    except Exception:
        return False
