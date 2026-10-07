"""Tests for the reference sources: the inspiration board and a plain folder.

No network: the board source only ever reads local HTML files.
"""

from __future__ import annotations

from pathlib import Path

from studio.library.sources import BoardHtmlSource, FolderSource

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_BOARD = REPO_ROOT / "brands" / "hybridge" / "inspiration-board.html"

# Two categories under one round. The first card is an <article>, the second
# a <figure>; a third card repeats the figure's address and must be skipped
# (keeping the figure's own label, not the repeat's). The lightbox image has
# no src at all, and the article's address carries an HTML entity.
_INLINE_BOARD = """\
<!DOCTYPE html>
<html>
<body>
<main>
<section class="round">
<div class="round-title"><h2>Round One</h2><span>2 picks</span></div>
<section class="category"><h3>Alpha</h3><div class="grid">
<article class="card">
<div class="imgwrap"><img alt="Fallback Alt A" src="https://example.com/a.jpg?x=1&amp;y=2"/></div>
<div class="meta"><span class="id">R1-A1</span><span class="brand">Brand A</span></div>
<div class="actions"><button type="button">View larger</button><a href="https://example.com/a.jpg">Open source</a></div>
</article>
</div></section>
<section class="category"><h3>Beta</h3><div class="grid">
<figure class="card">
<img alt="Fallback Alt B" src="https://example.com/b.jpg"/>
<figcaption>Brand B</figcaption>
</figure>
<article class="card">
<img alt="Duplicate of B" src="https://example.com/b.jpg"/>
<div class="meta">Brand B Dup</div>
</article>
</div></section>
</section>
</main>
<div class="lightbox"><img id="lightbox-img" alt="Enlarged advertisement reference"/></div>
</body>
</html>
"""


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_board_items_from_inline_html(tmp_path: Path) -> None:
    html_path = _write(tmp_path, "board.html", _INLINE_BOARD)
    source = BoardHtmlSource(html_path)

    items = source.items()

    assert source.name == "board"
    assert len(items) == 2
    first, second = items

    # The entity is decoded and the repeated address (the second card under
    # Beta) is gone, leaving one item per category, in document order.
    assert first.source_url == "https://example.com/a.jpg?x=1&y=2"
    assert second.source_url == "https://example.com/b.jpg"
    assert all(item.source == "board" for item in items)
    assert all(item.local_path is None for item in items)

    assert first.category == "Round One / Alpha"
    assert second.category == "Round One / Beta"

    assert first.label == "R1-A1 Brand A"
    # The figure's own text survives; the later duplicate's "Brand B Dup" never does.
    assert second.label == "Brand B"


def test_real_board_has_sixty_labelled_items() -> None:
    source = BoardHtmlSource(REAL_BOARD)

    items = source.items()

    assert len(items) == 60
    assert all(item.source == "board" for item in items)
    assert all(item.source_url and item.source_url.startswith("http") for item in items)
    assert all(item.label.strip() for item in items)
    assert all(item.category.strip() for item in items)
    assert "Apple MacBook Pro" in items[0].label
    assert any("Radical simplicity" in item.category for item in items)


def test_folder_source_lists_only_images_sorted(tmp_path: Path) -> None:
    folder = tmp_path / "pics"
    folder.mkdir()
    for name in ("b.PNG", "a.jpg", "c.gif", "note.txt", "d.webp", "e.jpeg"):
        (folder / name).write_bytes(b"fake-bytes")
    (folder / "sub").mkdir()  # a subfolder must not be picked up

    source = FolderSource(folder)
    items = source.items()

    assert source.name == "folder"
    assert [Path(item.local_path).name for item in items] == [
        "a.jpg",
        "b.PNG",
        "c.gif",
        "d.webp",
        "e.jpeg",
    ]
    assert [item.label for item in items] == ["a", "b", "c", "d", "e"]
    assert all(Path(item.local_path).is_absolute() for item in items)
    assert all(item.source == "folder" for item in items)
    assert all(item.source_url is None for item in items)
    assert all(item.category == "" for item in items)


def test_folder_source_missing_folder(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"

    assert FolderSource(missing).items() == []
