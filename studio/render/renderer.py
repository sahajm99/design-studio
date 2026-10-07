"""Turn a design spec and a brand kit into the finished post image.

Layouts are HTML and CSS templates that headless Chromium captures at the kit's
post size. Every brand value reaches a page as a CSS custom property and every
composition parameter as a class on the page, so no layout names a brand. Each
page measures its own text, and the renderer steps the headline down until the
words fit. Where the words sit over the photo, the renderer measures the photo
behind them and adds a shade under them when they would be hard to read.

The editor's custom layout is drawn as the designer placed it: its blocks arrive
as inline places, sizes, colours and faces, and its photo in a box of its own,
held inside the guardrails first. Nothing steps down and no shade is added; words
that do not fit their box, a block past the margin, and words that may be hard to
read over the photo are reported instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup
from PIL import Image, ImageOps
from playwright.async_api import Browser, Page, Playwright, async_playwright

from studio.brand import logo_for_mode
from studio.contracts import (
    TEXT_OVER_PHOTO,
    Block,
    BrandKit,
    Composition,
    CustomLayout,
    DesignSpec,
    LayoutId,
    Mode,
    PostSize,
    RenderReport,
)
from studio.render.compose import NEEDS_PHOTO, composition_for
from studio.render.custom import (
    HEADLINE_PX,
    MARGIN_PCT,
    SUBLINE_PX,
    apply_guardrails,
    resolve_colour,
)
from studio.render.faces import Face, face_named

LAYOUTS_DIR = Path(__file__).parent / "layouts"

# The headline starts at its layout's size and steps down until the text fits.
START_PX: dict[LayoutId, int] = {
    "hero": 64,
    "type_only": 96,
    "full_bleed": 60,
    "split": 56,
    "corner": 72,
    "caption_strip": 40,
    "custom": HEADLINE_PX,
}
STEP_PX = 4
MIN_PX = 44
# The caption strip's band holds at most two short lines, so its headline may go smaller.
# A custom layout's blocks carry their own sizes, so it starts and stops at one size.
MIN_PX_FOR: dict[LayoutId, int] = {"caption_strip": 36, "custom": HEADLINE_PX}

DOES_NOT_FIT = "Text does not fit at the smallest size. Shorten the copy."
# A custom layout's sizes are the designer's, so its words are reported, never stepped down.
CUSTOM_DOES_NOT_FIT = "Some words do not fit their box. Open the editor and make room."
CUSTOM_NEEDS_BLOCKS = "The custom layout needs its blocks."
CUSTOM_LOW_CONTRAST = (
    "The {kind} may be hard to read over the photo (contrast {ratio}). Add a shade."
)
CUSTOM_UNMEASURED = "The photo could not be measured for contrast."
# What the custom fit script reports for a block, in words.
_CUSTOM_FIT_LINES = {
    "box": "The {kind} overflows its box.",
    "margin": "The {kind} runs past the margin.",
}

# Words over a photo need at least this WCAG contrast ratio, or they get a shade.
MIN_CONTRAST = 4.5
SHADE_ADDED = "A shade was added behind the words for contrast."
SHADE_UNMEASURED = "The photo could not be measured, so a shade was added."
# The photo region behind the words is reduced to at most this size before it is measured.
SAMPLE_PX = 64

# Grayscale, unhinted text: subpixel smoothing leaves colour fringes in a finished
# image, and hinting bends the typeface's shapes to the pixel grid.
CHROMIUM_ARGS = ["--disable-lcd-text", "--font-render-hinting=none"]

# Defines window.studioFit(). The text fits when no words spill out of the text
# box (past the side margins, or out of a fixed text area) and the text block
# stays inside the safe area, clear of the bottom margin, the photo (unless the
# template lays the photo under the words) and the logo's clear space (half the
# logo's height). It also reports where the words are, which parts are shown
# and the box the photo is drawn in, for the contrast check.
FIT_SCRIPT = """
(() => {
  const overlaps = (a, b) =>
    a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

  // Where the words really are, which can be wider than their box.
  const wordsBox = (element) => {
    const range = document.createRange();
    range.selectNodeContents(element);
    return range.getBoundingClientRect();
  };

  const plain = (box) => ({ left: box.left, top: box.top, right: box.right, bottom: box.bottom });

  window.studioFit = () => {
    const overflow = [];
    const area = document.querySelector(".text").getBoundingClientRect();
    const parts = [...document.querySelectorAll(".headline, .subline")];
    const shown = [];
    let words = null;
    for (const part of parts) {
      if (!part.textContent.trim()) continue;
      const box = wordsBox(part);
      if (box.left < area.left - 1 || box.right > area.right + 1) {
        overflow.push(part.className);
      }
      shown.push(part.className);
      words = words === null ? plain(box) : {
        left: Math.min(words.left, box.left),
        top: Math.min(words.top, box.top),
        right: Math.max(words.right, box.right),
        bottom: Math.max(words.bottom, box.bottom),
      };
    }

    const boxes = parts.map((part) => part.getBoundingClientRect());
    const block = {
      left: area.left,
      right: area.right,
      top: Math.min(...boxes.map((box) => box.top)),
      bottom: Math.max(...boxes.map((box) => box.bottom)),
    };
    if (block.top < area.top - 1 || block.bottom > area.bottom + 1) overflow.push("text box");

    const page = getComputedStyle(document.body);
    if (block.top < parseFloat(page.paddingTop)) overflow.push("top margin");
    if (block.bottom > innerHeight - parseFloat(page.paddingBottom)) {
      overflow.push("bottom margin");
    }

    const photo = document.querySelector(".photo");
    if (photo && !photo.classList.contains("under-words") &&
        overlaps(block, photo.getBoundingClientRect())) {
      overflow.push("photo");
    }

    const logo = document.querySelector(".logo").getBoundingClientRect();
    const clear = logo.height / 2;
    const logoSpace = {
      left: logo.left - clear,
      right: logo.right + clear,
      top: logo.top - clear,
      bottom: logo.bottom + clear,
    };
    if (overlaps(block, logoSpace)) overflow.push("logo");

    const image = document.querySelector(".photo img");
    return {
      fits: overflow.length === 0,
      overflow,
      words,
      shown,
      photo: image ? plain(image.getBoundingClientRect()) : null,
    };
  };
})();
"""

# Defines window.studioFit() for a custom layout, in the same shape as above. Each
# text block is judged on its own: its words must not spill past its sides ("headline
# box"), and its box must not pass the margin's far edges ("headline margin"), nor may
# a logo's ("logo margin"). It also reports where the words are, each text block's
# words on their own (`parts`, in page order), which blocks are shown and the box the
# photo is drawn in, for the contrast check.
CUSTOM_FIT_SCRIPT = """
(() => {
  // Where the words really are, which can be wider than their box.
  const wordsBox = (element) => {
    const range = document.createRange();
    range.selectNodeContents(element);
    return range.getBoundingClientRect();
  };

  const plain = (box) => ({ left: box.left, top: box.top, right: box.right, bottom: box.bottom });

  window.studioFit = () => {
    const overflow = [];
    const shown = [];
    const parts = [];
    let words = null;
    // The margin's far edges, as a share of the canvas.
    const right = innerWidth * FAR_EDGE;
    const bottom = innerHeight * FAR_EDGE;
    const pastMargin = (box) => box.bottom > bottom + 1 || box.right > right + 1;
    for (const part of document.querySelectorAll(".headline, .subline")) {
      if (!part.textContent.trim()) continue;
      const kind = part.classList.contains("headline") ? "headline" : "subline";
      const block = part.getBoundingClientRect();
      const box = wordsBox(part);
      if (box.left < block.left - 1 || box.right > block.right + 1) {
        overflow.push(`${kind} box`);
      }
      if (pastMargin(block)) overflow.push(`${kind} margin`);
      shown.push(kind);
      parts.push({ kind, box: plain(box) });
      words = words === null ? plain(box) : {
        left: Math.min(words.left, box.left),
        top: Math.min(words.top, box.top),
        right: Math.max(words.right, box.right),
        bottom: Math.max(words.bottom, box.bottom),
      };
    }
    for (const logo of document.querySelectorAll(".logo")) {
      if (pastMargin(logo.getBoundingClientRect())) overflow.push("logo margin");
    }

    const image = document.querySelector(".photo img");
    return {
      fits: overflow.length === 0,
      overflow,
      words,
      shown,
      parts,
      photo: image ? plain(image.getBoundingClientRect()) : null,
    };
  };
})();
""".replace("FAR_EDGE", f"{(100 - MARGIN_PCT) / 100:g}")

# Loads every face the page declares, in each style, and waits for them, then names
# anything that failed to load.
READY_SCRIPT = """
async () => {
  const faces = [...document.fonts];
  await Promise.allSettled(faces.map((face) => face.load()));
  await document.fonts.ready;
  const missing = [];
  if (faces.length === 0 || faces.some((face) => face.status !== "loaded")) {
    missing.push("font");
  }
  for (const image of document.images) {
    if (!image.complete || image.naturalWidth === 0) {
      missing.push(image.classList.contains("logo") ? "logo" : "photo");
    }
  }
  return missing;
}
"""

RESIZE_SCRIPT = """
(px) => {
  document.documentElement.style.setProperty("--headline-size", `${px}px`);
  return window.studioFit();
}
"""

# Which brand colour each shown part of the words is set in.
_PART_COLOURS = {"headline": "headline", "subline": "body"}

# A custom layout's block colours: which role a block takes when its colour is the brand's.
_BLOCK_ROLES = {"headline": "headline", "subline": "body", "shade": "background"}
# The page draws the shades over the photo, the words over them and the logo on top.
_DRAWING_ORDER = {"shade": 0, "headline": 1, "subline": 1, "logo": 2}

_templates = Environment(
    loader=FileSystemLoader(LAYOUTS_DIR),
    autoescape=True,
    undefined=StrictUndefined,
)


def build_html(
    spec: DesignSpec,
    kit: BrandKit,
    *,
    logo_url: str,
    photo_url: str | None,
    font_url: str,
    italic_font_url: str | None,
    headline_px: int,
    scrim: bool = False,
) -> str:
    """The page for one post: its layout filled with the words, the brand's values and the composition.

    `scrim` draws the shade under words that sit over the photo. The renderer
    decides when a page needs it. A custom layout is drawn from its blocks, held
    inside the guardrails, with its own fit script; `scrim` does not apply to it.
    Its page also declares each further face its words are set in, and no other.
    """
    if spec.layout in NEEDS_PHOTO and photo_url is None:
        raise ValueError(_needs_photo(spec.layout))
    faces: list[Face] = []
    if spec.layout == "custom":
        layout, _ = apply_guardrails(_custom_layout(spec), kit, spec.mode)
        drawn = _drawn_blocks(layout, spec)
        faces = _extra_faces(drawn, kit)
        layout_values = {
            "fit_script": Markup(CUSTOM_FIT_SCRIPT),
            **_custom_page(layout, drawn, kit, spec.mode),
        }
    else:
        layout_values = {
            "fit_script": Markup(FIT_SCRIPT),
            "page_classes": _page_classes(spec.layout, composition_for(spec), scrim=scrim),
        }
    template = _templates.get_template(f"{spec.layout}.html")
    return template.render(
        brand_style=_brand_style(
            kit,
            spec.mode,
            font_url=font_url,
            italic_font_url=italic_font_url,
            headline_px=headline_px,
            faces=faces,
        ),
        logo_url=logo_url,
        photo_url=photo_url,
        headline=spec.headline,
        subline=spec.subline.strip(),
        **layout_values,
    )


class _Box(NamedTuple):
    """A rectangle on the page, in CSS pixels."""

    left: float
    top: float
    right: float
    bottom: float

    @property
    def width(self) -> float:
        return max(self.right - self.left, 0.0)

    @property
    def height(self) -> float:
        return max(self.bottom - self.top, 0.0)

    @property
    def area(self) -> float:
        return self.width * self.height

    def overlap(self, other: _Box) -> _Box:
        return _Box(
            max(self.left, other.left),
            max(self.top, other.top),
            min(self.right, other.right),
            min(self.bottom, other.bottom),
        )

    @classmethod
    def read(cls, raw: dict[str, float] | None) -> _Box | None:
        if not raw:
            return None
        return cls(raw["left"], raw["top"], raw["right"], raw["bottom"])


class _Shade(NamedTuple):
    """A shade a custom layout draws over the photo: its box in CSS pixels, colour and opacity."""

    box: _Box
    colour: str
    opacity: float


@dataclass(frozen=True)
class _Capture:
    """What one render of a page measured: the headline size it settled on and where things are."""

    headline_px: int
    fits: bool
    words: _Box | None  # None when the page shows no words
    shown: tuple[str, ...]  # the parts of the words on the page: headline, subline
    photo: _Box | None  # the box the photo is drawn in, if any
    overflow: tuple[str, ...] = ()  # what does not fit, as the fit script names it
    # A custom layout's text blocks in page order: each one's kind and where its words are.
    parts: tuple[tuple[str, _Box], ...] = ()


class Renderer:
    """Renders posts with one shared headless Chromium. Each render gets its own page."""

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Launch Chromium once and keep it. Calling this again does nothing."""
        async with self._lock:
            if self._browser is not None:
                return
            playwright = await async_playwright().start()
            try:
                self._browser = await playwright.chromium.launch(args=CHROMIUM_ARGS)
            except BaseException:
                await playwright.stop()
                raise
            self._playwright = playwright

    async def stop(self) -> None:
        """Close Chromium. The next render starts it again."""
        async with self._lock:
            browser, playwright = self._browser, self._playwright
            self._browser = self._playwright = None
            if browser is not None:
                await browser.close()
            if playwright is not None:
                await playwright.stop()

    async def render(
        self, spec: DesignSpec, kit: BrandKit, photo_path: Path | None, out_path: Path
    ) -> RenderReport:
        """Render the post as a PNG of exactly the kit's post size, written to out_path.

        For a layout whose words can sit over the photo, the page is rendered,
        the photo behind the words is measured, and when the contrast is too low
        (or cannot be measured) the page is rendered again with the shade. A
        custom layout is rendered once, as the designer placed it.
        """
        layout = spec.layout
        if layout in NEEDS_PHOTO and photo_path is None:
            raise ValueError(_needs_photo(layout))
        if photo_path is not None and not photo_path.is_file():
            raise FileNotFoundError(f"The photo file does not exist: {photo_path}")
        await self.start()
        browser = self._browser
        if browser is None:
            raise RuntimeError("The renderer was stopped while starting")
        if layout == "custom":
            return await self._render_custom(browser, spec, kit, photo_path, out_path)

        composition = composition_for(spec)
        start_px = START_PX[layout]
        min_px = MIN_PX_FOR.get(layout, MIN_PX)
        files = self._page_files(kit, spec.mode, photo_path)

        def page_html(headline_px: int, scrim: bool) -> str:
            return build_html(spec, kit, headline_px=headline_px, scrim=scrim, **files)

        over_photo = layout in TEXT_OVER_PHOTO
        shaded = over_photo and composition.scrim == "on"
        capture = await self._capture(
            browser, page_html(start_px, shaded), kit.post_size, start_px, min_px, out_path
        )

        contrast: float | None = None
        unmeasured = False
        if over_photo and capture.words is not None:
            # Reading the photo is blocking work, so it runs off the event loop.
            contrast = await asyncio.to_thread(
                _words_contrast, capture, photo_path, kit.mode_hex(spec.mode)
            )
            unmeasured = contrast is None
            too_low = contrast is None or contrast < MIN_CONTRAST
            if composition.scrim == "auto" and too_low:
                # The shade does not move the words, so the page settles on the same size.
                shaded = True
                capture = await self._capture(
                    browser,
                    page_html(capture.headline_px, True),
                    kit.post_size,
                    capture.headline_px,
                    min_px,
                    out_path,
                )

        adjustments: list[str] = []
        if capture.headline_px < start_px:
            adjustments.append(
                f"Headline reduced from {start_px}px to {capture.headline_px}px to fit."
            )
        if not capture.fits:
            adjustments.append(DOES_NOT_FIT)
        if shaded:
            asked = composition.scrim == "on"
            adjustments.append(SHADE_UNMEASURED if unmeasured and not asked else SHADE_ADDED)
        size = kit.post_size
        return RenderReport(
            width=size.width,
            height=size.height,
            layout=layout,
            mode=spec.mode,
            fits=capture.fits,
            adjustments=adjustments,
            text_contrast=_one_decimal_down(contrast) if contrast is not None else None,
            scrim_added=shaded,
        )

    async def _render_custom(
        self,
        browser: Browser,
        spec: DesignSpec,
        kit: BrandKit,
        photo_path: Path | None,
        out_path: Path,
    ) -> RenderReport:
        """Render the designer's arrangement as placed: nothing steps down and no shade is added.

        The report lists what the guardrails changed, each text block that overflows
        its box or runs past the margin, a logo past the margin, and, under a cover fit,
        the text block hardest to read over what is drawn beneath it, or that the photo
        could not be measured.
        """
        layout, adjustments = apply_guardrails(_custom_layout(spec), kit, spec.mode)
        start_px = START_PX["custom"]
        html = build_html(
            spec, kit, headline_px=start_px, **self._page_files(kit, spec.mode, photo_path)
        )
        capture = await self._capture(
            browser, html, kit.post_size, start_px, MIN_PX_FOR["custom"], out_path
        )
        adjustments.extend(_custom_fit_lines(capture.overflow))

        ratio: float | None = None
        if layout.photo_fit == "cover" and capture.parts and photo_path is not None:
            # Reading the photo is blocking work, so it runs off the event loop.
            lowest = await asyncio.to_thread(
                _custom_contrast,
                capture,
                photo_path,
                layout,
                _drawn_blocks(layout, spec),
                kit,
                spec.mode,
            )
            if lowest is None:
                adjustments.append(CUSTOM_UNMEASURED)
            else:
                contrast, kind = lowest
                ratio = _one_decimal_down(contrast)
                if contrast < MIN_CONTRAST:
                    adjustments.append(CUSTOM_LOW_CONTRAST.format(kind=kind, ratio=ratio))
        size = kit.post_size
        return RenderReport(
            width=size.width,
            height=size.height,
            layout="custom",
            mode=spec.mode,
            fits=capture.fits,
            adjustments=adjustments,
            text_contrast=ratio,
            scrim_added=False,
        )

    def _page_files(self, kit: BrandKit, mode: Mode, photo_path: Path | None) -> dict[str, Any]:
        """The files a page loads: the logo for the mode, the photo and the fonts."""
        italic_file = kit.typography.italic_file
        return {
            "logo_url": _file_url(logo_for_mode(kit, mode, self.work_dir / "logos")),
            "photo_url": _file_url(photo_path) if photo_path else None,
            "font_url": _file_url(Path(kit.root) / kit.typography.regular_file),
            "italic_font_url": _file_url(Path(kit.root) / italic_file) if italic_file else None,
        }

    async def _capture(
        self,
        browser: Browser,
        html: str,
        size: PostSize,
        start_px: int,
        min_px: int,
        out_path: Path,
    ) -> _Capture:
        """Load the page, fit its words, write its PNG to out_path and report what it measured."""
        scratch = self.work_dir / "render" / f"{uuid.uuid4().hex}.html"
        scratch.parent.mkdir(parents=True, exist_ok=True)
        scratch.write_text(html, encoding="utf-8")
        try:
            page = await browser.new_page(
                viewport={"width": size.width, "height": size.height}, device_scale_factor=1
            )
            try:
                await page.goto(_file_url(scratch), wait_until="load")
                await _wait_until_ready(page)
                headline_px, fit = await _fit_text(page, start_px, min_px)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=out_path, type="png")
            finally:
                # Closing is best-effort cleanup: a failing close() must not hide
                # whatever went wrong above, nor stop the scratch file below from
                # being deleted.
                with contextlib.suppress(Exception):
                    await page.close()
        finally:
            scratch.unlink(missing_ok=True)
        return _Capture(
            headline_px=headline_px,
            fits=fit["fits"],
            words=_Box.read(fit.get("words")),
            shown=tuple(fit.get("shown") or ()),
            photo=_Box.read(fit.get("photo")),
            overflow=tuple(fit.get("overflow") or ()),
            parts=tuple((part["kind"], _Box(**part["box"])) for part in fit.get("parts") or ()),
        )


