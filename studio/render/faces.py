"""The typefaces a post may be set in: the kit's own, and the studio's when the kit allows them.

A kit names its family and may name more faces under its folder. The studio keeps a few
open-licence faces of its own in `fonts/`, offered only to a kit that says so. The editor
lists these faces, the renderer embeds the ones a layout's words use, and the guardrails
turn any other name into the kit's family. Nothing here names a brand.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from studio.contracts import BrandKit, FontFace

STUDIO_FONTS_DIR = Path(__file__).parent / "fonts"

# The studio's own faces: variable fonts with a weight axis, under open licences (the OFL
# files sit beside them). A weight outside a face's range draws at the nearest end of it.
STUDIO_FACES: tuple[FontFace, ...] = (
    FontFace(
        name="Playfair Display",
        regular_file="PlayfairDisplay[wght].ttf",
        italic_file="PlayfairDisplay-Italic[wght].ttf",
    ),
    FontFace(name="Lora", regular_file="Lora[wght].ttf", italic_file="Lora-Italic[wght].ttf"),
    FontFace(
        name="Montserrat",
        regular_file="Montserrat[wght].ttf",
        italic_file="Montserrat-Italic[wght].ttf",
    ),
    FontFace(name="Space Grotesk", regular_file="SpaceGrotesk[wght].ttf"),
)


@dataclass(frozen=True)
class Face:
    """One typeface's files on disk: its upright file, and its italic file when it has one."""

    name: str
    regular: Path
    italic: Path | None


def available_faces(kit: BrandKit) -> list[Face]:
    """The faces a post of this kit may use, the kit's family first.

    Then each further face the kit names, under its folder, then the studio's own faces
    when the kit allows them. A face whose upright file is missing is left out, a missing
    italic file leaves a face without italics, and a name already taken is skipped.
    """
    typography = kit.typography
    root = Path(kit.root)
    family = FontFace(
        name=typography.family,
        regular_file=typography.regular_file,
        italic_file=typography.italic_file,
    )
    entries = [(family, root), *((font, root) for font in typography.fonts)]
    if typography.studio_fonts:
        entries.extend((font, STUDIO_FONTS_DIR) for font in STUDIO_FACES)
    faces: list[Face] = []
    for font, folder in entries:
        face = _face(font, folder)
        if face is not None and all(face.name != known.name for known in faces):
            faces.append(face)
    return faces


def face_named(kit: BrandKit, name: str) -> Face | None:
    """The available face with this name; "" and the family's name both give the kit's family."""
    wanted = name or kit.typography.family
    return next((face for face in available_faces(kit) if face.name == wanted), None)


def _face(font: FontFace, folder: Path) -> Face | None:
    """The face's files under `folder`, or None when its upright file is missing."""
    regular = folder / font.regular_file
    if not regular.is_file():
        return None
    italic = folder / font.italic_file if font.italic_file else None
    return Face(font.name, regular, italic if italic is not None and italic.is_file() else None)
