"""Tests for build_taste_profile: counting style cards and summarising them."""

from __future__ import annotations

from studio.contracts import Reference, StyleCard
from studio.library.taste import build_taste_profile


def _ref(ref_id: str, choice: str, **card_overrides: object) -> Reference:
    """A reference with a style card, defaulting to the brief's example values."""
    defaults: dict[str, object] = dict(
        background="dark",
        layout="centred_subject",
        text_amount="few",
        subject="product",
        colour_count="one_or_two",
    )
    defaults.update(card_overrides)
    return Reference(
        id=ref_id,
        source="board",
        status="analysed",
        choice=choice,  # type: ignore[arg-type]
        style_card=StyleCard(**defaults),  # type: ignore[arg-type]
    )


def test_nothing_counted() -> None:
    profile = build_taste_profile([])

    assert profile.liked_count == 0
    assert profile.disliked_count == 0
    assert profile.liked == {}
    assert profile.disliked == {}
    assert profile.liked_reference_ids == []
    assert profile.summary == "No likes or dislikes yet."


def test_example_summary() -> None:
    refs = [
        _ref("r1", "liked", background="dark", text_amount="few", subject="product"),
        _ref("r2", "liked", background="dark", text_amount="few", subject="product"),
        _ref("r3", "liked", background="dark", text_amount="some", subject="person"),
    ]

    profile = build_taste_profile(refs)

    assert profile.summary == (
        "Liked 3: 3 dark background, 3 single centred subject, 2 few words, "
        "2 product as subject, 3 one or two colours."
    )
    assert profile.liked_count == 3
    assert profile.disliked_count == 0
    assert profile.liked_reference_ids == ["r1", "r2", "r3"]


def test_ignores_unrated_and_unanalysed() -> None:
    liked = _ref("liked-1", "liked")
    unrated = _ref("none-1", "none")  # has a style card, but the designer made no choice
    no_card = Reference(id="pending-1", source="board", choice="liked", status="pending")  # not analysed yet

    profile = build_taste_profile([liked, unrated, no_card])

    assert profile.liked_count == 1
    assert profile.disliked_count == 0
    assert profile.liked_reference_ids == ["liked-1"]


def test_disliked_group_is_joined_with_a_space() -> None:
    liked = [_ref(f"liked-{i}", "liked", background="dark") for i in range(2)]
    disliked = [_ref(f"disliked-{i}", "disliked", background="light") for i in range(2)]

    profile = build_taste_profile(liked + disliked)

    expected_liked = (
        "Liked 2: 2 dark background, 2 single centred subject, 2 few words, "
        "2 product as subject, 2 one or two colours."
    )
    expected_disliked = (
        "Disliked 2: 2 light background, 2 single centred subject, 2 few words, "
        "2 product as subject, 2 one or two colours."
    )
    assert profile.summary == f"{expected_liked} {expected_disliked}"


def test_no_clear_pattern() -> None:
    # Three liked references, every field split evenly three ways: no majority anywhere.
    refs = [
        _ref(
            "r1", "liked", background="dark", layout="centred_subject",
            text_amount="none", subject="product", colour_count="one_or_two",
        ),
        _ref(
            "r2", "liked", background="light", layout="full_bleed_photo",
            text_amount="few", subject="person", colour_count="three_or_four",
        ),
        _ref(
            "r3", "liked", background="colour", layout="split",
            text_amount="some", subject="place", colour_count="five_plus",
        ),
    ]

    profile = build_taste_profile(refs)

    assert profile.summary == "Liked 3: no clear pattern yet."


def test_majority() -> None:
    refs = [
        _ref("r1", "liked", background="dark"),
        _ref("r2", "liked", background="dark"),
        _ref("r3", "liked", background="light"),
    ]

    profile = build_taste_profile(refs)

    assert profile.liked["background"] == {"dark": 2, "light": 1}
    assert profile.majority("background") == "dark"
    assert "2 dark background" in profile.summary
    assert "light background" not in profile.summary


def test_tie_goes_to_first_label() -> None:
    # "light" appears first in the input, but STYLE_LABELS["background"] lists "dark" first.
    refs = [
        _ref("r1", "liked", background="light"),
        _ref("r2", "liked", background="dark"),
    ]

    profile = build_taste_profile(refs)

    assert profile.liked["background"] == {"light": 1, "dark": 1}
    assert profile.summary == (
        "Liked 2: 1 dark background, 2 single centred subject, 2 few words, "
        "2 product as subject, 2 one or two colours."
    )
