"""The analyse workflow and the words revision, run end to end on the fakes.

No network and no key: the model is FakeLlm or a scripted model defined here, the
photos come from the fake provider, and the posts are rendered by the real renderer.
A revision's first post is made the v2.1 way, through a session's draft, samples,
compose and finish runs. Every test runs on the module's event loop, where the shared
Chromium lives.
"""

from __future__ import annotations

import io
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import replace
from typing import Any

import pytest
import pytest_asyncio
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from PIL import Image
from pydantic import PrivateAttr

from studio.config import Settings
from studio.contracts import (
    BrandKit,
    Post,
    PromptDraft,
    Run,
    RunEvent,
    StudioSession,
    StyleCard,
)
from studio.models import FakeLlm
from studio.photos.catalogue import load_catalogue
from studio.photos.registry import PhotoRegistry
from studio.render import Renderer
from studio.store import Store
from studio.workflows import (
    Deps,
    analyse_reference,
    run_compose,
    run_draft,
    run_finish,
    run_revise,
    run_samples,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")

# Long enough for the fake model to draft from (six words or more), and for it to
# shorten the headline, which keeps the first four words.
BRIEF = "Book a free consultation this week"


# ---------------------------------------------------------------- fixtures


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def renderer(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Renderer]:
    renderer = Renderer(tmp_path_factory.mktemp("renderer"))
    await renderer.start()
    yield renderer
    await renderer.stop()


@pytest.fixture
def deps(settings: Settings, store: Store, hybridge_kit: BrandKit, renderer: Renderer) -> Deps:
    return Deps(
        settings=settings,
        store=store,
        kit=hybridge_kit,
        llm=FakeLlm(),
        # Demo settings, so the stand-in makes every model's photos.
        photos=PhotoRegistry(load_catalogue(), settings, store),
        renderer=renderer,
    )


# ------------------------------------------------------------ stand-ins


class ScriptedLlm(BaseLlm):
    """Answers with canned replies in order and remembers each instruction it was given."""

    model: str = "scripted"
    replies: list[str]
    _instructions: list[str] = PrivateAttr(default_factory=list)

    @property
    def instructions(self) -> list[str]:
        return self._instructions

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        reply = self.replies[len(self._instructions)]
        self._instructions.append(str(llm_request.config.system_instruction or ""))
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=reply)]))


# ---------------------------------------------------------------- helpers


def draft_json(**changes: Any) -> str:
    """A valid prompt draft as the model would send it, with some fields changed."""
    fields: dict[str, Any] = {
        "photo_prompt": "A single white ceramic cup on a plain studio table.",
        "layout": "hero",
        "mode": "dark",
        "headline": "Smile with confidence again.",
        "subline": "Implant care, planned around you.",
        "caption": "Book a free consultation this week.",
        "hashtags": ["#smile"],
    }
    return PromptDraft(**{**fields, **changes}).model_dump_json()


async def finished_post(deps: Deps, brief: str = BRIEF) -> Post:
    """A first post made through a session: its draft, a round of one sample, that sample's
    layouts, and the first of them."""
    store, kit = deps.store, deps.kit
    session = StudioSession(brand_id=kit.id, brief=brief, sample_count=1)
    store.save_session(session)
    session = await run_draft(deps, store.create_run("draft", kit.id, brief=brief), session)
    (sample,) = await run_samples(deps, store.create_run("samples", kit.id), session)
    candidate, *_ = await run_compose(deps, store.create_run("compose", kit.id), session, sample)
    post = await run_finish(deps, store.create_run("finish", kit.id), session, candidate)
    assert post is not None
    return post


def revise_run(store: Store, kit: BrandKit, parent_post_id: str, comment: str) -> Run:
    return store.create_run("revise", kit.id, comment=comment, parent_post_id=parent_post_id)


def steps_of(store: Store, run_id: str) -> list[tuple[str, str]]:
    return [(event.step, event.status) for event in store.list_events(run_id)]


