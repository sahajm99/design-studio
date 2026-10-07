"""Sources that discover reference images before any of them are fetched.

Each source only ever produces `SourceItem`s; nothing here touches the
network or the disk cache that fetched bytes end up in.
"""

from __future__ import annotations

import copy
import html as html_lib
import re
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup, Tag

from studio.library.base import SourceItem

_CARD_TAGS = ("article", "figure", "li")
_REMOVED_TAGS = ("a", "button")
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
# v3.1: the most links one paste brings in.
MAX_LINKS = 20
_WEB_SCHEMES = ("http://", "https://")


class BoardHtmlSource:
    """Reads the images, labels and categories out of an inspiration-board page."""

    name = "board"

    def __init__(self, html_path: Path) -> None:
        self.html_path = html_path

    def items(self) -> list[SourceItem]:
        soup = BeautifulSoup(self.html_path.read_text(encoding="utf-8"), "html.parser")
        seen_urls: set[str] = set()
        results: list[SourceItem] = []

        for img in soup.find_all("img"):
            raw_src = img.get("src")
            if not raw_src or not raw_src.startswith(("http://", "https://")):
                continue  # a lightbox placeholder, or anything else with no web address

            source_url = html_lib.unescape(raw_src)
            if source_url in seen_urls:
                continue  # keep only the first occurrence of a repeated address
            seen_urls.add(source_url)

            results.append(
                SourceItem(
                    source=self.name,
                    source_url=source_url,
                    label=_label_for(img),
                    category=_category_for(img),
                )
            )
        return results


class FolderSource:
    """Reads every image file sitting directly inside a folder."""

    name = "folder"

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def items(self) -> list[SourceItem]:
        if not self.folder.is_dir():
            return []

        files = [
            path
            for path in self.folder.iterdir()
            if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES
        ]
        files.sort(key=lambda path: path.name)

        return [
            SourceItem(source=self.name, local_path=str(path.resolve()), label=path.stem)
            for path in files
        ]


class LinkListSource:
    """Reads the image links a designer pasted, one entry per link (v3.1).

    Each entry is stripped of surrounding whitespace; blanks and anything that is not an
    http or https address (the scheme in any case) are skipped, a repeated address counts
    once, and at most twenty links are kept.
    """

    name = "links"

    def __init__(self, urls: list[str]) -> None:
        self.urls = urls

    def items(self) -> list[SourceItem]:
        seen_urls: set[str] = set()
        results: list[SourceItem] = []

        for raw in self.urls:
            source_url = raw.strip()
            if not is_web_link(source_url):
                continue  # a blank line, or anything else with no web address
            if source_url in seen_urls:
                continue  # keep only the first occurrence of a repeated address
            seen_urls.add(source_url)

            results.append(
                SourceItem(
                    source=self.name,
                    source_url=source_url,
                    label=_link_label(source_url),
                    category="pasted",
                )
            )
            if len(results) == MAX_LINKS:
                break
        return results


# ------------------------------------------------------------------------ helpers


def is_web_link(text: str) -> bool:
    """Whether a pasted entry, already stripped, is an http or https address, in any case."""
    return text.lower().startswith(_WEB_SCHEMES)


def _label_for(img: Tag) -> str:
    card = _card_for(img)
    text = _visible_text(card) if card is not None else ""
    return text or (img.get("alt") or "").strip()


def _card_for(img: Tag) -> Tag | None:
    """The nearest article/figure/li ancestor, or a div holding just this one image."""
    ancestors = [tag for tag in img.parents if isinstance(tag, Tag)]

    for ancestor in ancestors:
        if ancestor.name in _CARD_TAGS:
            return ancestor

    for ancestor in ancestors:
        if ancestor.name == "div" and len(_http_images_within(ancestor)) == 1:
            return ancestor

    return None


def _http_images_within(container: Tag) -> list[Tag]:
    return [
        candidate
        for candidate in container.find_all("img")
        if (candidate.get("src") or "").startswith(("http://", "https://"))
    ]


def _visible_text(card: Tag) -> str:
    """The card's text with any link/button text removed, on a throwaway copy."""
    clone = copy.deepcopy(card)
    for tag in clone.find_all(_REMOVED_TAGS):
        tag.decompose()
    return _collapse_whitespace(clone.get_text(separator=" "))


def _category_for(img: Tag) -> str:
    h2 = img.find_previous("h2")
    h3 = img.find_previous("h3")
    h2_text = _collapse_whitespace(h2.get_text()) if h2 is not None else ""
    h3_text = _collapse_whitespace(h3.get_text()) if h3 is not None else ""

    if h2_text and h3_text:
        return f"{h2_text} / {h3_text}"
    return h3_text or h2_text


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _link_label(url: str) -> str:
    """The last part of a link's path without its extension, or the host when the path has none.

    A link too malformed to split is its own label; fetching it fails and reports why.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return url
    last = unquote(parts.path.rstrip("/").rsplit("/", 1)[-1]).strip()
    return PurePosixPath(last).stem or host