async def _wait_until_ready(page: Page) -> None:
    missing = await page.evaluate(READY_SCRIPT)
    if missing:
        raise RuntimeError(f"The post could not load its {' and '.join(missing)}.")


async def _fit_text(page: Page, start_px: int, min_px: int) -> tuple[int, dict[str, Any]]:
    """Step the headline down until the text fits or reaches the smallest size."""
    headline_px = start_px
    fit = await page.evaluate("window.studioFit()")
    while not fit["fits"] and headline_px > min_px:
        headline_px = max(headline_px - STEP_PX, min_px)
        fit = await page.evaluate(RESIZE_SCRIPT, headline_px)
    return headline_px, fit


def _page_classes(layout: LayoutId, composition: Composition, *, scrim: bool) -> str:
    """The composition's parameters as classes on the page. Hero and type only take none."""
    if layout in ("hero", "type_only"):
        return ""
    row, column = composition.logo_position.split("_")
    classes = [
        f"logo-{row}",
        f"logo-{column}",
        f"text-{composition.text_position}",
        f"align-{composition.text_align}",
        f"photo-{composition.photo_side}",
    ]
    if scrim:
        classes.append("scrim")
    return " ".join(classes)


def _needs_photo(layout: LayoutId) -> str:
    return f"The {layout.replace('_', ' ')} layout needs a photo"


