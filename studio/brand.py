"""Read a brand folder into a BrandKit, and prepare its logo for a background."""

from __future__ import annotations

from pathlib import Path

import yaml
from PIL import Image
from pydantic import ValidationError

from studio.contracts import BrandKit, Mode


class BrandKitError(Exception):
    """The brand folder is missing something. The message says what and where."""


def load_brand_kit(brands_dir: Path, brand_id: str) -> BrandKit:
    root = brands_dir / brand_id
    manifest = root / "brand.yaml"
    if not manifest.is_file():
        raise BrandKitError(f"Brand kit '{brand_id}' has no brand.yaml at {manifest}")

    try:
        raw = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise BrandKitError(f"brand.yaml for '{brand_id}' is not valid YAML: {error}") from error
    if not isinstance(raw, dict):
        raise BrandKitError(f"brand.yaml for '{brand_id}' must be a mapping of settings")

    try:
        kit = BrandKit.model_validate({**raw, "id": brand_id, "root": str(root)})
    except ValidationError as error:
        first = error.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise BrandKitError(
            f"brand.yaml for '{brand_id}' is invalid at '{where}': {first['msg']}"
        ) from error

    files = {
        "logo lockup": kit.logos.lockup_file,
        "logo mark": kit.logos.mark_file,
        "regular font": kit.typography.regular_file,
        "italic font": kit.typography.italic_file,
        "inspiration board": kit.inspiration_board,
    }
    for label, relative in files.items():
        if relative and not (root / relative).is_file():
            raise BrandKitError(f"Brand kit '{brand_id}': {label} file not found: {relative}")
    # The further faces the kit names, each by its own name.
    for face in kit.typography.fonts:
        for style, relative in (("regular", face.regular_file), ("italic", face.italic_file)):
            if relative and not (root / relative).is_file():
                raise BrandKitError(
                    f"Brand kit '{brand_id}': font '{face.name}' {style} file not found: {relative}"
                )

    names = {colour.name for colour in kit.colours}
    for mode in ("dark", "light"):
        if mode not in kit.modes:
            raise BrandKitError(f"Brand kit '{brand_id}': modes.{mode} is missing")
        roles = kit.modes[mode]  # type: ignore[index]
        for role in ("background", "headline", "body"):
            colour_name = getattr(roles, role)
            if colour_name not in names:
                raise BrandKitError(
                    f"Brand kit '{brand_id}': modes.{mode}.{role} names an unknown colour "
                    f"'{colour_name}'"
                )
    return kit


def logo_for_mode(kit: BrandKit, mode: Mode, cache_dir: Path) -> Path:
    """The logo file to place on a dark or a light background.

    The lockup is used as it is. The one exception is a kit that allows a plain
    white version on dark backgrounds: that version is made from the lockup's
    transparency, so its shape is untouched.
    """
    lockup = Path(kit.root) / kit.logos.lockup_file
    if mode != "dark" or kit.logos.dark_background != "knockout_white":
        return lockup

    cache_dir.mkdir(parents=True, exist_ok=True)
    knockout = cache_dir / f"{kit.id}-lockup-white.png"
    if not knockout.exists() or knockout.stat().st_mtime < lockup.stat().st_mtime:
        with Image.open(lockup) as source:
            alpha = source.convert("RGBA").getchannel("A")
        white = Image.new("RGBA", alpha.size, (255, 255, 255, 0))
        white.putalpha(alpha)
        white.save(knockout)
    return knockout
