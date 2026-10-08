"""Builds the studio's web app: every dependency, wired together, behind one FastAPI app.

`create_app` itself does nothing heavy; it only reads settings and builds the
FastAPI object, with the request guards in front of it. The store, the brand kit, the
model, the photo registry (the model catalogue, checked here, so a bad one stops the
studio), the search provider and the renderer are all built in the app's lifespan, which
runs once when the app starts and tears them down when it stops.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from studio.brand import load_brand_kit
from studio.config import Settings
from studio.contracts import Reference, StyleCard
from studio.library.analysis import AnalysisJob
from studio.library.base import ImageFetcher
from studio.library.fetch import HttpImageFetcher
from studio.models import get_llm
from studio.photos.catalogue import load_catalogue
from studio.photos.registry import PhotoRegistry
from studio.render import Renderer
from studio.research import get_search_provider
from studio.secrets import load_secret
from studio.store import Store
from studio.web.guard import LocalOnlyGuard
from studio.web.jobs import RunJobs
from studio.web.routes import router
from studio.workflows import Deps, analyse_reference

STATIC_DIR = Path(__file__).parent / "web" / "static"
# The HTTP clients' own loggers, kept at WARNING so request addresses and headers never reach
# the log (v6).
_QUIET_LOGGERS = ("httpx", "httpcore")

# Generous enough to cover every session a local, single-brand studio keeps.
_SESSION_SCAN_LIMIT = 10_000
# The last decision line of an auto run that the restart cut off.
_RESTARTED = "Stopped: The studio restarted while this run was in progress."


async def _no_pause(seconds: float) -> None:
    """Used instead of asyncio.sleep in demo mode, so the library analyses at once."""
    return None


def _clear_interrupted_sessions(store: Store) -> None:
    """Free any session left pointing at a run that `mark_interrupted_runs` just stopped.

    A run function always clears `active_run_id` itself when it ends, but a run killed by
    the process restarting never gets the chance. Without this, such a session would show
    as still working forever. An auto run would also have handed its session back to
    manual mode, with a line saying why it stopped, so that is done here too. A session
    whose scout run was cut off goes back to waiting for its brief, where "Research again"
    starts over (v4).
    """
    for session in store.list_sessions(limit=_SESSION_SCAN_LIMIT):
        if session.active_run_id is None:
            continue
        run = store.get_run(session.active_run_id)
        if run is not None and run.status == "interrupted":
            session.active_run_id = None
            if run.kind == "scout":
                session.status = "needs_brief"
            if session.mode == "auto":
                session.mode = "manual"
                if session.auto_state is not None:
                    decisions = [*session.auto_state.decisions, _RESTARTED]
                    session.auto_state = session.auto_state.model_copy(
                        update={"decisions": decisions, "stopped_by": "error"}
                    )
            store.save_session(session)


def create_app(settings: Settings | None = None, *, fetcher: ImageFetcher | None = None) -> FastAPI:
    """Build the studio's FastAPI app. Nothing heavy happens until the lifespan runs."""
    settings = settings or Settings.from_env()
    fetcher = fetcher or HttpImageFetcher()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = Store(settings.db_path, settings.data_dir)
        store.init()
        store.mark_interrupted_runs()
        _clear_interrupted_sessions(store)

        kit = load_brand_kit(settings.brands_dir, settings.brand_id)
        llm = get_llm(settings)
        # v6: every image model, from the catalogue, with the keys from .env and the saved ones.
        photos = PhotoRegistry(load_catalogue(), settings, store, secret=load_secret(settings))
        # A custom layout's image blocks and an uploaded logo must be files in the uploads folder.
        renderer = Renderer(store.work_dir, uploads_root=store.uploads_dir)
        await renderer.start()

        deps = Deps(
            settings=settings,
            store=store,
            kit=kit,
            llm=llm,
            photos=photos,
            renderer=renderer,
            search_provider=get_search_provider(settings),
        )

        async def analyse(ref: Reference, data: bytes, mime: str) -> StyleCard:
            return await analyse_reference(deps.llm, data, mime, label=ref.label)

        sleep = _no_pause if settings.demo_mode else asyncio.sleep
        analysis_job = AnalysisJob(store, analyse, per_minute=settings.analyse_per_minute, sleep=sleep)
        if store.list_references(status="pending"):
            analysis_job.start()

        app.state.settings = settings
        app.state.store = store
        app.state.deps = deps
        app.state.analysis_job = analysis_job
        app.state.fetcher = fetcher
        app.state.jobs = RunJobs()

        try:
            yield
        finally:
            await renderer.stop()
            aclose = getattr(fetcher, "aclose", None)
            if aclose is not None:
                await aclose()

    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    app = FastAPI(title="Design Studio", lifespan=lifespan)
    # v6: only this computer's names, and only the studio's own pages may change things.
    app.add_middleware(LocalOnlyGuard, port=settings.port)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(router)
    return app


app = create_app()