# ------------------------------------------------------------ the custom layout


def _custom_layout(spec: DesignSpec) -> CustomLayout:
    """The custom spec's arrangement. A custom spec without one cannot be drawn."""
    if spec.composition is None or spec.composition.custom is None:
        raise ValueError(CUSTOM_NEEDS_BLOCKS)
    return spec.composition.custom


def _custom_page(
    layout: CustomLayout, drawn: list[Block], kit: BrandKit, mode: Mode
) -> dict[str, Any]:
    """What custom.html draws for a guarded layout: the blocks it draws (`drawn`, in drawing
    order), the photo in its box and the canvas colour.

    The photo's box is None when the photo covers the canvas, and the canvas colour is
    empty when the mode's background stays.
    """
    canvas = layout.background
    box = layout.photo
    return {
        "blocks": [_block_style(block, kit, mode) for block in drawn],
        "photo_fit": layout.photo_fit,
        "photo_offset_x": layout.photo_offset_x,
        "photo_offset_y": layout.photo_offset_y,
        "photo_box": (
            {"left": box.x, "top": box.y, "width": box.w, "height": box.h} if box else None
        ),
        "background": resolve_colour(canvas, kit, mode, "background") if canvas else "",
    }


def _drawn_blocks(layout: CustomLayout, spec: DesignSpec) -> list[Block]:
    """The guarded layout's blocks the page draws, in drawing order.

    A text block whose words are empty is left out, as the other layouts leave out an
    empty subline.
    """
    texts = {"headline": spec.headline.strip(), "subline": spec.subline.strip()}
    shown = [block for block in layout.blocks if block.kind not in texts or texts[block.kind]]
    return sorted(shown, key=lambda block: _DRAWING_ORDER[block.kind])


