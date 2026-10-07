"""The renderer: a design spec and a brand kit go in, the finished post comes out.

The rendering tests share one Chromium, started once for the module.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import NoReturn

import pytest
import pytest_asyncio
from PIL import Image, ImageChops, ImageDraw, ImageOps

from studio.contracts import BrandKit, DesignSpec, LayoutId, Mode
from studio.render import Renderer, build_html

LAYOUTS_DIR = Path(__file__).resolve().parent.parent / "studio" / "render" / "layouts"
POST_SIZE = (1080, 1350)
DOES_NOT_FIT = "Text does not fit at the smallest size. Shorten the copy."

MAGENTA = (255, 0, 255)  # the second brand's logo, a solid block (see conftest)
LOGO_NAVY = (0x00, 0x30, 0x5E)  # the first brand's wordmark colour

# Async tests run on the module's event loop, where the shared Chromium lives.
on_module_loop = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def renderer(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Renderer]:
    renderer = Renderer(tmp_path_factory.mktemp("renderer"))
    await renderer.start()
    yield renderer
    await renderer.stop()


@pytest.fixture
def photo(tmp_path: Path) -> Path:
    """A soft radial gradient standing in for a provider's photo."""
    glow = ImageOps.invert(Image.radial_gradient("L")).resize((1024, 768))
    path = tmp_path / "photo.png"
    ImageOps.colorize(glow, black=(24, 24, 27), white=(190, 200, 214)).save(path)
    return path


def make_spec(
    layout: LayoutId = "hero",
    mode: Mode = "dark",
    *,
    headline: str = "Smile with confidence again.",
    subline: str = "Implant care, planned around you.",
) -> DesignSpec:
    return DesignSpec(layout=layout, mode=mode, headline=headline, subline=subline)


def html_for(spec: DesignSpec, kit: BrandKit) -> str:
    return build_html(
        spec,
        kit,
        logo_url="file:///brand/logo.png",
        photo_url="file:///photos/photo.png",
        font_url="file:///brand/font.ttf",
        italic_font_url=None,
        headline_px=84,
    )


def rgb(hex_colour: str) -> tuple[int, int, int]:
    return int(hex_colour[1:3], 16), int(hex_colour[3:5], 16), int(hex_colour[5:7], 16)


def pixel_at(path: Path, xy: tuple[int, int]) -> tuple[int, int, int]:
    with Image.open(path) as image:
        return image.convert("RGB").getpixel(xy)


def close_to(pixel: tuple[int, int, int], expected: tuple[int, int, int], tolerance: int) -> bool:
    return all(abs(got - want) <= tolerance for got, want in zip(pixel, expected))


def colour_mask(image: Image.Image, colour: tuple[int, int, int], tolerance: int) -> Image.Image:
    """White wherever a pixel is within the tolerance of the colour on every channel."""
    mask = Image.new("L", image.size, 255)
    for band, value in zip(image.convert("RGB").split(), colour):
        near = band.point(lambda level, value=value: 255 if abs(level - value) <= tolerance else 0)
        mask = ImageChops.multiply(mask, near)
    return mask


def green_mask(image: Image.Image) -> Image.Image:
    """White wherever green clearly outweighs red and blue, even where a photo fades out."""
    red, green, blue = image.convert("RGB").split()
    over_red = ImageChops.subtract(green, red).point(lambda level: 255 if level > 80 else 0)
    over_blue = ImageChops.subtract(green, blue).point(lambda level: 255 if level > 80 else 0)
    return ImageChops.multiply(over_red, over_blue)


def test_build_html_escapes_text(hybridge_kit: BrandKit) -> None:
    html = html_for(make_spec(headline='<b>Fish & chips</b> "quoted"'), hybridge_kit)

    assert "&lt;b&gt;Fish &amp; chips&lt;/b&gt;" in html
    assert "<b>Fish" not in html


def test_layout_files_hold_no_brand_values() -> None:
    files = sorted(path for path in LAYOUTS_DIR.rglob("*") if path.is_file())
    assert {"base.css", "hero.html", "type_only.html"} <= {path.name for path in files}

    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "inter" not in text.lower(), path.name
        assert "hybridge" not in text.lower(), path.name
        assert not re.search(r"#[0-9A-Fa-f]{6}", text), path.name


