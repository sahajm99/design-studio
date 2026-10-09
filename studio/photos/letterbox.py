"""The letterbox trim (v6): flat bands an image model sometimes paints across the top and the
bottom of a photo (Nano Banana framed one Gen 5 photo this way), cut off before the photo is
used, for every provider.

A band is a run of rows, from the edge in, that are each one flat colour and the same colour
as the band's first row. A seamless backdrop has rows like that too, so a band counts as a
letterbox only when the photo starts with a hard edge after it (most of the next rows'
pixels clearly differ from the band), and only when the top and the bottom both have one.
Anything less, and the photo is left as it is.
"""

from __future__ import annotations

from collections.abc import Iterable
from itertools import islice
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

_FLAT = 10  # the most a band's row may vary within itself, in each channel
_SAME = 12  # the most a band's row may differ from the band's first row, in each channel
_EDGE_ROWS = 3  # the rows after a band that must show the hard edge
_EDGE_DIFFERENCE = 24  # how far a pixel must be from the band's colour to differ clearly
_EDGE_SHARE = 0.6  # the share of the edge rows' pixels that must differ clearly
_MIN_BAND_SHARE = 0.01  # a band is at least this share of the height
_MIN_BAND_ROWS = 4  # and at least this many rows
_MAX_TRIM_SHARE = 0.4  # the two bands together are at most this share of the height


def trim_letterbox(path: Path) -> bool:
    """Cut a letterbox off the photo at `path`, in place, saved as a PNG. Returns whether it
    cut one. A file that cannot be read is left alone."""
    try:
        with Image.open(path) as opened:
            image = opened.convert("RGB")
    except OSError:
        return False
    width, height = image.size
    limit = int(height * _MAX_TRIM_SHARE)
    top = _band(image, range(height), limit)
    bottom = _band(image, range(height - 1, -1, -1), limit)
    shortest = max(_MIN_BAND_ROWS, round(height * _MIN_BAND_SHARE))
    if top < shortest or bottom < shortest or top + bottom > limit:
        return False
    image.crop((0, top, width, height - bottom)).save(path, format="PNG")
    return True


def _band(image: Image.Image, rows: Iterable[int], limit: int) -> int:
    """How many rows, from the first of `rows` in, form a flat band with a hard edge after it;
    0 when the first row is not flat, when there is no hard edge, or when the band runs past
    `limit` rows."""
    ordered = iter(rows)
    colour: list[float] | None = None
    count = 0
    edge: list[int] = []
    for y in ordered:
        row = image.crop((0, y, image.width, y + 1))
        flat = all(high - low <= _FLAT for low, high in row.getextrema())
        mean = ImageStat.Stat(row).mean
        if colour is None:
            if not flat:
                return 0
            colour = mean
        if flat and all(abs(a - b) <= _SAME for a, b in zip(mean, colour)):
            count += 1
            if count > limit:
                return 0
            continue
        # The band has ended: this row and the next ones must show the hard edge.
        edge = [y, *islice(ordered, _EDGE_ROWS - 1)]
        break
    if colour is None or not edge:
        return 0
    return count if _hard_edge(image, edge, colour) else 0


def _hard_edge(image: Image.Image, rows: list[int], colour: list[float]) -> bool:
    """Whether most pixels of the rows clearly differ from the band's colour."""
    fill = tuple(round(channel) for channel in colour)
    differing = 0
    for y in rows:
        row = image.crop((0, y, image.width, y + 1))
        difference = ImageChops.difference(row, Image.new("RGB", row.size, fill))
        red, green, blue = difference.split()
        biggest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
        differing += sum(biggest.histogram()[_EDGE_DIFFERENCE + 1 :])
    return differing >= _EDGE_SHARE * image.width * len(rows)
