"""What the session runs and the words revision share.

Each run builds its own workflow and runs it in an ADK session of its own, named after
the run (`run_workflow`). The steps reach the run's dependencies through a closure and
pass plain JSON data to each other through session state. `settle` is every run's way
out: it records how the run ended, and it never raises except to pass on a cancellation.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from google.adk import Context, Event, Runner, Workflow
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.sessions import InMemorySessionService
from google.adk.workflow._errors import DynamicNodeFailError
from google.genai import types
from pydantic import ValidationError

from studio.config import Settings
from studio.contracts import (
    BrandKit,
    DesignSpec,
    LayoutTemplate,
    Mode,
    Post,
    PromptDraft,
    PromptVersion,
    QualityBar,
    Run,
    SessionReference,
    StudioSession,
    new_id,
)
from studio.photos.base import PhotoProvider
from studio.render import CUSTOM_DOES_NOT_FIT, Renderer
from studio.research.base import SearchProvider
from studio.store import Store
from studio.web.uploads import agent_copy_path
from studio.workflows.steps import StepInfo, readable_error

logger = logging.getLogger(__name__)

T = TypeVar("T")

_APP_NAME = "studio"
_USER_ID = "studio"
_PROMPT_WRITER_ATTEMPTS = 2
_MAX_RETRY_NOTE_CHARS = 200
_MAX_HASHTAGS = 8
_MAX_PROMPT_WORDS = 120
# The sentence every photo prompt ends with; trimming keeps it.
_FIXED_ENDING = "No text, no logos, no watermarks."
# A word that ends a sentence, closing quotes or brackets allowed after the mark.
_SENTENCE_END_RE = re.compile(r"[.!?][\"')\]]*$")
# The kit's own quality bar, inside its brand folder.
_IDEAL_EXAMPLE = Path("examples") / "ideal-output.png"
# The setting that holds the designer's quality bar (v3.1).
QUALITY_BAR_SETTING = "quality_bar"
_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

DRAFT_INVALID = (
    f"The model's answer did not match the prompt draft after {_PROMPT_WRITER_ATTEMPTS} attempts."
)
EMPTY_ANSWER = "The answer was empty."
TEXT_DOES_NOT_FIT = "The text does not fit at the smallest size. Ask for shorter copy."
FITS_WITHOUT_CHANGES = "Fits without changes."
RUN_STOPPED = "The run was stopped before it finished."
NO_POST_SAVED = "The run finished without saving a post."
NO_PROMPT = "This session has no prompt yet."
TRIMMED = "Trimmed to 120 words."
# The line before the quality bar's image in a critic's message.
QUALITY_BAR = "The brand's quality bar."
QUALITY_BAR_SET_BY_YOU = "set by you from {label}"
QUALITY_BAR_KIT = "the kit's example"
QUALITY_BAR_NONE = "none"
# The line before each of the designer's references in an agent's message (v5), with the
# reference's note when it has one.
DESIGNER_REFERENCE = "Designer's reference {number}"
DESIGNER_REFERENCE_WITH_NOTE = "Designer's reference {number}: {note}"
# Ends the note of a step that sent the designer's references, "2 designer references".
DESIGNER_REFERENCES_SENT = " {references} sent."


class DraftInvalid(ValueError):
    """The prompt writer's answers could not be made into a prompt draft."""


@dataclass
class Deps:
    """Everything a run needs from outside the workflow."""

    settings: Settings
    store: Store
    kit: BrandKit
    llm: BaseLlm
    photo_provider: PhotoProvider | None
    renderer: Renderer
    # v4: the web search the scout runs through; None when research has no provider.
    search_provider: SearchProvider | None = None


# ------------------------------------------------------------------ running a run


async def settle(
    store: Store,
    run: Run,
    work: Callable[[], Awaitable[T]],
    session: StudioSession | None = None,
) -> T | None:
    """Do the run's work and record how it ended. Returns its result, or None when it failed.

    A failure is recorded on the run with a readable message. A cancellation is recorded
    as an interruption and raised again; that is the one case that raises. Either way the
    session, when the run has one, is left with no run in flight and with only the changes
    its finished steps saved.
    """
    try:
        result = await work()
        post_id = result.id if isinstance(result, Post) else None
        store.finish_run(run.id, "succeeded", post_id=post_id)
        return result
    except asyncio.CancelledError:
        # A cancelled task must end cancelled, so this is the one case that raises.
        _record_interruption(store, run.id)
        raise
    except Exception as error:
        _record_failure(store, run.id, error)
        return None
    finally:
        if session is not None:
            _release(store, session, run.id)


