"""Fixtures shared by every test module. Tests run in the container with no network."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PIL import Image

from studio.brand import load_brand_kit
from studio.config import Settings
from studio.contracts import BrandKit

REPO_ROOT = Path(__file__).resolve().parent.parent
BRANDS_DIR = REPO_ROOT / "brands"

# The second brand's logo is a solid block of this colour, so tests can find it in a render.
SECOND_BRAND_LOGO_RGB = (255, 0, 255)
SECOND_BRAND_LOGO_SIZE = (400, 100)

SECOND_BRAND_YAML = """\
name: Test Brand
audience: Testers.
feel: Plain and loud, nothing like the first brand.
rules:
  - Keep it short.
colours:
  - { name: night, hex: "#102030", use: Dark background }
  - { name: paper, hex: "#FFF8E7", use: Light background }
  - { name: flame, hex: "#FF5500", use: Headlines }
  - { name: coal, hex: "#111111", use: Body text }
modes:
  dark: { background: night, headline: paper, body: paper }
  light: { background: paper, headline: flame, body: coal }
typography:
  family: Test Serif
  regular_file: fonts/test.ttf
  headline_weight: 700
  body_weight: 400
logos:
  lockup_file: logo/lockup.png
  dark_background: as_is
post_size: { width: 1080, height: 1350 }
"""


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Demo-mode settings that write only inside the test's temporary folder."""
    return Settings(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "db" / "studio.db",
        brands_dir=BRANDS_DIR,
        brand_id="hybridge",
    )


@pytest.fixture
def hybridge_kit() -> BrandKit:
    return load_brand_kit(BRANDS_DIR, "hybridge")


def _system_font() -> Path:
    """Any font already in the image that is not the first brand's typeface."""
    fonts = sorted(Path("/usr/share/fonts").rglob("*.ttf"))
    serif = [font for font in fonts if "Serif" in font.name and "Regular" in font.name]
    if serif:
        return serif[0]
    if fonts:
        return fonts[0]
    raise RuntimeError("No system font found under /usr/share/fonts for the second brand")


@pytest.fixture
def second_brands_dir(tmp_path: Path) -> Path:
    """A brands folder holding one very different brand, 'testbrand'.

    Use it with load_brand_kit(second_brands_dir, "testbrand") to prove that a
    brand is only a folder.
    """
    root = tmp_path / "brands" / "testbrand"
    (root / "logo").mkdir(parents=True)
    (root / "fonts").mkdir()
    Image.new("RGBA", SECOND_BRAND_LOGO_SIZE, (*SECOND_BRAND_LOGO_RGB, 255)).save(
        root / "logo" / "lockup.png"
    )
    shutil.copy(_system_font(), root / "fonts" / "test.ttf")
    (root / "brand.yaml").write_text(SECOND_BRAND_YAML, encoding="utf-8")
    return tmp_path / "brands"


@pytest.fixture
def second_kit(second_brands_dir: Path) -> BrandKit:
    return load_brand_kit(second_brands_dir, "testbrand")


@pytest.fixture
def store(settings: Settings):
    from studio.store import Store

    store = Store(settings.db_path, settings.data_dir)
    store.init()
    return store
