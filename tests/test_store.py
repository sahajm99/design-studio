"""Tests for the SQLite-backed Store: references, runs, run events and posts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from studio.config import Settings
from studio.contracts import DesignSpec, Post, Reference, RenderReport, StyleCard
from studio.store import Store


def _style_card(**overrides: object) -> StyleCard:
    defaults: dict[str, object] = dict(
        background="dark",
        layout="centred_subject",
        text_amount="few",
        subject="product",
        colour_count="one_or_two",
    )
    defaults.update(overrides)
    return StyleCard(**defaults)  # type: ignore[arg-type]


def _post(**overrides: object) -> Post:
    defaults: dict[str, object] = dict(
        root_post_id="root-1",
        id="root-1",
        version=1,
        run_id="run-1",
        brand_id="hybridge",
        brief="A brief",
        image_path="posts/root-1.png",
        spec=DesignSpec(layout="type_only", mode="dark", headline="Smile more"),
        render_report=RenderReport(width=1080, height=1350, layout="type_only", mode="dark", fits=True),
    )
    defaults.update(overrides)
    return Post(**defaults)  # type: ignore[arg-type]


def test_init_is_idempotent_and_creates_folders(store: Store) -> None:
    # The fixture already called init() once; calling it again must be harmless.
    store.init()
    store.init()

    for folder in (
        store.references_dir,
        store.photos_dir,
        store.posts_dir,
        store.work_dir,
        store.inbox_dir,
    ):
        assert folder.is_dir()

    assert store.db_path.exists()


def test_reference_round_trip_with_style_card(store: Store) -> None:
    ref = Reference(
        id="r1",
        source="board",
        source_url="https://example.com/x.png",
        label="Hero shot",
        category="hero",
        image_path="references/r1.png",
        content_hash="abc123",
        status="analysed",
        choice="liked",
        style_card=_style_card(mood=["calm"], technique="soft light"),
    )
    store.upsert_reference(ref)

    fetched = store.get_reference("r1")

    assert fetched == ref
    assert store.get_reference("missing") is None


def test_list_references_filters_and_order(store: Store) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Inserted out of chronological order, to prove the list isn't just insertion order.
    refs = [
        Reference(id="c", source="board", status="analysed", choice="liked", created_at=base + timedelta(seconds=2)),
        Reference(id="a", source="board", status="pending", choice="none", created_at=base),
        Reference(id="b", source="board", status="analysed", choice="disliked", created_at=base + timedelta(seconds=1)),
    ]
    for ref in refs:
        store.upsert_reference(ref)

    assert [ref.id for ref in store.list_references()] == ["a", "b", "c"]
    assert [ref.id for ref in store.list_references(status="analysed")] == ["b", "c"]
    assert [ref.id for ref in store.list_references(choice="liked")] == ["c"]
    assert [ref.id for ref in store.list_references(status="analysed", choice="disliked")] == ["b"]
    assert [ref.id for ref in store.list_references(status="pending", choice="liked")] == []


def test_choice_survives_a_new_store_instance(store: Store, settings: Settings) -> None:
    store.upsert_reference(Reference(id="r1", source="board"))
    store.set_choice("r1", "liked")

    second = Store(settings.db_path, settings.data_dir)
    fetched = second.get_reference("r1")

    assert fetched is not None
    assert fetched.choice == "liked"


def test_setters_raise_keyerror_for_unknown_ids(store: Store) -> None:
    with pytest.raises(KeyError):
        store.set_choice("missing", "liked")
    with pytest.raises(KeyError):
        store.set_style_card("missing", _style_card())
    with pytest.raises(KeyError):
        store.set_reference_status("missing", "failed")
    with pytest.raises(KeyError):
        store.approve_post("missing")
    with pytest.raises(KeyError):
        store.finish_run("missing", "succeeded")
    with pytest.raises(KeyError):
        store.end_step(999999, "succeeded")


def test_find_reference_by_hash(store: Store) -> None:
    store.upsert_reference(Reference(id="a", source="board", content_hash="hash-a"))
    store.upsert_reference(Reference(id="b", source="board", content_hash="hash-b"))

    found = store.find_reference_by_hash("hash-b")

    assert found is not None
    assert found.id == "b"
    assert store.find_reference_by_hash("no-such-hash") is None


def test_run_lifecycle(store: Store) -> None:
    run = store.create_run("create", "hybridge", brief="A new post")
    assert run.status == "running"

    first = store.start_step(run.id, "load_context")
    second = store.start_step(run.id, "art_director")
    assert isinstance(first, int)
    assert isinstance(second, int)
    assert second != first

    store.end_step(first, "succeeded", note="loaded taste and brief")
    store.end_step(second, "succeeded", provider="gemini", attempts=2, note="picked a layout")

    store.finish_run(run.id, "succeeded", post_id="post-1")

    events = store.list_events(run.id)
    assert [event.id for event in events] == [first, second]
    assert [event.step for event in events] == ["load_context", "art_director"]
    assert all(event.status == "succeeded" for event in events)
    assert all(event.ended_at is not None for event in events)
    assert events[0].note == "loaded taste and brief"
    assert events[1].provider == "gemini"
    assert events[1].attempts == 2

    finished = store.get_run(run.id)
    assert finished is not None
    assert finished.status == "succeeded"
    assert finished.post_id == "post-1"
    assert finished.error is None
    assert finished.finished_at is not None


def test_mark_interrupted_runs(store: Store) -> None:
    stays_running = store.create_run("create", "hybridge")
    other_running = store.create_run("create", "hybridge")
    finished = store.create_run("create", "hybridge")
    store.finish_run(finished.id, "succeeded")

    # list_runs returns newest first, regardless of status.
    assert [run.id for run in store.list_runs()] == [finished.id, other_running.id, stays_running.id]

    changed = store.mark_interrupted_runs()

    assert changed == 2
    for run_id in (stays_running.id, other_running.id):
        run = store.get_run(run_id)
        assert run is not None
        assert run.status == "interrupted"
        assert run.error == "The studio restarted while this run was in progress."
        assert run.finished_at is not None

    untouched = store.get_run(finished.id)
    assert untouched is not None
    assert untouched.status == "succeeded"
    assert untouched.error is None


def test_post_versions_and_approve(store: Store) -> None:
    v1 = _post(id="root-1", root_post_id="root-1", version=1, image_path="posts/root-1.png")
    v2 = _post(id="root-2", root_post_id="root-1", version=2, image_path="posts/root-2.png")
    store.save_post(v1)
    store.save_post(v2)

    versions = store.list_versions("root-1")
    assert [post.id for post in versions] == ["root-1", "root-2"]
    assert [post.version for post in versions] == [1, 2]

    # list_posts returns newest first.
    assert [post.id for post in store.list_posts()] == ["root-2", "root-1"]

    store.approve_post("root-2")

    approved = store.get_post("root-2")
    untouched = store.get_post("root-1")
    assert approved is not None and approved.status == "approved"
    assert untouched is not None and untouched.status == "draft"


def test_media_path_rejects_escape(store: Store) -> None:
    for bad in ("../secret", "/etc/passwd", "a/../../b"):
        with pytest.raises(ValueError):
            store.media_path(bad)

    resolved = store.media_path("posts/x.png")
    assert resolved == (store.data_dir / "posts" / "x.png").resolve()


def test_relative_rejects_outside_paths(store: Store, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere" / "file.png"
    with pytest.raises(ValueError):
        store.relative(outside)

    inside = store.data_dir / "posts" / "p1.png"
    assert store.relative(inside) == "posts/p1.png"


def test_datetimes_round_trip_timezone_aware(store: Store) -> None:
    created = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
    store.upsert_reference(Reference(id="r1", source="board", created_at=created))

    fetched = store.get_reference("r1")
    assert fetched is not None
    assert fetched.created_at == created
    assert fetched.created_at.tzinfo is not None
    assert fetched.created_at.utcoffset() == timedelta(0)

    run = store.create_run("create", "hybridge")
    store.finish_run(run.id, "succeeded")
    finished = store.get_run(run.id)
    assert finished is not None
    assert finished.created_at.tzinfo is not None
    assert finished.finished_at is not None
    assert finished.finished_at.tzinfo is not None
