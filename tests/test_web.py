"""Tests for the web app: pages, routes and the background wiring that runs them.

No network: a fake image fetcher stands in for HTTP fetches, and the studio's own
demo-mode fakes (FakeLlm, FakePhotoProvider) stand in for the model and the photo
service. Most tests share one app, and its one headless browser, for speed; the few
that need their own data or a second startup build their own app on an isolated
settings fixture.
"""

from __future__ import annotations

import io
import time
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image

# This container's Starlette warns (as a UserWarning subclass, not a
# DeprecationWarning, so pytest.ini's existing filters never catch it) that its
# TestClient's httpx transport is deprecated in favour of a package this project
# does not, and should not, take a new dependency on just to run tests.
# pytest.ini is read-only, so the filter is scoped to this module instead.
from starlette.exceptions import StarletteDeprecationWarning

with warnings.catch_warnings():
    warnings.simplefilter("ignore", StarletteDeprecationWarning)
    from starlette.testclient import TestClient

from studio.config import Settings
from studio.contracts import Reference
from studio.library.base import FetchedImage, SourceItem
from studio.library.sources import BoardHtmlSource
from studio.main import create_app
from studio.store import Store

REPO_ROOT = Path(__file__).resolve().parent.parent
BRANDS_DIR = REPO_ROOT / "brands"


class FakeFetcher:
    """Returns a small, distinct generated PNG for every item. No network, no disk reads."""

    def __init__(self) -> None:
        self._count = 0

    async def fetch(self, item: SourceItem) -> FetchedImage:
        self._count += 1
        colour = (self._count & 0xFF, (self._count >> 8) & 0xFF, (self._count >> 16) & 0xFF)
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), colour).save(buffer, format="PNG")
        return FetchedImage(data=buffer.getvalue(), mime_type="image/png")

    async def aclose(self) -> None:
        return None


# ------------------------------------------------------------------- helpers


def _wait_for_analysis_idle(client: TestClient, *, timeout: float = 60.0) -> None:
    """Poll /api/library/status until the analysis job is no longer running."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get("/api/library/status")
        assert response.status_code == 200
        if response.json()["progress"]["running"] is False:
            return
        time.sleep(0.05)
    raise AssertionError("The analysis job did not finish in time")


# --------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """One app, and its one headless browser, shared by every test that allows it."""
    base = tmp_path_factory.mktemp("web_shared")
    settings = Settings(
        data_dir=base / "data",
        db_path=base / "db" / "studio.db",
        brands_dir=BRANDS_DIR,
        brand_id="hybridge",
    )
    with TestClient(create_app(settings, fetcher=FakeFetcher())) as test_client:
        yield test_client


# ------------------------------------------------------------------------- pages


def test_pages_load(client: TestClient) -> None:
    for path in ("/studio", "/library", "/runs"):
        response = client.get(path)
        assert response.status_code == 200, path

    redirect = client.get("/", follow_redirects=False)
    assert redirect.status_code in (301, 302, 303, 307, 308)
    assert redirect.headers["location"] == "/studio"


def test_demo_banner_is_shown(client: TestClient) -> None:
    response = client.get("/studio")

    assert response.status_code == 200
    assert "Demo mode: no model key is set, so posts are written by a stand-in." in response.text
    # The shared test settings configure the fake photo provider, so this
    # second banner line must not also appear.
    assert "No photo source is set" not in response.text


# ------------------------------------------------------------------- the library


def test_import_board_adds_references(settings: Settings) -> None:
    with TestClient(create_app(settings, fetcher=FakeFetcher())) as client:
        kit = client.app.state.deps.kit
        expected = len(BoardHtmlSource(Path(kit.root) / kit.inspiration_board).items())

        response = client.post("/library/import", data={"source": "board"})

        assert response.status_code == 200
        assert f"Added {expected}" in response.text
        assert response.text.count('class="ref-card"') == expected

        _wait_for_analysis_idle(client)


def test_choice_updates_the_taste(settings: Settings) -> None:
    with TestClient(create_app(settings, fetcher=FakeFetcher())) as client:
        store = client.app.state.store
        store.inbox_dir.joinpath("sample.jpg").write_bytes(b"stand-in bytes, never decoded as an image")

        import_response = client.post("/library/import", data={"source": "inbox"})
        assert import_response.status_code == 200
        _wait_for_analysis_idle(client)

        (reference,) = store.list_references()
        assert reference.status == "analysed"

        choice_response = client.post(f"/api/references/{reference.id}/choice", json={"choice": "liked"})

        assert choice_response.status_code == 200
        assert choice_response.json()["summary"].startswith("Liked 1:")


def test_choice_survives_restart(settings: Settings) -> None:
    with TestClient(create_app(settings, fetcher=FakeFetcher())) as client:
        client.app.state.store.upsert_reference(Reference(id="keep-1", source="test", label="Keep me"))
        response = client.post("/api/references/keep-1/choice", json={"choice": "liked"})
        assert response.status_code == 200

    with TestClient(create_app(settings, fetcher=FakeFetcher())) as second_client:
        _wait_for_analysis_idle(second_client)  # start-up re-analyses the still-pending reference

        assert second_client.app.state.store.get_reference("keep-1").choice == "liked"
        page = second_client.get("/library")
        assert 'data-ref-id="keep-1" data-choice="liked"' in page.text


# -------------------------------------------------------------------------- media


def test_media_rejects_path_escape(client: TestClient) -> None:
    assert client.get("/media/../../etc/passwd").status_code == 404
    assert client.get("/media/%2e%2e/%2e%2e/x").status_code == 404


# ------------------------------------------------------------------------ 404s


def test_unknown_ids_are_404(client: TestClient) -> None:
    assert client.get("/runs/does-not-exist").status_code == 404
    assert client.get("/api/runs/does-not-exist").status_code == 404
    assert client.get("/posts/does-not-exist").status_code == 404
    assert client.post("/posts/does-not-exist/revise", data={"comment": "hi"}).status_code == 404
    assert client.post("/posts/does-not-exist/approve").status_code == 404
    assert client.post("/api/references/does-not-exist/choice", json={"choice": "liked"}).status_code == 404


# --------------------------------------------------------------------- start-up


def test_interrupted_runs_are_marked_on_startup(settings: Settings) -> None:
    setup_store = Store(settings.db_path, settings.data_dir)
    setup_store.init()
    run = setup_store.create_run("create", "hybridge", brief="Left running by a crash")

    with TestClient(create_app(settings, fetcher=FakeFetcher())) as client:
        response = client.get(f"/api/runs/{run.id}")

        assert response.status_code == 200
        data = response.json()["run"]
        assert data["status"] == "interrupted"
        assert data["error"] == "The studio restarted while this run was in progress."

        # The run page itself (not just the JSON) shows the same terminal state:
        # the error. (The v1 "Run again" button went with the one-click create run.)
        page = client.get(f"/runs/{run.id}")
        assert page.status_code == 200
        assert "The studio restarted while this run was in progress." in page.text