def _block_style(block: Block, kit: BrandKit, mode: Mode) -> dict[str, Any]:
    """One block as the page places it: its box in percent of the canvas, and how its words look.

    The words take the face the block names (the kit's family when it names none, as a
    quoted CSS name), the block's weight or the role's, and italics when asked for.
    """
    role = _BLOCK_ROLES.get(block.kind)
    return {
        "kind": block.kind,
        "left": block.x,
        "top": block.y,
        "width": block.w,
        "height": block.h,
        "align": "center" if block.align == "centre" else "left",
        "px": block.size_px or (SUBLINE_PX if block.kind == "subline" else HEADLINE_PX),
        "hex": resolve_colour(block.colour, kit, mode, role) if role else "",
        "opacity": block.opacity,
        "family": _css_string(_face_name(block, kit)),
        "weight": block.weight or _role_weight(block, kit),
        "italic": block.italic,
    }


def _face_name(block: Block, kit: BrandKit) -> str:
    """The name of the face a block's words are set in: the one it names, or the kit's family."""
    face = face_named(kit, block.font)
    return face.name if face is not None else kit.typography.family


def _role_weight(block: Block, kit: BrandKit) -> int:
    """The kit's weight for a block's role: the headline's for the headline, the body's else."""
    typography = kit.typography
    return typography.headline_weight if block.kind == "headline" else typography.body_weight


