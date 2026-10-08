"""The custom layout: the blocks a designer places by hand, and the rules they keep.

The editor arranges a post freely over a photo, and the renderer draws the
arrangement. These pure functions give the editor its starting arrangement (the
default one, or one that follows a template candidate) and hold the guardrails
the renderer applies before it draws: at most twelve blocks, the words and the
logo inside the margin, every shade, image and the photo's box on the canvas,
images only from the uploads folder, the logo always there and never too narrow,
colours only from the palette and faces only from the kit's. The editor's Ask mode
(v5) changes a layout only through `apply_edits`, which applies the editor agent's
edits to a copy before the guardrails run. Nothing here names a brand.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from studio.contracts import (
    Block,
    BrandKit,
    Composition,
    CustomLayout,
    EditorEdit,
    Mode,
    PhotoBox,
    TextAlign,
)
from studio.render.faces import face_named

MARGIN_PCT = 4.0  # the words and the logo stay inside this margin, percent of the canvas
MIN_LOGO_PCT = 14.0  # the logo is never narrower than this, percent of the canvas width
MIN_PHOTO_PCT = 10.0  # the photo's box is never narrower or shorter than this, percent
MIN_IMAGE_PCT = 4.0  # an image block is never narrower or shorter than this, percent
MAX_BLOCKS = 12  # the most blocks a layout keeps; the editor offers no more
HEADLINE_PX = 64  # a text block's size when size_px is 0
SUBLINE_PX = 26
DEFAULT_LOGO_PCT = 24.0

# A text block's size, when one is set, stays within these.
MIN_TEXT_PX = 12
MAX_TEXT_PX = 400
# A text block's weight, when one is set, stays within these.
MIN_WEIGHT = 100
MAX_WEIGHT = 900

MOVED_INSIDE = "The {kind} was moved inside the margin."
LOGO_WIDENED = f"The logo was widened to {MIN_LOGO_PCT:g}% of the canvas."
NOT_IN_PALETTE = "The colour \"{name}\" is not in the palette, so the brand's colour was used."
FONT_NOT_IN_KIT = "The font \"{name}\" is not in the kit, so the brand's typeface was used."
LOGO_ADDED = "The logo was added, since every post carries it."
BLOCKS_LEFT_OUT = f"Only the first {MAX_BLOCKS} blocks were kept."
EMPTY_TEXT_LEFT_OUT = "A text block with no words was left out."
IMAGE_LEFT_OUT = "An image whose file is not among the uploads was left out."
LOGO_FILE_RESET = "The uploaded logo was not found, so the kit's logo was used."
# v5: an Ask mode edit that was dropped, named by its place in the answer, counted from 1.
EDIT_NO_BLOCK = "Edit {n} names a block that is not there."
EDIT_NO_UPLOAD = "Edit {n} names an upload that is not there."
EDIT_KEEPS_BLOCK = "Edit {n} would remove the {kind}, which stays."
EDIT_DOES_NOT_APPLY = "Edit {n} does not apply to the {kind}."
EDIT_NO_ROOM = "Edit {n} adds a block, and the layout holds no more."

# The most of an unknown colour or font name a line repeats.
_MAX_NAME_CHARS = 40

# The margin's far edge, on the right and at the bottom, in percent of the canvas.
_FAR = 100 - MARGIN_PCT
# Sums of percentages carry rounding noise; an edge this close to the margin is on it.
_EPSILON = 1e-6

# Where a template puts its logo, by the two parts of logo_position.
_LOGO_X: dict[str, float] = {"left": 8.0, "centre": 38.0, "right": 68.0}
_LOGO_Y: dict[str, float] = {"top": 6.0, "bottom": 86.0}

# v5: the Ask mode's edits. The kinds no edit removes, the words, the pictures and the kinds
# with a height of their own (the words and the logo take theirs from their width).
_KEPT_KINDS = ("logo", "headline", "subline")
_TEXT_KINDS = ("headline", "subline", "text")
_POST_WORD_KINDS = ("headline", "subline")  # their words are the post's, not the layout's
_PICTURE_KINDS = ("logo", "image")
_OWN_HEIGHT_KINDS = ("shade", "image")
# The fields set_style changes on each kind; any other field it gives is ignored.
_STYLE_FIELDS: dict[str, tuple[str, ...]] = {
    "headline": ("font", "weight", "italic", "size_px", "align", "colour"),
    "subline": ("font", "weight", "italic", "size_px", "align", "colour"),
    "text": ("font", "weight", "italic", "size_px", "align", "colour"),
    "shade": ("colour", "opacity"),
    "image": ("opacity", "fit"),
    "logo": (),
}
# The range each number an edit sets is held to. Nothing is made narrower or shorter than
# 4 percent, the least the editor's handles allow; the guardrails then add their own minimums.
_LEAST_PCT = 4.0
_EDIT_RANGES: dict[str, tuple[float, float]] = {
    "x": (0.0, 100.0),
    "y": (0.0, 100.0),
    "w": (_LEAST_PCT, 100.0),
    "h": (_LEAST_PCT, 100.0),
    "opacity": (0.0, 1.0),
    "offset_x": (-50.0, 50.0),
    "offset_y": (-50.0, 50.0),
}
_MAX_TEXT_CHARS = 200  # a text block's words, as Block allows
# reorder's two directions, as a step through the list: forward is drawn later, over more.
_REORDER_STEPS: dict[str, int] = {"forward": 1, "back": -1}
# Where an added block goes when the edit gives no box, as the editor adds it: text 60 wide
# and centred, its words centred; an image a third wide and centred; a shade across the
# lower part of the canvas.
_NEW_TEXT_W = 60.0
_NEW_TEXT_Y = 46.0
_NEW_IMAGE_W = 100.0 / 3
_NEW_SHADE = {"x": 4.0, "y": 56.0, "w": 92.0, "h": 40.0}


def default_layout() -> CustomLayout:
    """Where the editor starts with no template candidate: words bottom left, logo top left,
    and the photo over the whole canvas."""
    return CustomLayout(blocks=[*_words(66, 80), _logo("top", "left")])


def blocks_for_template(composition: Composition, *, scrim_added: bool = False) -> CustomLayout:
    """A template candidate's arrangement as blocks, for the editor to start from.

    The positions follow the template closely enough for the designer to adjust from
    there. Where the template gives the photo part of the canvas, the photo takes that
    box and the canvas colour around it stands for the template's panel or band. A full
    bleed keeps the photo over the whole canvas; `scrim_added` says the candidate drew a
    shade under its words, so it keeps one. Type only, and any other template, starts
    from the default layout.
    """
    template = composition.template
    if template == "hero":
        return CustomLayout(
            blocks=[*_words(70, 82, align="centre"), _logo("top", "centre")],
            photo=PhotoBox(x=0, y=16, w=100, h=52),
        )
    if template == "full_bleed":
        return _full_bleed(composition, scrim_added=scrim_added)
    if template == "split":
        return _split(composition)
    if template == "corner":
        return CustomLayout(
            blocks=[*_words(9, 24, w=62), _logo_at(6, 88)],
            photo=PhotoBox(x=36, y=44, w=64, h=56),
        )
    if template == "caption_strip":
        return _caption_strip(composition)
    return default_layout()


def apply_guardrails(
    layout: CustomLayout, kit: BrandKit, mode: Mode, *, uploads_root: Path | None = None
) -> tuple[CustomLayout, list[str]]:
    """A copy of the layout with every guardrail applied, and a line for each change it needed.

    In order:
    - a text block whose words are blank is left out;
    - given `uploads_root`, an image block whose file is not in the brand's folder in it
      (`uploads_root/<kit.id>`) is left out, and a logo whose uploaded file is not has the
      kit's file again (its box is kept);
    - at most twelve blocks: those past the twelfth are left out, from the end of the list,
      though never the logo;
    - the words (the headline, the subline and every text block) and the logo inside the
      margin; a shade and an image only on the canvas (without a line);
    - the logo at least 14% wide, and an image at least 4% each way (without a line);
    - colours only from the palette;
    - sizes, weights and opacities in range (without a line): a text size that is set is at
      least 12px;
    - the canvas colour only from the palette;
    - faces only from the kit's, and italics only from a face's italic file (without a line);
    - the photo's box on the canvas and at least 10% each way (without a line);
    - the default logo when the layout has none, in place of the twelfth block if need be.

    The same line is given once. The renderer draws the copy and reports the lines, so what
    is reported is what is drawn. The rules are the same in both modes.
    """
    lines: list[str] = []
    palette = {colour.name for colour in kit.colours}
    blocks = [block for block in layout.blocks if _has_words(block, lines)]
    if uploads_root is not None:
        blocks = _uploads_only(blocks, uploads_root, kit.id, lines)
    blocks = _at_most(blocks, MAX_BLOCKS, lines)
    blocks = [_inside_margin(block, lines) for block in blocks]
    blocks = [_wide_enough(block, lines) for block in blocks]
    blocks = [_palette_colour(block, palette, lines) for block in blocks]
    blocks = [_in_range(block) for block in blocks]
    background = _palette_name(layout.background, palette, lines)
    blocks = [_known_font(block, kit, lines) for block in blocks]
    blocks = [_real_italic(block, kit) for block in blocks]
    photo = _photo_inside(layout.photo)
    if not any(block.kind == "logo" for block in blocks):
        # Every post carries the logo: it goes where the editor starts it, in a place of
        # its own among the twelve.
        _note(lines, LOGO_ADDED)
        blocks = _at_most(blocks, MAX_BLOCKS - 1, lines)
        blocks.append(_logo("top", "left"))
    update = {"blocks": blocks, "background": background, "photo": photo}
    return layout.model_copy(update=update), lines


def upload_file(image_path: str, uploads_root: Path, brand_id: str) -> Path | None:
    """The file an upload's media path names, or None when it names no file in the brand's
    folder of `uploads_root`.

    A media path is relative to the data folder that holds the uploads folder, as the media
    route serves it: "uploads/<brand>/<id>.png". The file must sit in
    `uploads_root/<brand_id>` itself, where the brand's uploads are saved. One that climbs out
    ("../"), an absolute one, one in another folder of the uploads (a session's photo,
    another brand's), or one that names nothing gives None, so a layout never draws, and
    Remove never deletes, a file from anywhere else.
    """
    if not image_path:
        return None
    root = uploads_root.resolve()
    try:
        folder = (uploads_root / brand_id).resolve()
        path = (root.parent / image_path).resolve()
        if path.parent != folder or not path.is_file():
            return None
    except (OSError, ValueError):
        # A name the system cannot look up (a null byte, a loop of links) names no upload.
        return None
    return path


def resolve_colour(name: str, kit: BrandKit, mode: Mode, role: str) -> str:
    """The hex value a colour name draws in.

    A name in the palette gives that colour; any other, and the empty name, gives the
    mode's colour for the role: "headline", "body" or "background".
    """
    if any(colour.name == name for colour in kit.colours):
        return kit.hex(name)
    return kit.mode_hex(mode)[role]


def apply_edits(
    layout: CustomLayout,
    edits: list[EditorEdit],
    *,
    upload_path: Callable[[str], str | None],
    upload_ratio: Callable[[str], float | None] | None = None,
) -> tuple[CustomLayout, list[str]]:
    """A copy of the layout with the editor agent's edits applied in order (v5), and a line
    naming each edit that was dropped.

    An edit names a block by its number in the layout as given, the number the agent's
    context shows, so an earlier edit that deletes or reorders a block never changes which
    block a later one names; a block an edit adds takes the next number. An edit is dropped,
    with a line, when its block is not there, when it would remove the logo, the headline or
    the subline or move one of them in the list, which the editor keeps in place, when it
    adds a block to a layout that holds twelve, when its upload is not there (`upload_path`
    gives an upload's media path, or None), or when it changes nothing its block's kind has
    (words on a shade, a picture on a headline). Reordering past either end of the list
    changes nothing. A field an edit does not use, or that its block's kind lacks, is
    ignored, and every number is held to its field's range, so the copy is always a valid
    layout. set_text on the headline or the subline changes the post's words, which are not
    in the layout: `edited_words` gives them to the caller, and this leaves the layout as it
    is, with no line.

    Added blocks take the box the edit gives, or the editor's: text centred and 60 wide, an
    image a third wide and centred, and a shade across the lower part, behind the words. An
    added image is opaque and keeps its picture's proportions: `upload_ratio` gives an
    upload's height for each unit of its width, both in percent of the canvas; without one,
    a new image is as tall as it is wide in percent. The caller runs the guardrails on the
    copy and reports their lines too.
    """
    blocks = list(enumerate(layout.blocks))  # each block with the number edits know it by
    added = len(blocks)  # the number the next added block takes
    canvas: dict[str, Any] = {}  # what set_photo and set_background change
    lines: list[str] = []
    for n, edit in enumerate(edits, start=1):
        line = ""
        if edit.op in ("set_photo", "set_background"):
            canvas.update(_canvas_changes(edit, layout.model_copy(update=canvas)))
        elif edit.op in ("add_text", "add_image", "add_shade"):
            block = _added_block(edit, upload_path, upload_ratio)
            # On a full list the guardrails would cut the designer's last block instead.
            if len(blocks) >= MAX_BLOCKS:
                line = EDIT_NO_ROOM.format(n=n)
            elif block is None:
                line = EDIT_NO_UPLOAD.format(n=n)
            else:
                place = _behind_words(blocks) if block.kind == "shade" else len(blocks)
                blocks.insert(place, (added, block))
                added += 1
        else:
            numbers = [number for number, _ in blocks]
            if edit.block in numbers:
                line = _edit_block(blocks, numbers.index(edit.block), edit, n, upload_path)
            else:
                line = EDIT_NO_BLOCK.format(n=n)
        if line:
            _note(lines, line)
    update = {"blocks": [block for _, block in blocks], **canvas}
    return layout.model_copy(update=update), lines


def edited_words(
    layout: CustomLayout, edits: list[EditorEdit], words: dict[str, str]
) -> dict[str, str] | None:
    """The post's headline and subline (`words`) after the edits' set_text on their blocks
    (v5), or None when no edit changes them.

    A block is named by its number in the layout as given, as `apply_edits` names it. No edit
    removes the headline or the subline or moves them in the list, and an added block is
    never one, so that number tells which words an edit changes. New words are trimmed and
    cut to what a text block holds.
    """
    changed = dict(words)
    for edit in edits:
        named = edit.block if edit.block is not None else -1
        if edit.op != "set_text" or edit.text is None or not 0 <= named < len(layout.blocks):
            continue
        kind = layout.blocks[named].kind
        if kind in _POST_WORD_KINDS:
            changed[kind] = edit.text.strip()[:_MAX_TEXT_CHARS]
    return changed if changed != words else None


# ------------------------------------------------------------ starting arrangements


def _full_bleed(composition: Composition, *, scrim_added: bool) -> CustomLayout:
    """The words along the top or the bottom, and the candidate's shade if it drew one."""
    row, column = composition.logo_position.split("_")
    if composition.text_position == "bottom":
        headline_y, shade = 66.0, _panel(0, 55, 100, 45, opacity=0.7)
    else:
        headline_y, shade = (20.0 if row == "top" else 8.0), _panel(0, 0, 100, 45, opacity=0.7)
    words = _words(headline_y, headline_y + 14, align=composition.text_align)
    shades = [shade] if scrim_added else []
    return CustomLayout(blocks=[*shades, *words, _logo(row, column)])


def _split(composition: Composition) -> CustomLayout:
    """The photo in its part of the canvas, and the logo and the words on the rest, where
    the canvas colour stands for the template's panel.

    A panel beside the photo is narrow, so its headline starts smaller and its subline lower.
    """
    side = composition.photo_side
    if side == "top":
        return CustomLayout(
            blocks=[*_words(72, 86), _logo_at(8, 62)], photo=PhotoBox(x=0, y=0, w=100, h=58)
        )
    if side == "left":
        words = _words(40, 62, x=59, w=37, headline_px=48)
        return CustomLayout(blocks=[*words, _logo_at(59, 6)], photo=PhotoBox(x=0, y=0, w=55, h=100))
    words = _words(40, 62, x=4, w=37, headline_px=48)
    return CustomLayout(blocks=[*words, _logo_at(4, 6)], photo=PhotoBox(x=45, y=0, w=55, h=100))


def _caption_strip(composition: Composition) -> CustomLayout:
    """The photo above the band, which is the canvas colour, with the logo at one side of
    the band and the headline on the other; no subline."""
    logo_right = composition.logo_position.endswith("_right")
    headline = Block(
        kind="headline", x=8 if logo_right else 36, y=80, w=56, size_px=40, align="left"
    )
    return CustomLayout(
        blocks=[headline, _logo_at(68 if logo_right else 8, 82)],
        photo=PhotoBox(x=0, y=0, w=100, h=76),
    )


def _words(
    headline_y: float,
    subline_y: float,
    *,
    x: float = 8,
    w: float = 84,
    align: TextAlign = "left",
    headline_px: int = 0,
) -> list[Block]:
    """The headline and, under it, the subline, in one column; the headline at the default
    size unless given one."""
    return [
        Block(kind="headline", x=x, y=headline_y, w=w, align=align, size_px=headline_px),
        Block(kind="subline", x=x, y=subline_y, w=w, align=align),
    ]


def _logo(row: str, column: str) -> Block:
    """The logo in a template's corner or at its centre, by the parts of logo_position."""
    return _logo_at(_LOGO_X[column], _LOGO_Y[row])


def _logo_at(x: float, y: float) -> Block:
    return Block(kind="logo", x=x, y=y, w=DEFAULT_LOGO_PCT)


def _panel(x: float, y: float, w: float, h: float, *, opacity: float = 1.0) -> Block:
    """A shade in the brand's background colour, solid unless given a lighter opacity."""
    return Block(kind="shade", x=x, y=y, w=w, h=h, opacity=opacity)


# --------------------------------------------------------------------- guardrails


def _has_words(block: Block, lines: list[str]) -> bool:
    """A text block with blank words is left out, with a line, since it would draw nothing;
    every other block stays."""
    if block.kind != "text" or block.text.strip():
        return True
    _note(lines, EMPTY_TEXT_LEFT_OUT)
    return False


def _uploads_only(
    blocks: list[Block], uploads_root: Path, brand_id: str, lines: list[str]
) -> list[Block]:
    """The blocks whose files are the brand's uploads: an image block whose file is not in
    the brand's uploads folder is left out, and a logo whose uploaded file is not is drawn
    from the kit's file again, in its own box; each with a line."""
    kept: list[Block] = []
    for block in blocks:
        if block.kind == "image" and upload_file(block.image_path, uploads_root, brand_id) is None:
            _note(lines, IMAGE_LEFT_OUT)
            continue
        overridden = block.kind == "logo" and block.image_path
        if overridden and upload_file(block.image_path, uploads_root, brand_id) is None:
            _note(lines, LOGO_FILE_RESET)
            block = block.model_copy(update={"image_path": ""})
        kept.append(block)
    return kept


def _at_most(blocks: list[Block], most: int, lines: list[str]) -> list[Block]:
    """At most `most` blocks: those past it are left out from the end of the list, with a
    line. The first logo always keeps its place, since every post carries it."""
    if len(blocks) <= most:
        return blocks
    _note(lines, BLOCKS_LEFT_OUT)
    logo = next((index for index, block in enumerate(blocks) if block.kind == "logo"), None)
    room = most - (1 if logo is not None else 0)  # the places left beside the logo
    kept: list[Block] = []
    for index, block in enumerate(blocks):
        if index == logo:
            kept.append(block)
        elif room > 0:
            kept.append(block)
            room -= 1
    return kept


def _inside_margin(block: Block, lines: list[str]) -> Block:
    """Rule 1: the words and the logo moved, then shrunk, until they sit inside the margin.

    They take their own height, so only their top edge is held. A shade may run to the
    canvas edge, as a template's panel does, and so may an image, so they are only kept on
    the canvas, silently.
    """
    if block.kind in ("shade", "image"):
        x, w = _span_inside(block.x, block.w, 0.0, 100.0)
        y, h = _span_inside(block.y, block.h, 0.0, 100.0)
        return block.model_copy(update={"x": x, "y": y, "w": w, "h": h})
    x, w = _span_inside(block.x, block.w)
    y = max(block.y, MARGIN_PCT)
    line = MOVED_INSIDE.format(kind=block.kind)
    return _changed(block, {"x": x, "y": y, "w": w}, line, lines)


def _wide_enough(block: Block, lines: list[str]) -> Block:
    """Rule 2: a logo narrower than the minimum is widened to it.

    Shrinking it again would undo that, so a widened logo that now crosses the margin
    moves left instead. An image narrower or shorter than its minimum is widened or
    lengthened to it the same way, moving back from the canvas's right or bottom edge,
    silently, as the photo's box is; so an image whose height was left out still shows.
    """
    if block.kind == "image":
        update: dict[str, float] = {}
        if block.w < MIN_IMAGE_PCT:
            update.update(w=MIN_IMAGE_PCT, x=min(block.x, 100.0 - MIN_IMAGE_PCT))
        if block.h < MIN_IMAGE_PCT:
            update.update(h=MIN_IMAGE_PCT, y=min(block.y, 100.0 - MIN_IMAGE_PCT))
        return block.model_copy(update=update) if update else block
    if block.kind != "logo" or block.w >= MIN_LOGO_PCT:
        return block
    _note(lines, LOGO_WIDENED)
    widened = block.model_copy(update={"w": MIN_LOGO_PCT})
    inside = {"x": min(block.x, _FAR - MIN_LOGO_PCT)}
    return _changed(widened, inside, MOVED_INSIDE.format(kind="logo"), lines)


def _palette_colour(block: Block, palette: set[str], lines: list[str]) -> Block:
    """Rule 3 for a block: a colour the palette does not name becomes the brand's colour."""
    colour = _palette_name(block.colour, palette, lines)
    return block if colour == block.colour else block.model_copy(update={"colour": colour})


def _palette_name(name: str, palette: set[str], lines: list[str]) -> str:
    """The name when it is empty or in the palette; otherwise "", the role's colour, with a line."""
    if not name or name in palette:
        return name
    _note(lines, NOT_IN_PALETTE.format(name=_shown(name)))
    return ""


def _known_font(block: Block, kit: BrandKit, lines: list[str]) -> Block:
    """A face the kit does not offer becomes "", the kit's family, with a line."""
    if not block.font or face_named(kit, block.font) is not None:
        return block
    _note(lines, FONT_NOT_IN_KIT.format(name=_shown(block.font)))
    return block.model_copy(update={"font": ""})


def _real_italic(block: Block, kit: BrandKit) -> Block:
    """Italics turned off, silently, when the block's face has no italic file, rather than
    left to a slant the browser fakes from the upright file."""
    if not block.italic:
        return block
    face = face_named(kit, block.font)
    if face is not None and face.italic is not None:
        return block
    return block.model_copy(update={"italic": False})


def _shown(name: str) -> str:
    """A name as a line repeats it: in full, or its first 40 characters and "…" when longer."""
    return name if len(name) <= _MAX_NAME_CHARS else f"{name[:_MAX_NAME_CHARS]}…"


def _in_range(block: Block) -> Block:
    """Rule 4: a size and a weight that are set kept within their ranges, and the opacity
    within 0 to 1."""
    size_px = min(max(block.size_px, MIN_TEXT_PX), MAX_TEXT_PX) if block.size_px else 0
    weight = min(max(block.weight, MIN_WEIGHT), MAX_WEIGHT) if block.weight else 0
    opacity = min(max(block.opacity, 0.0), 1.0)
    if (size_px, weight, opacity) == (block.size_px, block.weight, block.opacity):
        return block
    return block.model_copy(update={"size_px": size_px, "weight": weight, "opacity": opacity})


def _photo_inside(box: PhotoBox | None) -> PhotoBox | None:
    """The photo's box kept on the canvas and at least 10% each way, silently.

    Like a shade, it is moved, then shrunk, until it is on the canvas; one made too small
    is widened or lengthened, moving back from the far edge to make room. None, the whole
    canvas, stays as it is.
    """
    if box is None:
        return None
    x, w = _span_inside(box.x, box.w, 0.0, 100.0)
    y, h = _span_inside(box.y, box.h, 0.0, 100.0)
    if w < MIN_PHOTO_PCT:
        x, w = min(x, 100.0 - MIN_PHOTO_PCT), MIN_PHOTO_PCT
    if h < MIN_PHOTO_PCT:
        y, h = min(y, 100.0 - MIN_PHOTO_PCT), MIN_PHOTO_PCT
    if (x, y, w, h) == (box.x, box.y, box.w, box.h):
        return box
    return box.model_copy(update={"x": x, "y": y, "w": w, "h": h})


def _span_inside(
    start: float, length: float, low: float = MARGIN_PCT, high: float = _FAR
) -> tuple[float, float]:
    """A start and a length on one side of the canvas, moved then shrunk to sit between
    `low` and `high`, the margin's edges unless told otherwise.

    The near edge moves in to `low`, then the length shrinks until the far edge is
    inside too. A span that starts at or past `high` cannot shrink inside, so it moves
    back in, no longer than the room between the two.
    """
    start = max(start, low)
    if start + length <= high + _EPSILON:
        return start, length
    if start < high:
        return start, high - start
    length = min(length, high - low)
    return high - length, length


def _changed(block: Block, updates: dict[str, float], line: str, lines: list[str]) -> Block:
    """The block with the updates, and the line noted, when any of them changes it."""
    if all(getattr(block, field) == value for field, value in updates.items()):
        return block
    _note(lines, line)
    return block.model_copy(update=updates)


def _note(lines: list[str], line: str) -> None:
    """Add the line, once."""
    if line not in lines:
        lines.append(line)


# ------------------------------------------------------------- the Ask mode's edits (v5)


def _edit_block(
    blocks: list[tuple[int, Block]],
    position: int,
    edit: EditorEdit,
    n: int,
    upload_path: Callable[[str], str | None],
) -> str:
    """Apply one edit to the block at `position` in the list, in place, and give back the line
    saying why it was dropped, or "" when it was applied."""
    number, block = blocks[position]
    kind = block.kind
    if edit.op == "delete":
        if kind in _KEPT_KINDS:
            return EDIT_KEEPS_BLOCK.format(n=n, kind=kind)
        del blocks[position]
        return ""
    if edit.op == "reorder":
        # The editor keeps the logo, the headline and the subline in place: a logo sent
        # back could end up hidden under a shade.
        if kind in _KEPT_KINDS:
            return EDIT_DOES_NOT_APPLY.format(n=n, kind=kind)
        other = position + _REORDER_STEPS.get(edit.direction or "", 0)
        # At either end of the list there is nowhere to go, which is not an error.
        if other != position and 0 <= other < len(blocks):
            blocks[position], blocks[other] = blocks[other], blocks[position]
        return ""
    changes: dict[str, Any] = {}
    if edit.op == "move":
        changes = _held_fields(edit, ("x", "y"))
    elif edit.op == "resize":
        changes = _resized(block, edit)
    elif edit.op == "set_text":
        # The headline's and the subline's words are the post's, not the layout's:
        # `edited_words` gives them to the caller, so the layout is left as it is here.
        if kind in _POST_WORD_KINDS:
            return ""
        if kind != "text":
            return EDIT_DOES_NOT_APPLY.format(n=n, kind=kind)
        changes = _held_fields(edit, ("text",))
    elif edit.op == "set_style":
        changes = _style_changes(kind, edit)
        if not changes:
            return EDIT_DOES_NOT_APPLY.format(n=n, kind=kind)
    elif edit.op == "replace_image":
        if kind not in _PICTURE_KINDS:
            return EDIT_DOES_NOT_APPLY.format(n=n, kind=kind)
        # On the logo the upload stands in for the kit's file, in the same box.
        path = upload_path(edit.upload_id or "")
        if path is None:
            return EDIT_NO_UPLOAD.format(n=n)
        changes = {"image_path": path}
    blocks[position] = (number, block.model_copy(update=changes))
    return ""


def _added_block(
    edit: EditorEdit,
    upload_path: Callable[[str], str | None],
    upload_ratio: Callable[[str], float | None] | None,
) -> Block | None:
    """The block an add_text, add_image or add_shade edit adds, from the box and the style it
    gives and the editor's defaults for the rest; None for an image whose upload is not there.

    An image's height follows its picture (see `apply_edits`), so the edit's own height is
    not used; one too tall for the canvas is made narrower instead, as the editor does.
    """
    given = _held_fields(edit, ("x", "y", "w", "h"))
    if edit.op == "add_text":
        w = given.get("w", _NEW_TEXT_W)
        style = {"align": "centre", **_style_changes("text", edit)}
        text = _held_fields(edit, ("text",)).get("text", "")
        x, y = given.get("x", (100.0 - w) / 2), given.get("y", _NEW_TEXT_Y)
        return Block(kind="text", x=x, y=y, w=w, text=text, **style)
    if edit.op == "add_shade":
        box = {**_NEW_SHADE, **given}
        return Block(kind="shade", **box, **_style_changes("shade", edit))
    upload_id = edit.upload_id or ""
    path = upload_path(upload_id)
    if path is None:
        return None
    ratio = upload_ratio(upload_id) if upload_ratio is not None else None
    w = given.get("w", _NEW_IMAGE_W)
    h = w * ratio if ratio else w
    if h > 100.0:
        w, h = max(_LEAST_PCT, w * 100.0 / h), 100.0
    h = max(h, _LEAST_PCT)
    x, y = given.get("x", (100.0 - w) / 2), given.get("y", (100.0 - h) / 2)
    opacity = _held_fields(edit, ("opacity",)).get("opacity", 1.0)
    fit = edit.fit or "contain"
    return Block(kind="image", x=x, y=y, w=w, h=h, image_path=path, fit=fit, opacity=opacity)


def _behind_words(blocks: list[tuple[int, Block]]) -> int:
    """Where a new shade goes in the list: just before the first of the words and the logo,
    so it is drawn behind them, as the editor adds one; at the end when there are none."""
    in_front = (*_TEXT_KINDS, "logo")
    places = (place for place, (_, block) in enumerate(blocks) if block.kind in in_front)
    return next(places, len(blocks))


def _resized(block: Block, edit: EditorEdit) -> dict[str, float]:
    """A resize's new width and, for a shade or an image, height; the words and the logo take
    their height from their width. An image that keeps its aspect scales both sides by one
    factor, the width's when the edit gives one, held so that the box fits the canvas and is
    at least the least size each way, as the editor's handles keep it."""
    changes = _held_fields(edit, ("w", "h") if block.kind in _OWN_HEIGHT_KINDS else ("w",))
    if block.kind != "image" or not block.keep_aspect or not changes or not block.h:
        return changes
    scale = changes["w"] / block.w if "w" in changes else changes["h"] / block.h
    low = max(_LEAST_PCT / block.w, _LEAST_PCT / block.h)
    high = min(100.0 / block.w, 100.0 / block.h)
    scale = min(max(scale, min(low, high)), high)
    return {"w": block.w * scale, "h": block.h * scale}


def _style_changes(kind: str, edit: EditorEdit) -> dict[str, Any]:
    """The style fields the edit sets that a block of this kind has, each held to its range."""
    return _held_fields(edit, _STYLE_FIELDS.get(kind, ()))


def _canvas_changes(edit: EditorEdit, layout: CustomLayout) -> dict[str, Any]:
    """What set_photo or set_background changes on the layout: the photo's fit, offsets and
    box, or the canvas colour (a palette name; "" is the mode's, and the guardrails turn any
    other name into it). The photo's box takes the sides the edit gives over its box now,
    the whole canvas when it has none, at least the photo's least size each way."""
    if edit.op == "set_background":
        colour = edit.background if edit.background is not None else edit.colour
        return {} if colour is None else {"background": colour}
    changes: dict[str, Any] = {}
    if edit.fit is not None:
        changes["photo_fit"] = edit.fit
    offsets = _held_fields(edit, ("offset_x", "offset_y"))
    changes.update({f"photo_{field}": value for field, value in offsets.items()})
    sides = _held_fields(edit, ("x", "y", "w", "h"))
    for side in ("w", "h"):
        if side in sides:
            sides[side] = max(sides[side], MIN_PHOTO_PCT)
    if sides:
        changes["photo"] = (layout.photo or PhotoBox()).model_copy(update=sides)
    return changes


def _held_fields(edit: EditorEdit, fields: tuple[str, ...]) -> dict[str, Any]:
    """The fields the edit gives, among `fields`, each held to the range its field keeps: a
    place in percent, an opacity, an offset, a size or a weight that is set (0 keeps the
    role's own), and words cut to what a text block holds. A field left None is left out."""
    held: dict[str, Any] = {}
    for field in fields:
        value = getattr(edit, field)
        if value is None:
            continue
        if field in _EDIT_RANGES:
            low, high = _EDIT_RANGES[field]
            value = min(max(float(value), low), high)
        elif field == "size_px":
            value = round(min(max(value, MIN_TEXT_PX), MAX_TEXT_PX)) if value else 0
        elif field == "weight":
            value = round(min(max(value, MIN_WEIGHT), MAX_WEIGHT)) if value else 0
        elif field == "text":
            value = value[:_MAX_TEXT_CHARS]
        held[field] = value
    return held