def _record_failure(store: Store, run_id: str, error: Exception) -> None:
    try:
        store.finish_run(run_id, "failed", error=readable_error(error))
    except Exception:
        logger.exception("Could not record the failure of run %s", run_id)


def _record_interruption(store: Store, run_id: str) -> None:
    # A failure to record it is logged, so the cancellation is still raised again.
    try:
        store.finish_run(run_id, "interrupted", error=RUN_STOPPED)
    except Exception:
        logger.exception("Could not record that run %s was stopped", run_id)


def _release(store: Store, session: StudioSession, run_id: str) -> None:
    """Clear the session's run in flight, unless a newer run has taken it over. Never raises."""
    try:
        latest = store.get_session(session.id) or session
        if latest.active_run_id == run_id:
            latest.active_run_id = None
            store.save_session(latest)
    except Exception:
        logger.exception("Could not clear the run in flight on session %s", session.id)


def update_session(store: Store, session: StudioSession, **changes: Any) -> StudioSession:
    """Save the changes on top of the session as last saved, so no other change is lost."""
    latest = store.get_session(session.id) or session
    updated = latest.model_copy(update=changes)
    store.save_session(updated)
    return updated


def current_version(store: Store, session: StudioSession) -> PromptVersion:
    """The session's current prompt version. Raises when the session has none."""
    version_id = session.current_prompt_version_id
    version = store.get_prompt_version(version_id) if version_id else None
    if version is None:
        raise ValueError(NO_PROMPT)
    return version


