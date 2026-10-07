"""Tests for AnalysisJob: paced analysis of pending references.

No network: `analyse` is always a stub here, never a real model call.
"""

from __future__ import annotations

from studio.contracts import Reference, StyleCard
from studio.library.analysis import AnalysisJob, AnalysisProgress
from studio.store import Store

_CARD = StyleCard(
    background="dark",
    layout="type_only",
    text_amount="none",
    subject="other",
    colour_count="one_or_two",
)


async def _no_sleep(seconds: float) -> None:
    return None


def _add_pending(store: Store, ref_id: str, *, label: str = "") -> Reference:
    """A pending reference with a real (tiny) image file already on disk."""
    path = store.references_dir / f"{ref_id}.jpg"
    path.write_bytes(b"stand-in bytes, never decoded as a real image in this test")
    ref = Reference(id=ref_id, source="board", label=label, image_path=store.relative(path), status="pending")
    store.upsert_reference(ref)
    return ref


async def test_run_once_analyses_all_pending(store: Store) -> None:
    refs = [_add_pending(store, f"r{i}") for i in range(3)]
    calls: list[tuple[str, bytes, str]] = []

    async def fake_analyse(ref: Reference, data: bytes, mime: str) -> StyleCard:
        calls.append((ref.id, data, mime))
        return _CARD

    sleeps: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    job = AnalysisJob(store, fake_analyse, per_minute=30, sleep=recording_sleep)

    progress = await job.run_once()

    assert [call[0] for call in calls] == [ref.id for ref in refs]
    assert all(mime == "image/jpeg" for _, _, mime in calls)
    assert sleeps == [2.0, 2.0]  # n - 1 = 2 calls, each 60 / per_minute = 60 / 30
    assert progress == AnalysisProgress(total=3, analysed=3, pending=0, failed=0, running=False)

    for ref in refs:
        stored = store.get_reference(ref.id)
        assert stored is not None
        assert stored.status == "analysed"
        assert stored.style_card == _CARD
        assert stored.error is None


async def test_one_failure_does_not_stop_the_rest(store: Store) -> None:
    _add_pending(store, "good-1")
    _add_pending(store, "bad-1")
    _add_pending(store, "good-2")

    async def flaky_analyse(ref: Reference, data: bytes, mime: str) -> StyleCard:
        if ref.id == "bad-1":
            raise RuntimeError("the model refused")
        return _CARD

    job = AnalysisJob(store, flaky_analyse, sleep=_no_sleep)

    progress = await job.run_once()

    assert progress.analysed == 2
    assert progress.failed == 1
    assert progress.pending == 0
    assert progress.total == 3

    failed = store.get_reference("bad-1")
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error == "the model refused"

    for ref_id in ("good-1", "good-2"):
        ok = store.get_reference(ref_id)
        assert ok is not None
        assert ok.status == "analysed"


async def test_missing_image_file_is_marked_failed(store: Store) -> None:
    # image_path points nowhere: the file was never written (or was removed).
    store.upsert_reference(
        Reference(id="ghost", source="board", image_path="references/ghost.jpg", status="pending")
    )
    called = False

    async def must_not_be_called(ref: Reference, data: bytes, mime: str) -> StyleCard:
        nonlocal called
        called = True
        return _CARD

    job = AnalysisJob(store, must_not_be_called, sleep=_no_sleep)
    await job.run_once()

    assert called is False
    stored = store.get_reference("ghost")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.error == "The cached image is missing."


def test_progress_counts(store: Store) -> None:
    store.upsert_reference(Reference(id="p1", source="board", status="pending"))
    store.upsert_reference(Reference(id="p2", source="board", status="pending"))
    store.upsert_reference(Reference(id="a1", source="board", status="analysed", style_card=_CARD))
    store.upsert_reference(Reference(id="f1", source="board", status="failed", error="boom"))
    store.upsert_reference(Reference(id="u1", source="board", status="unavailable", error="gone"))

    async def unused_analyse(ref: Reference, data: bytes, mime: str) -> StyleCard:
        raise AssertionError("progress() must not analyse anything")

    job = AnalysisJob(store, unused_analyse)

    progress = job.progress()

    # total excludes the unavailable reference; running is false with no task started.
    assert progress == AnalysisProgress(total=4, analysed=1, pending=2, failed=1, running=False)


async def test_references_added_during_a_pass_are_analysed(store: Store) -> None:
    refs = [_add_pending(store, f"r{i}") for i in range(2)]
    calls: list[str] = []
    added = False

    async def fake_analyse(ref: Reference, data: bytes, mime: str) -> StyleCard:
        nonlocal added
        calls.append(ref.id)
        if not added:
            added = True
            _add_pending(store, "latecomer")
        return _CARD

    sleeps: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    job = AnalysisJob(store, fake_analyse, per_minute=30, sleep=recording_sleep)

    progress = await job.run_once()

    expected_ids = {ref.id for ref in refs} | {"latecomer"}
    assert set(calls) == expected_ids
    assert len(calls) == len(expected_ids)  # analyse was called exactly once per reference
    assert len(sleeps) == len(calls) - 1
    assert sleeps == [2.0] * len(sleeps)  # 60 / per_minute = 60 / 30, never after the last call

    for ref_id in expected_ids:
        stored = store.get_reference(ref_id)
        assert stored is not None
        assert stored.status == "analysed"
        assert stored.style_card == _CARD

    assert progress == AnalysisProgress(total=3, analysed=3, pending=0, failed=0, running=False)


async def test_start_twice_returns_false(store: Store) -> None:
    _add_pending(store, "only-one")

    async def fake_analyse(ref: Reference, data: bytes, mime: str) -> StyleCard:
        return _CARD

    job = AnalysisJob(store, fake_analyse, sleep=_no_sleep)

    assert job.start() is True
    assert job.progress().running is True
    assert job.start() is False  # one is already scheduled

    await job._task  # let the scheduled run finish so nothing is left pending at teardown

    assert job.progress().running is False
    finished = store.get_reference("only-one")
    assert finished is not None
    assert finished.status == "analysed"
