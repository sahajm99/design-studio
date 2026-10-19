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
from collections.abc import AsyncIterator, Awaitable, Callable
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
from studio.web.guard import LocalOnlyGuard, PasscodeGate, WrongPasscodes, pass_key
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
# v7 hosting: why a studio with a passcode will not start without its secret.
_NO_PASS_SECRET = (
    "STUDIO_PASSCODE is set, but the studio has no secret to sign the sign-in cookie with. "
    "Fix STUDIO_SECRET_KEY, or let the studio write its secret in the database volume."
)


def _pacing(settings: Settings) -> Callable[[float], Awaitable[None]]:
    """The library analysis's pause between references: asyncio.sleep, except in demo mode,
    so the stand-in analyses at once. Asked at each pause (v7), since a key saved on the
    Settings page can end demo mode while the studio runs."""

    async def pause(seconds: float) -> None:
        if not settings.demo_mode:
            await asyncio.sleep(seconds)

    return pause


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
    """Build the studio's FastAPI app. Nothing heavy happens until the lifespan runs. v7: a
    hosting set-up that would put the studio online without a passcode stops it here."""
    settings = settings or Settings.from_env()
    problem = settings.hosting_problem()
    if problem:
        raise RuntimeError(problem)
    fetcher = fetcher or HttpImageFetcher()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = Store(settings.db_path, settings.data_dir)
        store.init()
        store.mark_interrupted_runs()
        _clear_interrupted_sessions(store)

        kit = load_brand_kit(settings.brands_dir, settings.brand_id)
        secret = load_secret(settings)
        # v7 hosting: the passcode gate signs its cookie with a key derived from the secret, so
        # without a secret the studio does not start rather than start with the gate open.
        if settings.passcode and secret is None:
            raise RuntimeError(_NO_PASS_SECRET)
        app.state.pass_key = pass_key(secret) if settings.passcode and secret else None
        app.state.wrong_passcodes = WrongPasscodes()
        # v6: every image model, from the catalogue, with the keys from .env and the saved ones.
        # v7: built first, since it hands the saved keys to the settings, the agents' and the
        # search's included, which the model and the search provider below are built with.
        photos = PhotoRegistry(load_catalogue(), settings, store, secret=secret)
        llm = get_llm(settings)
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

        analysis_job = AnalysisJob(
            store, analyse, per_minute=settings.analyse_per_minute, sleep=_pacing(settings)
        )
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
    # v7 hosting: the passcode gate, behind the Host and Origin checks (the middleware added
    # last runs first). Without a passcode it lets everything through. Keep this order:
    # swapped, a request with a foreign Host or Origin would reach the gate first.
    app.add_middleware(
        PasscodeGate, passcode=settings.passcode, public_host=settings.public_host
    )
    # v6: only this computer's names, and only the studio's own pages may change things.
    # v7: and the public host, on a server; with no passcode, nothing through a proxy.
    app.add_middleware(
        LocalOnlyGuard, public_host=settings.public_host, gated=bool(settings.passcode)
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(router)
    return app


app = create_app()