def _extra_faces(drawn: list[Block], kit: BrandKit) -> list[Face]:
    """The faces the drawn words are set in beside the kit's family, once each, in page order."""
    faces: list[Face] = []
    for block in drawn:
        if block.kind not in ("headline", "subline"):
            continue
        face = face_named(kit, block.font)
        if face is not None and face.name != kit.typography.family and face not in faces:
            faces.append(face)
    return faces


def _custom_fit_lines(overflow: tuple[str, ...]) -> list[str]:
    """The custom fit script's findings as report lines, each given once.

    "headline box" reads "The headline overflows its box." and "subline margin" reads
    "The subline runs past the margin.".
    """
    lines: list[str] = []
    for entry in overflow:
        kind, _, problem = entry.partition(" ")
        if problem not in _CUSTOM_FIT_LINES:
            continue
        line = _CUSTOM_FIT_LINES[problem].format(kind=kind)
        if line not in lines:
            lines.append(line)
    return lines


def _custom_contrast(
    capture: _Capture,
    photo_path: Path,
    layout: CustomLayout,
    drawn: list[Block],
    kit: BrandKit,
    mode: Mode,
) -> tuple[float, str] | None:
    """The lowest WCAG contrast ratio among a custom layout's text blocks, with that block's kind.

    Each text block is measured on its own: its own words over all the page draws under
    them (the canvas colour, the photo where its box reaches, and every shade over both),
    against its own colour. The page shows the text blocks of `drawn` in that order, as
    `capture.parts` lists them. The photo is read once, and every block is measured from
    that one image. None when the photo cannot be measured.
    """
    if capture.photo is None:
        return None
    image = _read_photo(photo_path)
    background = resolve_colour(layout.background, kit, mode, "background")
    position = _photo_position(layout)
    shades = _custom_shades(layout, kit, mode, kit.post_size)
    texts = [block for block in drawn if block.kind in ("headline", "subline")]
    lowest: tuple[float, str] | None = None
    for block, (kind, words) in zip(texts, capture.parts):
        under = _custom_luminance_under(
            words, capture.photo, image, background, position, shades
        )
        if under is None:
            return None
        colour = resolve_colour(block.colour, kit, mode, _BLOCK_ROLES[block.kind])
        ratio = _contrast_ratio(_hex_luminance(colour), under)
        if lowest is None or ratio < lowest[0]:
            lowest = (ratio, kind)
    return lowest


