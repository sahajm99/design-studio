"""The custom layout: the blocks a designer places by hand, and the rules they keep.

The editor arranges a post freely over a photo, and the renderer draws the
arrangement. These pure functions give the editor its starting arrangement (the
default one, or one that follows a template candidate) and hold the guardrails
the renderer applies before it draws: the words and the logo inside the margin,
every shade and the photo's box on the canvas, the logo always there and never
too narrow, colours only from the palette and faces only from the kit's. Nothing
here names a brand.
"""

from __future__ import annotations

from studio.contracts import Block, BrandKit, Composition, CustomLayout, Mode, PhotoBox, TextAlign
from studio.render.faces import face_named

MARGIN_PCT = 4.0  # the words and the logo stay inside this margin, percent of the canvas
MIN_LOGO_PCT = 14.0  # the logo is never narrower than this, percent of the canvas width
MIN_PHOTO_PCT = 10.0  # the photo's box is never narrower or shorter than this, percent
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

# The most of an unknown colour or font name a line repeats.
_MAX_NAME_CHARS = 40

# The margin's far edge, on the right and at the bottom, in percent of the canvas.
_FAR = 100 - MARGIN_PCT
# Sums of percentages carry rounding noise; an edge this close to the margin is on it.
_EPSILON = 1e-6

# Where a template puts its logo, by the two parts of logo_position.
_LOGO_X: dict[str, float] = {"left": 8.0, "centre": 38.0, "right": 68.0}
_LOGO_Y: dict[str, float] = {"top": 6.0, "bottom": 86.0}


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
    layout: CustomLayout, kit: BrandKit, mode: Mode
) -> tuple[CustomLayout, list[str]]:
    """A copy of the layout with every guardrail applied, and a line for each change it needed.

    In order: the words and the logo inside the margin (a shade only on the canvas, without
    a line), the logo at least 14% wide, colours only from the palette, faces only from the
    kit's, italics only from a face's italic file (without a line), sizes, weights and
    opacities in range (without a line), the photo's box on the canvas and at least 10% each
    way (without a line), and the default logo when the layout has none. The same line is
    given once. The renderer draws the copy and reports the lines, so what is reported is
    what is drawn. The rules are the same in both modes.
    """
    lines: list[str] = []
    palette = {colour.name for colour in kit.colours}
    blocks = [_inside_margin(block, lines) for block in layout.blocks]
    blocks = [_wide_enough(block, lines) for block in blocks]
    blocks = [_palette_colour(block, palette, lines) for block in blocks]
    blocks = [_in_range(block) for block in blocks]
    background = _palette_name(layout.background, palette, lines)
    blocks = [_known_font(block, kit, lines) for block in blocks]
    blocks = [_real_italic(block, kit) for block in blocks]
    photo = _photo_inside(layout.photo)
    if not any(block.kind == "logo" for block in blocks):
        # Every post carries the logo: it goes where the editor starts it.
        _note(lines, LOGO_ADDED)
        blocks.append(_logo("top", "left"))
    update = {"blocks": blocks, "background": background, "photo": photo}
    return layout.model_copy(update=update), lines


def resolve_colour(name: str, kit: BrandKit, mode: Mode, role: str) -> str:
    """The hex value a colour name draws in.

    A name in the palette gives that colour; any other, and the empty name, gives the
    mode's colour for the role: "headline", "body" or "background".
    """
    if any(colour.name == name for colour in kit.colours):
        return kit.hex(name)
    return kit.mode_hex(mode)[role]


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


def _inside_margin(block: Block, lines: list[str]) -> Block:
    """Rule 1: the words and the logo moved, then shrunk, until they sit inside the margin.

    They take their own height, so only their top edge is held. A shade may run to the
    canvas edge, as a template's panel does, so it is only kept on the canvas, silently.
    """
    if block.kind == "shade":
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
    moves left instead.
    """
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