def event_for(store: Store, run_id: str, step: str) -> RunEvent:
    (event,) = [event for event in store.list_events(run_id) if event.step == step]
    return event


def png_bytes(colour: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), colour).save(buffer, format="PNG")
    return buffer.getvalue()


# ------------------------------------------------------------------ revise


async def test_revise_makes_version_two(deps: Deps) -> None:
    first = await finished_post(deps)
    assert first.photo_path is not None
    run = revise_run(deps.store, deps.kit, first.id, "make it shorter")

    second = await run_revise(deps, run)

    assert second is not None
    assert (second.version, second.spec.mode) == (2, first.spec.mode)
    assert (second.root_post_id, second.photo_path) == (first.root_post_id, first.photo_path)
    assert len(second.spec.headline) < len(first.spec.headline)
    assert second.brief == first.brief
    assert [post.id for post in deps.store.list_versions(first.id)] == [first.id, second.id]
    assert deps.store.get_run(run.id).status == "succeeded"


async def test_revision_with_a_blank_photo_prompt_keeps_its_photo(deps: Deps) -> None:
    first = await finished_post(deps)
    assert first.photo_path is not None and first.spec.photo_prompt
    reply = draft_json(layout="hero", mode=first.spec.mode, photo_prompt="")
    llm = ScriptedLlm(replies=[reply])
    run = revise_run(deps.store, deps.kit, first.id, "make the headline warmer")

    second = await run_revise(replace(deps, llm=llm), run)

    assert second is not None
    assert (second.spec.layout, second.spec.photo_prompt) == ("hero", first.spec.photo_prompt)
    assert second.photo_path == first.photo_path
    assert not any("type-only" in note for note in second.notes)
    assert deps.store.get_run(run.id).status == "succeeded"


async def test_revising_an_older_version_makes_the_next_version(deps: Deps) -> None:
    first = await finished_post(deps)
    second = await run_revise(deps, revise_run(deps.store, deps.kit, first.id, "make the text light"))
    assert second is not None

    third = await run_revise(deps, revise_run(deps.store, deps.kit, first.id, "make it dark"))

    assert third is not None
    assert (third.version, third.root_post_id) == (3, first.id)
    assert [post.version for post in deps.store.list_versions(first.id)] == [1, 2, 3]


async def test_revise_missing_parent_fails_cleanly(deps: Deps) -> None:
    run = revise_run(deps.store, deps.kit, "no-such-post", "make it light")

    post = await run_revise(deps, run)

    assert post is None
    finished = deps.store.get_run(run.id)
    assert (finished.status, finished.error) == ("failed", "The post to revise was not found.")
    assert steps_of(deps.store, run.id) == [("load_context", "failed")]
    load_context = event_for(deps.store, run.id, "load_context")
    assert load_context.error == "The post to revise was not found."


# ----------------------------------------------------------------- analyse


async def test_analyse_reference_returns_a_style_card() -> None:
    image = png_bytes((20, 30, 40))

    first = await analyse_reference(FakeLlm(), image, "image/png", label="Spring {launch}")
    again = await analyse_reference(FakeLlm(), image, "image/png", label="Spring {launch}")
    other = await analyse_reference(FakeLlm(), png_bytes((230, 220, 210)), "image/png")

    assert isinstance(first, StyleCard)
    assert first == again
    assert other != first


async def test_analyse_reference_fails_with_a_plain_message() -> None:
    llm = ScriptedLlm(replies=["This is not JSON.", '{"background": "neon"}'])

    # pydantic's ValidationError is also a ValueError, so the message is what tells them apart.
    with pytest.raises(ValueError) as failure:
        await analyse_reference(llm, png_bytes((20, 30, 40)), "image/png")

    assert str(failure.value) == "The model's answer did not match the style card after 2 attempts."
    assert len(llm.instructions) == 2