def _custom_shades(
    layout: CustomLayout, kit: BrandKit, mode: Mode, size: PostSize
) -> tuple[_Shade, ...]:
    """The layout's shades as the page draws them, in drawing order, in CSS pixels."""
    across, down = size.width / 100, size.height / 100
    return tuple(
        _Shade(
            _Box(
                block.x * across,
                block.y * down,
                (block.x + block.w) * across,
                (block.y + block.h) * down,
            ),
            resolve_colour(block.colour, kit, mode, "background"),
            block.opacity,
        )
        for block in layout.blocks
        if block.kind == "shade"
    )


def _photo_position(layout: CustomLayout) -> tuple[float, float]:
    """The photo's place in its box: the share of the spare width and height it is offset by."""
    return (50 + layout.photo_offset_x) / 100, (50 + layout.photo_offset_y) / 100


# ------------------------------------------------------------ the contrast check

# sRGB levels as linear light, for relative luminance (WCAG 2).
_LINEAR: tuple[float, ...] = tuple(
    value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
    for value in (level / 255 for level in range(256))
)


def _words_contrast(
    capture: _Capture,
    photo_path: Path | None,
    colours: dict[str, str],
    position: tuple[float, float] = (0.5, 0.5),
) -> float | None:
    """The WCAG contrast ratio between the words and what the page draws under them.

    The lowest ratio over the colours of the parts shown (the headline's, and
    the subline's when there is one). None when the photo cannot be measured.
    `position` is passed on to `_luminance_under`.
    """
    if capture.words is None or capture.photo is None or photo_path is None:
        return None
    under = _luminance_under(
        capture.words, capture.photo, photo_path, colours["background"], position
    )
    if under is None:
        return None
    roles = {_PART_COLOURS[part] for part in capture.shown if part in _PART_COLOURS} or {"headline"}
    return min(_contrast_ratio(_hex_luminance(colours[role]), under) for role in roles)


