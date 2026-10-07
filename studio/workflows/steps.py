"""Records each step of a run as a run event, which the run page shows as one row."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from studio.photos.base import PhotoUnavailable
from studio.store import Store

_MAX_ERROR_CHARS = 300

STEP_STOPPED = "The run stopped before this step finished."


@dataclass
class StepInfo:
    """What a step reports about itself. The step body fills it in as it goes."""

    provider: str = ""
    attempts: int = 1
    note: str = ""
    event_id: int = 0  # the run event the step writes to, for progress notes while it runs


class StepRecorder:
    """Opens a run event when a step starts and closes it when the step ends."""

    def __init__(self, store: Store, run_id: str) -> None:
        self._store = store
        self._run_id = run_id

    @asynccontextmanager
    async def step(self, name: str) -> AsyncIterator[StepInfo]:
        """Record the step around its body: succeeded on a normal exit, failed on an exception.

        A failure is recorded with a readable message and then raised again. A cancelled
        step is recorded as failed too, so it is not left running, and the cancellation
        is raised again.
        """
        event_id = self._store.start_step(self._run_id, name)
        info = StepInfo(event_id=event_id)
        try:
            yield info
        except asyncio.CancelledError:
            self._store.end_step(
                event_id,
                "failed",
                provider=info.provider,
                attempts=info.attempts,
                note=info.note,
                error=STEP_STOPPED,
            )
            raise
        except Exception as error:
            self._store.end_step(
                event_id,
                "failed",
                provider=info.provider,
                attempts=info.attempts,
                note=info.note,
                error=readable_error(error),
            )
            raise
        self._store.end_step(
            event_id,
            "succeeded",
            provider=info.provider,
            attempts=info.attempts,
            note=info.note,
        )


def readable_error(error: Exception) -> str:
    """The message a person sees for a failed step or run.

    The studio's own failures (ValueError, PhotoUnavailable) already carry a plain
    message. Anything else is unexpected, so its type is kept to help with debugging.
    """
    if isinstance(error, (ValueError, PhotoUnavailable)):
        return str(error)
    text = str(error)
    message = f"{type(error).__name__}: {text}" if text else type(error).__name__
    return message[:_MAX_ERROR_CHARS]
