"""SQLite-backed storage for references, runs, run events and posts.

Every object is stored as one row: a few plain columns used for filtering
and ordering, plus a `json` column holding the full `model_dump_json()`,
read back with `model_validate_json()`. The v5 tables, the brand's uploads and
the session references, keep each field in a column of its own instead. Each
call opens its own short-lived connection and closes it before returning; there
is no long-running connection and no WAL mode.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from studio.contracts import (
    Choice,
    LayoutCandidate,
    Post,
    PromptVersion,
    Reaction,
    Reference,
    RefStatus,
    RoundFeedback,
    RoundReview,
    Run,
    RunEvent,
    RunKind,
    RunStatus,
    Sample,
    SampleStatus,
    SessionReference,
    StepStatus,
    StudioSession,
    StyleCard,
    Upload,
    now,
)

_INTERRUPTED_ERROR = "The studio restarted while this run was in progress."
_INTERRUPTED_STEP_ERROR = "The studio restarted while this step was in progress."


class Store:
    """Reads and writes the studio's references, runs and posts."""

    def __init__(self, db_path: Path, data_dir: Path) -> None:
        self.db_path = db_path
        self.data_dir = data_dir
        self.references_dir = data_dir / "references"
        self.photos_dir = data_dir / "photos"
        self.posts_dir = data_dir / "posts"
        self.work_dir = data_dir / "work"
        self.inbox_dir = data_dir / "inbox"
        self.samples_dir = data_dir / "samples"
        # v2.1: rendered layout candidates, one subfolder per session.
        self.candidates_dir = data_dir / "compositions"
        # v3: photos the designer uploads into a session.
        self.uploads_dir = data_dir / "uploads"
        # v3.1: images set as the quality bar, copied so archive deletes never break it.
        self.quality_bar_dir = data_dir / "quality-bar"

    # ------------------------------------------------------------- setup

    def init(self) -> None:
        """Create the database, its tables and the nine data folders.

        Calling this more than once is harmless.
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        for folder in (
            self.references_dir,
            self.photos_dir,
            self.posts_dir,
            self.work_dir,
            self.inbox_dir,
            self.samples_dir,
            self.candidates_dir,
            self.uploads_dir,
            self.quality_bar_dir,
        ):
            folder.mkdir(parents=True, exist_ok=True)

        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS refs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    choice TEXT NOT NULL,
                    content_hash TEXT,
                    created_at REAL NOT NULL,
                    json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS posts (
                    id TEXT PRIMARY KEY,
                    root_post_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS prompt_versions (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS samples (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    round INTEGER NOT NULL,
                    idx INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    reaction TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS round_feedback (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    round INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS round_reviews (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    round INTEGER NOT NULL,
                    json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS layout_candidates (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    sample_id TEXT NOT NULL,
                    idx INTEGER NOT NULL,
                    json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, json TEXT NOT NULL)"
            )
            # v5: the images the designer uploaded under the brand, and the references a
            # session carries. A reference's card is its StyleCard as JSON, or NULL.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY,
                    brand_id TEXT,
                    image_path TEXT,
                    name TEXT,
                    width INTEGER,
                    height INTEGER,
                    source TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_references (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    upload_id TEXT,
                    image_path TEXT,
                    note TEXT,
                    card TEXT,
                    created_at TEXT
                )
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """A fresh connection, committed on a clean exit and always closed."""
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------- paths

    def relative(self, path: Path) -> str:
        """`path` relative to the data folder, with forward slashes."""
        base = self.data_dir.resolve()
        try:
            return path.resolve().relative_to(base).as_posix()
        except ValueError as error:
            raise ValueError(f"Path '{path}' is outside the data folder") from error

    def media_path(self, relative: str) -> Path:
        """Resolve a relative path under the data folder.

        Raises ValueError if the result would escape the data folder
        (`../x`, an absolute path, `a/../../b`). Does not check that the
        file exists.
        """
        base = self.data_dir.resolve()
        resolved = (base / relative).resolve()
        if resolved != base and base not in resolved.parents:
            raise ValueError(f"Path '{relative}' escapes the data folder")
        return resolved

    # -------------------------------------------------------- references

    def upsert_reference(self, ref: Reference) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO refs (id, status, choice, content_hash, created_at, json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    ref.id,
                    ref.status,
                    ref.choice,
                    ref.content_hash,
                    ref.created_at.timestamp(),
                    ref.model_dump_json(),
                ),
            )

    def get_reference(self, ref_id: str) -> Reference | None:
        with self._connect() as conn:
            row = conn.execute("SELECT json FROM refs WHERE id = ?", (ref_id,)).fetchone()
        return Reference.model_validate_json(row[0]) if row else None

    def find_reference_by_hash(self, content_hash: str) -> Reference | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT json FROM refs WHERE content_hash = ? LIMIT 1", (content_hash,)
            ).fetchone()
        return Reference.model_validate_json(row[0]) if row else None

    def list_references(
        self, *, status: RefStatus | None = None, choice: Choice | None = None
    ) -> list[Reference]:
        clauses: list[str] = []
        params: list[str] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if choice is not None:
            clauses.append("choice = ?")
            params.append(choice)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT json FROM refs {where} ORDER BY created_at ASC, id ASC", params
            ).fetchall()
        return [Reference.model_validate_json(row[0]) for row in rows]

    def set_choice(self, ref_id: str, choice: Choice) -> None:
        ref = self._require_reference(ref_id)
        ref.choice = choice
        self.upsert_reference(ref)

    def set_style_card(self, ref_id: str, card: StyleCard) -> None:
        ref = self._require_reference(ref_id)
        ref.style_card = card
        ref.status = "analysed"
        ref.error = None
        self.upsert_reference(ref)

    def set_reference_status(self, ref_id: str, status: RefStatus, error: str | None = None) -> None:
        ref = self._require_reference(ref_id)
        ref.status = status
        ref.error = error
        self.upsert_reference(ref)

    def _require_reference(self, ref_id: str) -> Reference:
        ref = self.get_reference(ref_id)
        if ref is None:
            raise KeyError(ref_id)
        return ref

    # -------------------------------------------------------------- runs

    def create_run(
        self,
        kind: RunKind,
        brand_id: str,
        *,
        brief: str = "",
        comment: str = "",
        parent_post_id: str | None = None,
        session_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> Run:
        run = Run(
            kind=kind,
            brand_id=brand_id,
            brief=brief,
            comment=comment,
            parent_post_id=parent_post_id,
            session_id=session_id,
            parent_run_id=parent_run_id,
        )
        self._save_run(run)
        return run

    def finish_run(
        self, run_id: str, status: RunStatus, *, error: str | None = None, post_id: str | None = None
    ) -> None:
        run = self._require_run(run_id)
        run.status = status
        run.error = error
        run.post_id = post_id
        run.finished_at = now()
        self._save_run(run)

    def get_run(self, run_id: str) -> Run | None:
        with self._connect() as conn:
            row = conn.execute("SELECT json FROM runs WHERE id = ?", (run_id,)).fetchone()
        return Run.model_validate_json(row[0]) if row else None

    def list_runs(self, limit: int = 50) -> list[Run]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT json FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Run.model_validate_json(row[0]) for row in rows]

    def mark_interrupted_runs(self) -> int:
        """Mark every run still `running` as interrupted, after a restart stopped it.

        v3.1: each of its steps still running ends as failed, with the restart error, so the
        run page never shows a step working forever. Returns how many runs were marked.
        """
        with self._connect() as conn:
            rows = conn.execute("SELECT json FROM runs WHERE status = 'running'").fetchall()

        for (raw,) in rows:
            run = Run.model_validate_json(raw)
            run.status = "interrupted"
            run.error = _INTERRUPTED_ERROR
            run.finished_at = now()
            self._save_run(run)
            self._end_interrupted_steps(run.id)
        return len(rows)

    def _save_run(self, run: Run) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs (id, status, created_at, json) VALUES (?, ?, ?, ?)",
                (run.id, run.status, run.created_at.timestamp(), run.model_dump_json()),
            )

    def _require_run(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    # ------------------------------------------------------- run events

    def start_step(self, run_id: str, step: str) -> int:
        event = RunEvent(run_id=run_id, step=step)
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO run_events (run_id, json) VALUES (?, ?)",
                (run_id, event.model_dump_json()),
            )
            event_id = cursor.lastrowid
            assert event_id is not None
            event.id = event_id
            conn.execute(
                "UPDATE run_events SET json = ? WHERE id = ?", (event.model_dump_json(), event_id)
            )
        return event_id

    def end_step(
        self,
        event_id: int,
        status: StepStatus,
        *,
        provider: str = "",
        attempts: int = 1,
        note: str = "",
        error: str | None = None,
    ) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT json FROM run_events WHERE id = ?", (event_id,)).fetchone()
            if row is None:
                raise KeyError(event_id)
            event = RunEvent.model_validate_json(row[0])
            event.status = status
            event.ended_at = now()
            event.provider = provider
            event.attempts = attempts
            event.note = note
            event.error = error
            conn.execute(
                "UPDATE run_events SET json = ? WHERE id = ?", (event.model_dump_json(), event_id)
            )

    def update_step_note(self, event_id: int, note: str) -> None:
        """Update a run event's note in place, leaving its status and timing untouched.

        Used to post progress on a step still running (`store.update_step_note`).
        Raises `KeyError` if the event does not exist.
        """
        with self._connect() as conn:
            row = conn.execute("SELECT json FROM run_events WHERE id = ?", (event_id,)).fetchone()
            if row is None:
                raise KeyError(event_id)
            event = RunEvent.model_validate_json(row[0])
            event.note = note
            conn.execute(
                "UPDATE run_events SET json = ? WHERE id = ?", (event.model_dump_json(), event_id)
            )

    def list_events(self, run_id: str) -> list[RunEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT json FROM run_events WHERE run_id = ? ORDER BY id ASC", (run_id,)
            ).fetchall()
        return [RunEvent.model_validate_json(row[0]) for row in rows]

    def _end_interrupted_steps(self, run_id: str) -> None:
        """End every step of `run_id` still running as failed, with the restart error."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, json FROM run_events "
                "WHERE run_id = ? AND json_extract(json, '$.status') = 'running'",
                (run_id,),
            ).fetchall()
            for event_id, raw in rows:
                event = RunEvent.model_validate_json(raw)
                event.status = "failed"
                event.ended_at = now()
                event.error = _INTERRUPTED_STEP_ERROR
                conn.execute(
                    "UPDATE run_events SET json = ? WHERE id = ?",
                    (event.model_dump_json(), event_id),
                )

    # ------------------------------------------------------------- posts

    def save_post(self, post: Post) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO posts (id, root_post_id, version, created_at, json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    post.id,
                    post.root_post_id,
                    post.version,
                    post.created_at.timestamp(),
                    post.model_dump_json(),
                ),
            )

    def get_post(self, post_id: str) -> Post | None:
        with self._connect() as conn:
            row = conn.execute("SELECT json FROM posts WHERE id = ?", (post_id,)).fetchone()
        return Post.model_validate_json(row[0]) if row else None

    def list_posts(self, limit: int = 50) -> list[Post]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT json FROM posts ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Post.model_validate_json(row[0]) for row in rows]

    def list_versions(self, root_post_id: str) -> list[Post]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT json FROM posts WHERE root_post_id = ? ORDER BY version ASC",
                (root_post_id,),
            ).fetchall()
        return [Post.model_validate_json(row[0]) for row in rows]

    def approve_post(self, post_id: str) -> None:
        post = self.get_post(post_id)
        if post is None:
            raise KeyError(post_id)
        post.status = "approved"
        self.save_post(post)

    # ------------------------------------------------------- sessions (v2)

    def save_session(self, session: StudioSession) -> None:
        """Insert or replace, always refreshing `updated_at` to now."""
        session.updated_at = now()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sessions (id, status, created_at, json) "
                "VALUES (?, ?, ?, ?)",
                (
                    session.id,
                    session.status,
                    session.created_at.timestamp(),
                    session.model_dump_json(),
                ),
            )

    def get_session(self, session_id: str) -> StudioSession | None:
        with self._connect() as conn:
            row = conn.execute("SELECT json FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return StudioSession.model_validate_json(row[0]) if row else None

    def list_sessions(self, limit: int = 50) -> list[StudioSession]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT json FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [StudioSession.model_validate_json(row[0]) for row in rows]

    # ----------------------------------------------------- prompt versions

    def save_prompt_version(self, version: PromptVersion) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO prompt_versions (id, session_id, number, json) "
                "VALUES (?, ?, ?, ?)",
                (version.id, version.session_id, version.number, version.model_dump_json()),
            )

    def get_prompt_version(self, version_id: str) -> PromptVersion | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT json FROM prompt_versions WHERE id = ?", (version_id,)
            ).fetchone()
        return PromptVersion.model_validate_json(row[0]) if row else None

    def list_prompt_versions(self, session_id: str) -> list[PromptVersion]:
        """Versions for a session, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT json FROM prompt_versions WHERE session_id = ? ORDER BY number ASC",
                (session_id,),
            ).fetchall()
        return [PromptVersion.model_validate_json(row[0]) for row in rows]

    # ----------------------------------------------------------- samples

    def save_sample(self, sample: Sample) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO samples "
                "(id, session_id, round, idx, status, reaction, created_at, json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sample.id,
                    sample.session_id,
                    sample.round,
                    sample.index,
                    sample.status,
                    sample.reaction,
                    sample.created_at.timestamp(),
                    sample.model_dump_json(),
                ),
            )

    def get_sample(self, sample_id: str) -> Sample | None:
        with self._connect() as conn:
            row = conn.execute("SELECT json FROM samples WHERE id = ?", (sample_id,)).fetchone()
        return Sample.model_validate_json(row[0]) if row else None

    def list_samples(self, session_id: str) -> list[Sample]:
        """Samples for a session, excluding deleted ones, by round then index."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT json FROM samples WHERE session_id = ? AND status != 'deleted' "
                "ORDER BY round ASC, idx ASC",
                (session_id,),
            ).fetchall()
        return [Sample.model_validate_json(row[0]) for row in rows]

    def list_archive(
        self,
        *,
        reaction: Reaction | None = None,
        status: SampleStatus | None = None,
        limit: int = 500,
    ) -> list[Sample]:
        """Samples across every session, excluding deleted ones, newest first.

        `reaction` and `status`, when given, combine with AND.
        """
        clauses: list[str] = ["status != 'deleted'"]
        params: list[str | int] = []
        if reaction is not None:
            clauses.append("reaction = ?")
            params.append(reaction)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT json FROM samples WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [Sample.model_validate_json(row[0]) for row in rows]

    def delete_sample(self, sample_id: str) -> bool:
        """Soft-delete a sample: drop its image file and layout candidates, but keep the row
        for the archive.

        A photo reused from the archive shares its file with the sample it was copied from,
        so the file goes only when no other live sample points at it. Raises `KeyError` if
        the sample does not exist. Returns False and changes nothing if the sample, or any
        sample sharing its file, is used in a post.
        """
        sample = self._require_sample(sample_id)
        if sample.used_in_post_id is not None:
            return False
        if sample.image_path is not None:
            sharing = [
                other for other in self._samples_with_image(sample.image_path) if other.id != sample.id
            ]
            if any(other.used_in_post_id is not None for other in sharing):
                return False
            if not any(other.status != "deleted" for other in sharing):
                self.media_path(sample.image_path).unlink(missing_ok=True)
        self.delete_layout_candidates(sample.session_id, sample.id)
        sample.status = "deleted"
        sample.deleted_at = now()
        self.save_sample(sample)
        return True

    def _samples_with_image(self, image_path: str) -> list[Sample]:
        """Every sample row, deleted or not, whose image is the file at `image_path`."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT json FROM samples WHERE json_extract(json, '$.image_path') = ?",
                (image_path,),
            ).fetchall()
        return [Sample.model_validate_json(row[0]) for row in rows]

    def count_photos_since(self, since: datetime) -> int:
        """Count of samples with an image, including deleted ones, created at or after `since`.

        Photos the designer uploaded are left out: the count is of photos made.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM samples "
                "WHERE created_at >= ? AND json_extract(json, '$.image_path') IS NOT NULL "
                "AND json_extract(json, '$.provider') != 'upload'",
                (since.timestamp(),),
            ).fetchone()
        return row[0]

    def _require_sample(self, sample_id: str) -> Sample:
        sample = self.get_sample(sample_id)
        if sample is None:
            raise KeyError(sample_id)
        return sample

    # ------------------------------------------------------- round feedback

    def save_round_feedback(self, feedback: RoundFeedback) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO round_feedback (id, session_id, round, created_at, json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    feedback.id,
                    feedback.session_id,
                    feedback.round,
                    feedback.created_at.timestamp(),
                    feedback.model_dump_json(),
                ),
            )

    def list_round_feedback(self, session_id: str) -> list[RoundFeedback]:
        """Feedback for a session, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT json FROM round_feedback WHERE session_id = ? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
        return [RoundFeedback.model_validate_json(row[0]) for row in rows]

    # ----------------------------------------------------- round reviews (v2.1)

    def save_round_review(self, review: RoundReview) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO round_reviews (id, session_id, round, json) "
                "VALUES (?, ?, ?, ?)",
                (review.id, review.session_id, review.round, review.model_dump_json()),
            )

    def get_round_review(self, session_id: str, round: int) -> RoundReview | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT json FROM round_reviews WHERE session_id = ? AND round = ?",
                (session_id, round),
            ).fetchone()
        return RoundReview.model_validate_json(row[0]) if row else None

    # ------------------------------------------------- layout candidates (v2.1)

    def save_layout_candidate(self, candidate: LayoutCandidate) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO layout_candidates (id, session_id, sample_id, idx, json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    candidate.id,
                    candidate.session_id,
                    candidate.sample_id,
                    candidate.index,
                    candidate.model_dump_json(),
                ),
            )

    def get_layout_candidate(self, candidate_id: str) -> LayoutCandidate | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT json FROM layout_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        return LayoutCandidate.model_validate_json(row[0]) if row else None

    def list_layout_candidates(
        self, session_id: str, sample_id: str | None = None
    ) -> list[LayoutCandidate]:
        """Candidates for a session, optionally narrowed to one sample, by index."""
        clauses = ["session_id = ?"]
        params: list[str] = [session_id]
        if sample_id is not None:
            clauses.append("sample_id = ?")
            params.append(sample_id)

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT json FROM layout_candidates WHERE {' AND '.join(clauses)} "
                "ORDER BY idx ASC",
                params,
            ).fetchall()
        return [LayoutCandidate.model_validate_json(row[0]) for row in rows]

    def delete_layout_candidates(self, session_id: str, sample_id: str) -> int:
        """Delete every layout candidate for a sample: their image files, then their rows.

        Returns how many were deleted. Not an error when there are none.
        """
        candidates = self.list_layout_candidates(session_id, sample_id)
        for candidate in candidates:
            self.media_path(candidate.image_path).unlink(missing_ok=True)

        with self._connect() as conn:
            conn.execute(
                "DELETE FROM layout_candidates WHERE session_id = ? AND sample_id = ?",
                (session_id, sample_id),
            )
        return len(candidates)

    # ------------------------------------------- runs and posts (v2 reads)

    def list_runs_for_session(self, session_id: str) -> list[Run]:
        """Runs belonging to a session, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT json FROM runs WHERE json_extract(json, '$.session_id') = ? "
                "ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
        return [Run.model_validate_json(row[0]) for row in rows]

    def list_runs_for_parent(self, run_id: str) -> list[Run]:
        """Runs that are stages of the auto run `run_id`, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT json FROM runs WHERE json_extract(json, '$.parent_run_id') = ? "
                "ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
        return [Run.model_validate_json(row[0]) for row in rows]

    def recent_headlines(self, brand_id: str, limit: int = 10) -> list[str]:
        """Headlines of the newest posts for a brand, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT json_extract(json, '$.spec.headline') FROM posts "
                "WHERE json_extract(json, '$.brand_id') = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (brand_id, limit),
            ).fetchall()
        return [row[0] for row in rows]

    # ---------------------------------------------------------- settings (v3.1)

    def get_setting(self, key: str) -> dict | None:
        """The JSON object stored under `key`, or None when nothing is."""
        with self._connect() as conn:
            row = conn.execute("SELECT json FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_setting(self, key: str, value: BaseModel | None) -> None:
        """Store `value` under `key` as its JSON, replacing what was there; None deletes the row."""
        with self._connect() as conn:
            if value is None:
                conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, json) VALUES (?, ?)",
                    (key, value.model_dump_json()),
                )

    # ----------------------------------------------------- search credits (v4)

    def add_search_credits(self, n: int) -> int:
        """Add `n` search credits to this UTC month's count and return the new count.

        Kept in the settings table as `{"credits": n}` under `search_credits:{YYYY-MM}`, so
        a new month starts from 0. One statement adds, so two runs never lose a credit.
        """
        key = _search_credits_key()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, json) VALUES (?, json_object('credits', ?)) "
                "ON CONFLICT(key) DO UPDATE SET json = json_object('credits', "
                "COALESCE(json_extract(settings.json, '$.credits'), 0) + ?)",
                (key, n, n),
            )
            row = conn.execute(
                "SELECT json_extract(json, '$.credits') FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return int(row[0] or 0)

    def search_credits_this_month(self) -> int:
        """The search credits spent this UTC month; 0 before the month's first search."""
        data = self.get_setting(_search_credits_key())
        return int((data or {}).get("credits") or 0)

    # ---------------------------------------------------- the brand's uploads (v5)

    def add_upload(self, upload: Upload) -> None:
        """Record an upload whose file is already under the brand's uploads folder."""
        with self._connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO uploads ({_UPLOAD_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    upload.id,
                    upload.brand_id,
                    upload.image_path,
                    upload.name,
                    upload.width,
                    upload.height,
                    upload.source,
                    _timestamp(upload.created_at),
                ),
            )

    def get_upload(self, upload_id: str) -> Upload | None:
        """The upload with this id, or None when there is none."""
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_UPLOAD_COLUMNS} FROM uploads WHERE id = ?", (upload_id,)
            ).fetchone()
        return _upload_from_row(row) if row else None

    def list_uploads(self, brand_id: str) -> list[Upload]:
        """The brand's uploads, newest first; two made in the same instant, the later one first."""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_UPLOAD_COLUMNS} FROM uploads WHERE brand_id = ? "
                "ORDER BY created_at DESC, rowid DESC",
                (brand_id,),
            ).fetchall()
        return [_upload_from_row(row) for row in rows]

    def delete_upload(self, upload_id: str) -> None:
        """Delete the upload's row. The caller removes its file. Not an error when it is gone."""
        with self._connect() as conn:
            conn.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))

    # ------------------------------------------------- session references (v5)

    def add_session_reference(self, reference: SessionReference) -> None:
        """Record a reference on its session."""
        with self._connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO session_references ({_REFERENCE_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    reference.id,
                    reference.session_id,
                    reference.upload_id,
                    reference.image_path,
                    reference.note,
                    _card_json(reference.card),
                    _timestamp(reference.created_at),
                ),
            )

    def list_session_references(self, session_id: str) -> list[SessionReference]:
        """The session's references, oldest first, so they keep the order they were given in."""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_REFERENCE_COLUMNS} FROM session_references WHERE session_id = ? "
                "ORDER BY created_at ASC, rowid ASC",
                (session_id,),
            ).fetchall()
        return [_reference_from_row(row) for row in rows]

    def update_session_reference(self, reference: SessionReference) -> None:
        """Save a reference's note and card; its other fields never change."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE session_references SET note = ?, card = ? WHERE id = ?",
                (reference.note, _card_json(reference.card), reference.id),
            )

    def delete_session_reference(self, reference_id: str) -> None:
        """Delete a reference's row. Its upload stays on the brand's shelf."""
        with self._connect() as conn:
            conn.execute("DELETE FROM session_references WHERE id = ?", (reference_id,))

    def delete_session_references_for_upload(self, upload_id: str) -> int:
        """Delete every session's reference to this upload, as the upload leaves the brand's
        shelf, and give back how many rows went."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM session_references WHERE upload_id = ?", (upload_id,)
            )
            return cursor.rowcount


