"""Summarise what the designer's liked and disliked references have in common."""

from __future__ import annotations

from studio.contracts import STYLE_FIELDS, STYLE_LABELS, Reference, TasteProfile

_NOTHING_COUNTED = "No likes or dislikes yet."


def build_taste_profile(references: list[Reference]) -> TasteProfile:
    """Count the liked and disliked references' style cards and summarise them.

    Only references with a style card whose choice is "liked" or "disliked"
    are counted; everything else (no choice yet, or not analysed yet) is
    ignored.
    """
    liked_refs = [ref for ref in references if _counts_toward(ref, "liked")]
    disliked_refs = [ref for ref in references if _counts_toward(ref, "disliked")]

    liked_counts = _tally(liked_refs)
    disliked_counts = _tally(disliked_refs)

    sentences = [
        sentence
        for sentence in (
            _sentence("Liked", len(liked_refs), liked_counts),
            _sentence("Disliked", len(disliked_refs), disliked_counts),
        )
        if sentence
    ]
    summary = " ".join(sentences) if sentences else _NOTHING_COUNTED

    return TasteProfile(
        liked_count=len(liked_refs),
        disliked_count=len(disliked_refs),
        liked=liked_counts,
        disliked=disliked_counts,
        summary=summary,
        liked_reference_ids=[ref.id for ref in liked_refs],
    )


def _counts_toward(ref: Reference, choice: str) -> bool:
    return ref.style_card is not None and ref.choice == choice


def _tally(refs: list[Reference]) -> dict[str, dict[str, int]]:
    """`{field: {value: count}}` for each field in STYLE_FIELDS; zero counts left out."""
    tally: dict[str, dict[str, int]] = {}
    for field in STYLE_FIELDS:
        counts: dict[str, int] = {}
        for ref in refs:
            value = getattr(ref.style_card, field)
            counts[value] = counts.get(value, 0) + 1
        if counts:
            tally[field] = counts
    return tally


def _sentence(label: str, n: int, counts: dict[str, dict[str, int]]) -> str:
    """`"{label} {n}: ..."`, or "" when there is nothing in this group to report."""
    if n == 0:
        return ""

    parts = []
    for field in STYLE_FIELDS:
        value, count = _majority(field, counts.get(field, {}))
        if value is not None and count * 2 >= n:
            parts.append(f"{count} {STYLE_LABELS[field][value]}")

    if not parts:
        return f"{label} {n}: no clear pattern yet."
    return f"{label} {n}: " + ", ".join(parts) + "."


def _majority(field: str, counts: dict[str, int]) -> tuple[str | None, int]:
    """The most common value for a field; a tie goes to whichever STYLE_LABELS lists first."""
    best_value: str | None = None
    best_count = 0
    for value in STYLE_LABELS[field]:
        count = counts.get(value, 0)
        if count > best_count:
            best_value, best_count = value, count
    return best_value, best_count