async def run_workflow(
    workflow: Workflow, run_id: str, message: str, state: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Run the workflow in an ADK session named after the run and return its final state.

    `state` starts the session with what an earlier workflow of the same run left, so a
    run made of two workflows can hand data from the first to the second. No step reads
    `message`: each step's input comes from the step before it.
    """
    sessions = InMemorySessionService()
    runner = Runner(node=workflow, app_name=_APP_NAME, session_service=sessions)
    await sessions.create_session(
        app_name=_APP_NAME, user_id=_USER_ID, session_id=run_id, state=dict(state or {})
    )
    async for _ in runner.run_async(
        user_id=_USER_ID, session_id=run_id, new_message=text_message(message)
    ):
        pass
    session = await sessions.get_session(app_name=_APP_NAME, user_id=_USER_ID, session_id=run_id)
    return dict(session.state) if session else {}


# --------------------------------------------------------------- the prompt writer


async def ask_prompt_writer(
    ctx: Context, agent: LlmAgent, message: types.Content, info: StepInfo
) -> PromptDraft:
    """The prompt writer's draft, asked for again once with the problem fed back.

    Only an answer that does not fit the prompt draft, or a blank one, is retried. Any
    other failure, such as the model service being down, fails the step as it is. The
    calling step must be a node with rerun_on_resume=True, as ADK requires of a step
    that runs another node.
    """
    for attempt in range(1, _PROMPT_WRITER_ATTEMPTS + 1):
        info.attempts = attempt
        try:
            answer = await ctx.run_node(agent, node_input=message)
        except DynamicNodeFailError as failure:
            if not isinstance(failure.error, ValidationError):
                raise failure.error from None
            problem = _first_validation_message(failure.error)
        else:
            if answer is not None:
                return PromptDraft.model_validate(answer)
            problem = EMPTY_ANSWER
        # The prompt writer's instruction reads this on the next attempt.
        ctx.state["retry_note"] = problem[:_MAX_RETRY_NOTE_CHARS]
    raise DraftInvalid(DRAFT_INVALID)


def _first_validation_message(error: ValidationError) -> str:
    """The first problem pydantic found, with the field it was found in."""
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"])
    return f"{where}: {first['msg']}" if where else first["msg"]


# ------------------------------------------------------------------ the post


async def render_post(
    deps: Deps,
    design_spec: dict[str, Any],
    photo_path: str | None,
    post_notes: list[str],
    info: StepInfo,
) -> Event:
    """Render the post's image, and hand its id, path, report and notes to the save step."""
    spec = DesignSpec.model_validate(design_spec)
    post_id = new_id()
    out_path = deps.store.posts_dir / f"{post_id}.png"
    photo = deps.store.media_path(photo_path) if photo_path else None
    report = await deps.renderer.render(spec, deps.kit, photo, out_path)
    info.note = "; ".join(report.adjustments) or FITS_WITHOUT_CHANGES
    # A custom layout's words are placed by hand, so the advice points to the editor.
    does_not_fit = CUSTOM_DOES_NOT_FIT if spec.layout == "custom" else TEXT_DOES_NOT_FIT
    notes = post_notes if report.fits else [*post_notes, does_not_fit]
    report_data = report.model_dump(mode="json")
    return Event(
        output=report_data,
        state={
            "post_id": post_id,
            "image_path": deps.store.relative(out_path),
            "render_report": report_data,
            "post_notes": notes,
        },
    )


def saved_post(store: Store, state: dict[str, Any]) -> Post:
    """The post the run saved, found through the id its render step put in state."""
    post_id = state.get("post_id")
    post = store.get_post(post_id) if post_id else None
    if post is None:
        raise RuntimeError(NO_POST_SAVED)
    return post


def place_in_history(store: Store, post_id: str, previous: Post | None) -> tuple[str, int]:
    """The root post id and version number for a new post: the next version after `previous`."""
    if previous is None:
        return post_id, 1
    versions = store.list_versions(previous.root_post_id)
    highest = max((post.version for post in versions), default=previous.version)
    return previous.root_post_id, highest + 1


# ------------------------------------------------------------------ small helpers


def brand_block(kit: BrandKit) -> dict[str, Any]:
    """The brand as the prompt writer reads it."""
    return {"name": kit.name, "audience": kit.audience, "feel": kit.feel, "rules": list(kit.rules)}


def with_backdrop(
    photo_prompt: str, mode: Mode, kit: BrandKit, layout_hint: LayoutTemplate = "hero"
) -> str:
    """The photo prompt, asking for a backdrop that melts into the post's background and a
    framing that leaves room for the layout's words.

    Only hero and type_only centre the subject. Full bleed and caption strip keep the bottom
    third plain for the words over the photo, corner puts the subject in the lower right, and
    split and custom add nothing.
    """
    background = kit.mode_hex(mode)["background"]
    backdrop = (
        f"{photo_prompt} Set on a seamless, evenly lit {mode} backdrop close to the "
        f"colour {background}"
    )
    if layout_hint in ("hero", "type_only"):
        return f"{backdrop}, with the subject centred and empty space around it."
    if layout_hint in ("full_bleed", "caption_strip"):
        return f"{backdrop}. Keep the bottom third of the frame plain, backdrop only."
    if layout_hint == "corner":
        return f"{backdrop}. Place the subject in the lower right of the frame."
    return f"{backdrop}."


def quality_bar(store: Store, kit: BrandKit) -> tuple[Path | None, str]:
    """The image every judgement is held to, and where it came from in a few words.

    The designer's choice (the `quality_bar` setting) while its file is still in the data
    folder; else the kit's ideal example while the kit has one; else no image, labelled
    "none". A setting that cannot be read counts as no choice.
    """
    data = store.get_setting(QUALITY_BAR_SETTING)
    if data is not None:
        try:
            chosen = QualityBar.model_validate(data)
        except ValidationError:
            logger.warning("The quality bar setting could not be read; using the kit's example")
        else:
            relative = existing_media(store, chosen.image_path)
            if relative is not None:
                label = chosen.label.strip()
                text = QUALITY_BAR_SET_BY_YOU.format(label=label) if label else "set by you"
                return store.media_path(relative), text
    ideal = Path(kit.root) / _IDEAL_EXAMPLE
    if ideal.is_file():
        return ideal, QUALITY_BAR_KIT
    return None, QUALITY_BAR_NONE


def quality_bar_parts(bar: Path | None) -> list[types.Part]:
    """The end of a critic's message: the quality bar's image after the line naming it.
    Empty when there is no quality bar."""
    return [types.Part(text=QUALITY_BAR), image_part(bar)] if bar is not None else []


# ------------------------------------------------------- the designer's references (v5)


def designer_references(store: Store, session_id: str | None) -> list[SessionReference]:
    """The session's references whose image is still in the data folder, oldest first; none
    without a session (a post can have none).

    Removing an upload in the Library takes its references off every session. A reference
    whose file has gone some other way is left out everywhere, so the session page, the six
    a session takes, the agents and the post page's count still agree.
    """
    if not session_id:
        return []
    return [
        reference
        for reference in store.list_session_references(session_id)
        if existing_media(store, reference.image_path) is not None
    ]


def designer_reference_label(number: int, note: str) -> str:
    """The line naming a designer's reference before its image: its number, and its note when
    it has one."""
    if note.strip():
        return DESIGNER_REFERENCE_WITH_NOTE.format(number=number, note=note.strip())
    return DESIGNER_REFERENCE.format(number=number)


def designer_reference_context(references: list[SessionReference]) -> list[dict[str, Any]]:
    """The references as an agent's context carries them, in their order and numbered from 1:
    each one's number, note and the analyst's card, None when no card could be written."""
    return [
        {
            "number": number,
            "note": reference.note,
            "card": reference.card.model_dump(mode="json") if reference.card else None,
        }
        for number, reference in enumerate(references, start=1)
    ]


def designer_reference_images(
    store: Store, references: list[SessionReference]
) -> list[dict[str, str]]:
    """Each reference's image with the line naming it, numbered as the context numbers them.
    The image is the reference's agent copy, upright and at most 2048 pixels on its longer
    side, or the upload itself when it has no copy."""
    return [
        {
            "label": designer_reference_label(number, reference.note),
            "path": str(_agent_image(store, reference.image_path)),
        }
        for number, reference in enumerate(references, start=1)
    ]


def _agent_image(store: Store, image_path: str) -> Path:
    """The file the models are sent for an upload: its agent copy, a JPEG or, for a picture
    with transparency, a PNG; or the upload itself, for a reference attached before the copies
    were made or one whose copy could not be written."""
    for transparent in (False, True):
        relative = existing_media(store, agent_copy_path(image_path, transparent=transparent))
        if relative is not None:
            return store.media_path(relative)
    return store.media_path(image_path)


def designer_references_sent(count: int) -> str:
    """The sentence a step's note gains when it sent `count` designer references, or ""."""
    if not count:
        return ""
    return DESIGNER_REFERENCES_SENT.format(references=plural(count, "designer reference"))


def plural(count: int, noun: str) -> str:
    """The count with its noun, which takes an "s" for any count but one: "1 round", "2 rounds"."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def trim_prompt(prompt: str) -> tuple[str, bool]:
    """The photo prompt cut to at most 120 words, and whether it was cut.

    The fixed ending ("No text, no logos, no watermarks."), when present, is set aside and
    put back at the end, so the cut never takes it. The rest is cut after its last sentence
    end within the words left (114 with the ending), or after the last of those words when
    they hold no sentence end. A prompt that fits is returned as it is.
    """
    if len(prompt.split()) <= _MAX_PROMPT_WORDS:
        return prompt, False
    ending = _FIXED_ENDING if _FIXED_ENDING in prompt else ""
    body = prompt.replace(_FIXED_ENDING, " ", 1) if ending else prompt
    kept = body.split()[: _MAX_PROMPT_WORDS - len(ending.split())]
    ends = [count for count, word in enumerate(kept, start=1) if _SENTENCE_END_RE.search(word)]
    cut = kept[: ends[-1] if ends else len(kept)]
    return " ".join([*cut, ending] if ending else cut), True


def mentions(text: str, term: str) -> bool:
    """Whether the text holds the term as whole words, in any case."""
    return re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE) is not None


def clean_hashtags(hashtags: list[str]) -> list[str]:
    """Each hashtag without spaces and starting with '#'; empty ones dropped; at most 8."""
    cleaned: list[str] = []
    for hashtag in hashtags:
        compact = "".join(hashtag.split())
        if not compact.strip("#"):
            continue
        cleaned.append(compact if compact.startswith("#") else f"#{compact}")
    return cleaned[:_MAX_HASHTAGS]


def existing_media(store: Store, relative: str | None) -> str | None:
    """`relative` when it names a file that still exists in the data folder."""
    if not relative:
        return None
    try:
        exists = store.media_path(relative).is_file()
    except ValueError:
        return None
    return relative if exists else None


def text_message(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


def with_images(text: str, images: list[dict[str, str]]) -> types.Content:
    """The text, then each image after a line naming it. With no images it is the same
    message as `text_message`."""
    return types.Content(role="user", parts=[types.Part(text=text), *labelled_parts(images)])


def labelled_parts(images: list[dict[str, str]]) -> list[types.Part]:
    """Each image (`{label, path}`) as two parts of a model message: the line naming it, then
    the image."""
    parts: list[types.Part] = []
    for image in images:
        parts += [types.Part(text=image["label"]), image_part(Path(image["path"]))]
    return parts


def image_part(path: Path) -> types.Part:
    """The image file as a part of a model message, its type taken from the file's suffix."""
    mime_type = _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")
    return types.Part.from_bytes(data=path.read_bytes(), mime_type=mime_type)