def _luminance_under(
    words: _Box,
    photo: _Box,
    photo_path: Path,
    background: str,
    position: tuple[float, float] = (0.5, 0.5),
) -> float | None:
    """The mean relative luminance of what a template's page draws under the words.

    Over the photo's box that is the photo as the page draws it, scaled to cover
    the box and placed at `position` (the share of the spare width and height it
    is offset by; centred unless told otherwise), so the region is found with the
    same maths. Anywhere else it is the background colour. None when the photo
    cannot be read.
    """
    if words.area <= 0:
        return None
    over = words.overlap(photo)
    share = over.area / words.area
    luminance = (1 - share) * _hex_luminance(background)
    if share == 0:
        return luminance
    image = _read_photo(photo_path)
    if image is None:
        return None
    region = _photo_crop(image, photo, over, position)
    region.thumbnail((SAMPLE_PX, SAMPLE_PX), Image.Resampling.BOX)
    return luminance + share * _mean_luminance(region)


def _custom_luminance_under(
    words: _Box,
    photo: _Box,
    image: Image.Image | None,
    background: str,
    position: tuple[float, float],
    shades: tuple[_Shade, ...],
) -> float | None:
    """The mean relative luminance of what a custom layout's page draws under the words.

    The whole words rectangle is drawn as the page draws it: the canvas colour, then the
    photo where the rectangle overlaps the photo's box (cut out as `_luminance_under`
    cuts it), then every shade, blended over the part of the rectangle it covers, on the
    photo or off it. A mean does not depend on the rectangle's proportions, so it is drawn
    SAMPLE_PX each way, which keeps every edge within half a sample of its place. None
    when the rectangle reaches the photo's box and the photo could not be read (`image`,
    as `_read_photo` returns it, is None).
    """
    if words.area <= 0:
        return None
    drawn = Image.new("RGB", (SAMPLE_PX, SAMPLE_PX), _rgb(background))
    over = words.overlap(photo)
    left, top, right, bottom = _pixel_box(over, words, drawn.size)
    if right > left and bottom > top:
        if image is None:
            return None
        region = _photo_crop(image, photo, over, position)
        drawn.paste(region.resize((right - left, bottom - top), Image.Resampling.BOX), (left, top))
    for shade in shades:
        drawn = _shaded(drawn, words, shade)
    return _mean_luminance(drawn)