@on_module_loop
async def test_hero_dark_size_and_background(
    renderer: Renderer, hybridge_kit: BrandKit, photo: Path, tmp_path: Path
) -> None:
    out = tmp_path / "hero-dark.png"

    report = await renderer.render(make_spec("hero", "dark"), hybridge_kit, photo, out)

    with Image.open(out) as image:
        assert image.format == "PNG"
        assert image.size == POST_SIZE
    background = rgb(hybridge_kit.mode_hex("dark")["background"])
    assert close_to(pixel_at(out, (5, 1345)), background, 2)
    assert (report.width, report.height) == POST_SIZE
    assert (report.layout, report.mode) == ("hero", "dark")


@on_module_loop
async def test_type_only_light_without_photo(
    renderer: Renderer, hybridge_kit: BrandKit, tmp_path: Path
) -> None:
    out = tmp_path / "not" / "yet" / "made" / "type-only-light.png"

    report = await renderer.render(make_spec("type_only", "light"), hybridge_kit, None, out)

    with Image.open(out) as image:
        assert image.size == POST_SIZE
    background = rgb(hybridge_kit.mode_hex("light")["background"])
    assert close_to(pixel_at(out, (5, 1345)), background, 2)
    assert (report.layout, report.mode, report.fits) == ("type_only", "light", True)


@on_module_loop
async def test_hero_without_photo_raises(
    renderer: Renderer, hybridge_kit: BrandKit, tmp_path: Path
) -> None:
    out = tmp_path / "post.png"

    with pytest.raises(ValueError, match="The hero layout needs a photo"):
        await renderer.render(make_spec("hero"), hybridge_kit, None, out)
    assert not out.exists()


@on_module_loop
async def test_missing_photo_file_raises(
    renderer: Renderer, hybridge_kit: BrandKit, tmp_path: Path
) -> None:
    out = tmp_path / "post.png"

    with pytest.raises(FileNotFoundError):
        await renderer.render(make_spec("hero"), hybridge_kit, tmp_path / "missing.png", out)
    assert not out.exists()


