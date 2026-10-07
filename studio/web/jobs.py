"""Starts run coroutines in the background, without losing them to garbage collection.

`asyncio.create_task` does not keep its task alive on its own: if nothing holds a
reference, the task can be collected before it finishes. `RunJobs` holds one until
it is done, then lets it go. Tasks are kept by run id so a run in flight can be
found again and cancelled (the session page's Stop button).
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any


class RunJobs:
    """Runs any workflow coroutine as a background task the studio keeps a hold of."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    def start(self, run_id: str, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coro)
        self._tasks[run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run_id, None))

    def cancel(self, run_id: str) -> bool:
        """Cancel the task running `run_id`, if it is still going. Returns whether it did.

        The task's own cancellation handling (every run ends through `settle`) is what
        actually marks the run interrupted and frees the session; this only requests it.
        """
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True
