"""Tests for the brand kit loader and the dark-background logo knockout."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PIL import Image

from studio.brand import BrandKitError, load_brand_kit, logo_for_mode
from studio.contracts import BrandKit

REPO_ROOT = Path(__file__).resolve().parent.parent
BRANDS_DIR = REPO_ROOT / "brands"
_HYBRIDGE_FONT = BRANDS_DIR / "hybridge" / "fonts" / "InterVariable.ttf"


def _write_minimal_brand(root: Path, brand_yaml: str) -> None:
    """A brand folder with a valid logo and font file, plus the given brand.yaml text."""
    (root / "logo").mkdir(parents=True)
    (root / "fonts").mkdir()
    Image.new("RGBA", (10, 10), (0, 0, 0, 255)).save(root / "logo" / "lockup.png")
    shutil.copy(_HYBRIDGE_FONT, root / "fonts" / "test.ttf")
    (root / "brand.yaml").write_text(brand_yaml, encoding="utf-8")


def test_hybridge_kit_loads(hybridge_kit: BrandKit) -> None:
    assert hybridge_kit.name == "Hybridge"
    assert hybridge_kit.mode_hex("dark")["background"] == "#18181B"
    assert hybridge_kit.mode_hex("light")["headline"] == "#0F4B7B"
    assert hybridge_kit.post_size.width == 1080
    assert hybridge_kit.post_size.height == 1350
    assert hybridge_kit.inspiration_board == "inspiration-board.html"


def test_missing_manifest_message(tmp_path: Path) -> None:
    brands_dir = tmp_path / "brands"
    brands_dir.mkdir()

    with pytest.raises(BrandKitError) as exc_info:
        load_brand_kit(brands_dir, "ghost")

    message = str(exc_info.value)
    assert "ghost" in message
    assert "brand.yaml" in message


def test_missing_logo_file_message(second_brands_dir: Path) -> None:
    (second_brands_dir / "testbrand" / "logo" / "lockup.png").unlink()

    with pytest.raises(BrandKitError) as exc_info:
        load_brand_kit(second_brands_dir, "testbrand")

    message = str(exc_info.value)
    assert "logo lockup" in message
    assert "lockup.png" in message


def test_unknown_mode_colour_message(tmp_path: Path) -> None:
    root = tmp_path / "brands" / "badcolour"
    _write_minimal_brand(
        root,
        """\
name: Bad Colour
audience: Testers.
feel: For testing only.
colours:
  - { name: ink, hex: "#111111", use: Everything }
modes:
  dark: { background: ink, headline: ink, body: ghost }
  light: { background: ink, headline: ink, body: ink }
typography:
  family: Test
  regular_file: fonts/test.ttf
logos:
  lockup_file: logo/lockup.png
""",
    )

    with pytest.raises(BrandKitError) as exc_info:
        load_brand_kit(tmp_path / "brands", "badcolour")

    message = str(exc_info.value)
    assert "modes.dark.body" in message
    assert "ghost" in message


def test_invalid_hex_message(tmp_path: Path) -> None:
    root = tmp_path / "brands" / "badhex"
    _write_minimal_brand(
        root,
        """\
name: Bad Hex
audience: Testers.
feel: For testing only.
colours:
  - { name: ink, hex: "not-a-colour", use: Everything }
modes:
  dark: { background: ink, headline: ink, body: ink }
  light: { background: ink, headline: ink, body: ink }
typography:
  family: Test
  regular_file: fonts/test.ttf
logos:
  lockup_file: logo/lockup.png
""",
    )

    with pytest.raises(BrandKitError) as exc_info:
        load_brand_kit(tmp_path / "brands", "badhex")

    message = str(exc_info.value)
    assert "badhex" in message
    assert "hex" in message


def test_second_brand_loads(second_kit: BrandKit) -> None:
    assert second_kit.id == "testbrand"
    assert second_kit.name == "Test Brand"
    assert second_kit.logos.dark_background == "as_is"
    assert second_kit.mode_hex("light")["background"] == "#FFF8E7"


def test_knockout_logo_for_dark(hybridge_kit: BrandKit, tmp_path: Path) -> None:
    cache_dir = tmp_path / "logos"

    knockout_path = logo_for_mode(hybridge_kit, "dark", cache_dir)

    assert knockout_path.parent == cache_dir
    assert knockout_path.is_file()

    lockup_path = Path(hybridge_kit.root) / hybridge_kit.logos.lockup_file
    with Image.open(lockup_path) as lockup, Image.open(knockout_path) as knockout:
        assert knockout.size == lockup.size
        lockup_alpha = lockup.convert("RGBA").getchannel("A").get_flattened_data()
        knockout_rgba = knockout.convert("RGBA")
        knockout_alpha = knockout_rgba.getchannel("A").get_flattened_data()
        assert knockout_alpha == lockup_alpha

        non_transparent = [pixel for pixel in knockout_rgba.get_flattened_data() if pixel[3] != 0]
        assert non_transparent  # the lockup is not fully transparent
        assert all(pixel[:3] == (255, 255, 255) for pixel in non_transparent)


def test_logo_is_untouched_for_light_and_for_as_is_kits(
    hybridge_kit: BrandKit, second_kit: BrandKit, tmp_path: Path
) -> None:
    cache_dir = tmp_path / "logos"

    light_result = logo_for_mode(hybridge_kit, "light", cache_dir)
    assert light_result == Path(hybridge_kit.root) / hybridge_kit.logos.lockup_file

    as_is_result = logo_for_mode(second_kit, "dark", cache_dir)
    assert as_is_result == Path(second_kit.root) / second_kit.logos.lockup_file

    # Neither call needed the cache folder.
    assert not cache_dir.exists()