def _search_credits_key() -> str:
    """The settings key for this UTC month's search credits, e.g. `search_credits:2026-10`."""
    return f"search_credits:{now():%Y-%m}"


# The v5 tables' columns, in the order their rows are written and read.
_UPLOAD_COLUMNS = "id, brand_id, image_path, name, width, height, source, created_at"
_REFERENCE_COLUMNS = "id, session_id, upload_id, image_path, note, card, created_at"


def _timestamp(moment: datetime) -> str:
    """A moment as the v5 tables keep it: ISO 8601 with microseconds, so the text sorts in time."""
    return moment.isoformat(timespec="microseconds")


def _card_json(card: StyleCard | None) -> str | None:
    """A reference's card as its JSON, or None for a reference without one."""
    return card.model_dump_json() if card is not None else None


def _upload_from_row(row: tuple) -> Upload:
    """An uploads row, in `_UPLOAD_COLUMNS` order, as an Upload."""
    upload_id, brand_id, image_path, name, width, height, source, created_at = row
    return Upload(
        id=upload_id,
        brand_id=brand_id,
        image_path=image_path,
        name=name or "",
        width=width or 0,
        height=height or 0,
        source=source,
        created_at=datetime.fromisoformat(created_at),
    )


def _reference_from_row(row: tuple) -> SessionReference:
    """A session_references row, in `_REFERENCE_COLUMNS` order, as a SessionReference."""
    reference_id, session_id, upload_id, image_path, note, card, created_at = row
    return SessionReference(
        id=reference_id,
        session_id=session_id,
        upload_id=upload_id,
        image_path=image_path,
        note=note or "",
        card=StyleCard.model_validate_json(card) if card else None,
        created_at=datetime.fromisoformat(created_at),
    )