@on_module_loop
async def test_failed_render_leaves_no_scratch_file(
    renderer: Renderer,
    hybridge_kit: BrandKit,
    photo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def explode(**_: object) -> NoReturn:
        raise RuntimeError("synthetic new_page failure")

    monkeypatch.setattr(renderer._browser, "new_page", explode)

    with pytest.raises(RuntimeError, match="synthetic new_page failure"):
        await renderer.render(
            make_spec("hero", "dark"), hybridge_kit, photo, tmp_path / "post.png"
        )

    assert list((renderer.work_dir / "render").iterdir()) == []


@on_module_loop
async def test_long_headline_steps_down(
    renderer: Renderer, hybridge_kit: BrandKit, photo: Path, tmp_path: Path
) -> None:
    sentence = "Every full-arch case deserves a calm, careful plan from the first scan to the fit. "
    headline = (sentence * 3)[:200]
    assert len(headline) == 200

    report = await renderer.render(
        make_spec("hero", headline=headline), hybridge_kit, photo, tmp_path / "long.png"
    )

    assert report.adjustments
    reduced = re.fullmatch(r"Headline reduced from 64px to (\d+)px to fit\.", report.adjustments[0])
    assert reduced is not None, report.adjustments
    assert int(reduced.group(1)) in {60, 56, 52, 48, 44}
    assert report.fits or (DOES_NOT_FIT in report.adjustments and not report.fits)


@on_module_loop
@pytest.mark.parametrize("layout", ["hero", "type_only"])
async def test_short_headline_needs_no_adjustment(
    renderer: Renderer, hybridge_kit: BrandKit, photo: Path, tmp_path: Path, layout: LayoutId
) -> None:
    spec = make_spec(layout, headline="Smile again.")
    photo_path = photo if layout == "hero" else None

    report = await renderer.render(spec, hybridge_kit, photo_path, tmp_path / "short.png")

    assert report.fits
    assert report.adjustments == []


def test_empty_subline_is_left_out(hybridge_kit: BrandKit) -> None:
    with_subline = html_for(make_spec(subline="A quiet line beneath."), hybridge_kit)
    without_subline = html_for(make_spec(subline=""), hybridge_kit)

    assert 'class="subline"' in with_subline
    assert "A quiet line beneath." in with_subline
    assert 'class="subline"' not in without_subline


@on_module_loop
async def test_logo_keeps_its_proportions(
    renderer: Renderer, second_kit: BrandKit, tmp_path: Path
) -> None:
    out = tmp_path / "logo.png"

    await renderer.render(make_spec("type_only", "dark"), second_kit, None, out)

    with Image.open(out) as image:
        box = colour_mask(image, MAGENTA, 60).getbbox()
    assert box is not None, "the logo is missing"
    left, top, right, bottom = box
    assert abs((right - left) / (bottom - top) - 4.0) / 4.0 <= 0.03
    width, height = POST_SIZE
    assert min(left, top, width - right, height - bottom) >= 40


@on_module_loop
async def test_second_brand_colours(
    renderer: Renderer, second_kit: BrandKit, tmp_path: Path
) -> None:
    for mode, background in (("dark", "#102030"), ("light", "#FFF8E7")):
        out = tmp_path / f"second-{mode}.png"

        await renderer.render(make_spec("type_only", mode), second_kit, None, out)

        assert close_to(pixel_at(out, (0, 0)), rgb(background), 2), mode
        assert close_to(pixel_at(out, (1079, 1349)), rgb(background), 2), mode


@on_module_loop
async def test_hybridge_logo_is_white_on_dark(
    renderer: Renderer, hybridge_kit: BrandKit, tmp_path: Path
) -> None:
    out = tmp_path / "logo-dark.png"

    await renderer.render(
        make_spec("type_only", "dark", headline="Smile again."), hybridge_kit, None, out
    )

    with Image.open(out) as image:
        top_quarter = image.convert("RGB").crop((0, 0, POST_SIZE[0], POST_SIZE[1] // 4))
    assert colour_mask(top_quarter, (255, 255, 255), 16).getbbox() is not None
    assert colour_mask(top_quarter, LOGO_NAVY, 40).getbbox() is None


@on_module_loop
async def test_photo_is_cropped_not_stretched(
    renderer: Renderer, hybridge_kit: BrandKit, tmp_path: Path
) -> None:
    circle = tmp_path / "circle.png"
    picture = Image.new("RGB", (640, 480), (0, 0, 0))
    ImageDraw.Draw(picture).ellipse((272, 192, 368, 288), fill=(0, 255, 0))
    picture.save(circle)
    out = tmp_path / "circle-post.png"

    await renderer.render(make_spec("hero", "dark"), hybridge_kit, circle, out)

    with Image.open(out) as image:
        box = green_mask(image).getbbox()
    assert box is not None, "the photo is missing"
    left, top, right, bottom = box
    assert abs((right - left) / (bottom - top) - 1.0) <= 0.05


@on_module_loop
async def test_three_renders_at_once(
    renderer: Renderer, hybridge_kit: BrandKit, photo: Path, tmp_path: Path
) -> None:
    jobs: list[tuple[DesignSpec, Path | None]] = [
        (make_spec("hero", "dark"), photo),
        (make_spec("type_only", "light"), None),
        (make_spec("type_only", "dark"), None),
    ]
    outs = [tmp_path / f"post-{index}.png" for index in range(len(jobs))]

    reports = await asyncio.gather(
        *(
            renderer.render(spec, hybridge_kit, photo_path, out)
            for (spec, photo_path), out in zip(jobs, outs)
        )
    )

    for (spec, _), report, out in zip(jobs, reports, outs):
        assert (report.layout, report.mode) == (spec.layout, spec.mode)
        with Image.open(out) as image:
            assert image.size == POST_SIZE
        background = rgb(hybridge_kit.mode_hex(spec.mode)["background"])
        assert close_to(pixel_at(out, (5, 1345)), background, 2)
    assert list((renderer.work_dir / "render").iterdir()) == []