def _read_photo(photo_path: Path) -> Image.Image | None:
    """The photo in RGB, turned upright as the page shows it; None when it cannot be read."""
    try:
        with Image.open(photo_path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None
    if image.width == 0 or image.height == 0:
        return None
    return image


def _photo_crop(
    image: Image.Image, photo: _Box, over: _Box, position: tuple[float, float]
) -> Image.Image:
    """The part of the photo the page draws in `over`, a part of the photo's box.

    The page scales the photo to cover its box and places it at `position`, the share of
    the spare width and height it is offset by.
    """
    scale = max(photo.width / image.width, photo.height / image.height)
    left = photo.left + (photo.width - image.width * scale) * position[0]
    top = photo.top + (photo.height - image.height * scale) * position[1]
    x0 = min(max(math.floor((over.left - left) / scale), 0), image.width - 1)
    y0 = min(max(math.floor((over.top - top) / scale), 0), image.height - 1)
    x1 = max(min(math.ceil((over.right - left) / scale), image.width), x0 + 1)
    y1 = max(min(math.ceil((over.bottom - top) / scale), image.height), y0 + 1)
    return image.crop((x0, y0, x1, y1))


def _shaded(drawn: Image.Image, area: _Box, shade: _Shade) -> Image.Image:
    """`drawn`, an image of what the page draws in `area`, with the shade laid over the part
    of it the shade covers.

    The shade is blended in sRGB at its opacity, as the page blends it.
    """
    box = _pixel_box(area.overlap(shade.box), area, drawn.size)
    if box[2] <= box[0] or box[3] <= box[1] or shade.opacity <= 0:
        return drawn
    mask = Image.new("L", drawn.size, 0)
    mask.paste(round(shade.opacity * 255), box)
    return Image.composite(Image.new("RGB", drawn.size, _rgb(shade.colour)), drawn, mask)


def _pixel_box(part: _Box, whole: _Box, size: tuple[int, int]) -> tuple[int, int, int, int]:
    """Where `part`, a part of `whole`, falls in an image of `whole` that is `size` pixels.

    A part that misses `whole` comes out empty: its right edge at or left of its left edge,
    or its bottom edge at or above its top edge.
    """
    across, down = size[0] / whole.width, size[1] / whole.height
    return (
        round((part.left - whole.left) * across),
        round((part.top - whole.top) * down),
        round((part.right - whole.left) * across),
        round((part.bottom - whole.top) * down),
    )


def _mean_luminance(image: Image.Image) -> float:
    """The mean relative luminance of an RGB image's pixels, from its channel histograms."""
    pixels = image.width * image.height
    red, green, blue = (
        sum(count * _LINEAR[level] for level, count in enumerate(band.histogram())) / pixels
        for band in image.split()
    )
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _hex_luminance(hex_colour: str) -> float:
    red, green, blue = (_LINEAR[level] for level in _rgb(hex_colour))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    """The 0 to 255 levels of a #RRGGBB colour."""
    red, green, blue = (int(hex_colour[index : index + 2], 16) for index in (1, 3, 5))
    return red, green, blue


def _contrast_ratio(first: float, second: float) -> float:
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def _one_decimal_down(ratio: float) -> float:
    """The ratio rounded down to one decimal, so a ratio just under 4.5 never reads as 4.5."""
    return math.floor(ratio * 10 + 1e-9) / 10


# ------------------------------------------------------------ brand values


def _brand_style(
    kit: BrandKit,
    mode: Mode,
    *,
    font_url: str,
    italic_font_url: str | None,
    headline_px: int,
    faces: Sequence[Face] = (),
) -> Markup:
    """The page's one style block of brand values: the font files and the custom properties.

    The kit's family is always declared. `faces` are the further faces a custom layout's
    words are set in, each declared beside it, one rule per face and style.
    """
    typography = kit.typography
    family = _css_string(typography.family)
    rules = _font_rules(family, font_url, italic_font_url)
    for face in faces:
        italic_url = _file_url(face.italic) if face.italic else None
        rules.extend(_font_rules(_css_string(face.name), _file_url(face.regular), italic_url))
    colours = kit.mode_hex(mode)
    properties = {
        "--bg": colours["background"],
        "--headline": colours["headline"],
        "--body": colours["body"],
        "--font-family": family,
        "--headline-weight": str(typography.headline_weight),
        "--body-weight": str(typography.body_weight),
        "--headline-tracking": _css_token(typography.headline_tracking),
        "--headline-size": f"{headline_px}px",
        # The logo's width over its height, so layouts can keep clear of it.
        "--logo-ratio": f"{_logo_ratio(kit):.4f}",
    }
    declarations = "".join(f"  {name}: {value};\n" for name, value in properties.items())
    rules.append(f":root {{\n{declarations}}}")
    return Markup("<style>\n" + "\n".join(rules) + "\n</style>")


def _font_rules(family: str, regular_url: str, italic_url: str | None) -> list[str]:
    """One @font-face rule for each style a face has, across the whole weight range.

    `family` is the face's name as a quoted CSS string.
    """
    return [
        f"@font-face {{ font-family: {family}; src: url({_css_string(url)}); "
        f"font-weight: 100 900; font-style: {style}; }}"
        for url, style in ((regular_url, "normal"), (italic_url, "italic"))
        if url
    ]


def _logo_ratio(kit: BrandKit) -> float:
    with Image.open(Path(kit.root) / kit.logos.lockup_file) as logo:
        width, height = logo.size
    return width / height


def _css_string(value: str) -> str:
    """A quoted CSS string. Unusual characters are escaped so none can end the style block."""
    escaped = "".join(
        char if char.isalnum() or char in " -_./:" else f"\\{ord(char):x} " for char in value
    )
    return f'"{escaped}"'


def _css_token(value: str) -> str:
    """A bare CSS value such as -0.02em, refused if it could break out of its declaration."""
    if not re.fullmatch(r"[-+.%\w]+", value):
        raise ValueError(
            f"The brand's headline tracking must be a length such as -0.02em: {value!r}"
        )
    return value


def _file_url(path: Path) -> str:
    return path.resolve().as_uri()
