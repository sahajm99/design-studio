"""The words revision: the designer's comment on a post, followed where it points.

The prompt writer reads the comment and says what it asks to change: the words, the photo
or both. A comment about the words makes the next version of the post, with the previous
version's photo, since a revision never makes a photo. A comment about the photo goes to
the post's session instead: it is saved as feedback on the latest round and the session's
prompt is revised from it. A comment about both does both. A post with no session takes
every comment as being about the words.

Code fixes the order of the steps. The run is two workflows: the words step always runs,
and the post is rendered and saved only when the comment is about the words.
"""

from __future__ import annotations

import re
from typing import Any

from google.adk import Context, Event, Workflow
from google.adk.workflow import node

from studio.contracts import DesignSpec, Post, PromptDraft, RenderReport, RoundFeedback, Run
from studio.models import describe_llm
from studio.render import NEEDS_PHOTO, allowed_templates
from studio.store import Store
from studio.workflows.agents import build_prompt_writer
from studio.workflows.session import run_revise_prompt
from studio.workflows.shared import (
    Deps,
    ask_prompt_writer,
    brand_block,
    clean_hashtags,
    designer_reference_context,
    designer_reference_images,
    designer_references,
    designer_references_sent,
    existing_media,
    place_in_history,
    render_post,
    run_workflow,
    saved_post,
    settle,
    update_session,
    with_images,
)
from studio.workflows.steps import StepRecorder

PARENT_NOT_FOUND = "The post to revise was not found."
SESSION_NOT_FOUND = "The post's session was not found."
NO_PHOTO_TO_REUSE = "The previous version has no photo, so the type-only layout was used."
SENT_TO_SESSION = "The comment asks for a new photo, so it went to the session."
READS_AS_PHOTO = "The comment reads as a photo request."
SESSION_BUSY = "The session is busy; add the comment there when it is free."


def _cue_pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    """Any of the words as a whole word, in any case, a plural ending allowed."""
    return re.compile(rf"\b(?:{'|'.join(words)})(?:s|es)?\b", re.IGNORECASE)


# A comment the model calls words is taken as a photo request when it names none of the
# words cues and some of the photo cues.
_WORDS_CUES_RE = _cue_pattern(
    ("headline", "subline", "caption", "hashtag", "wording", "copy", "text", "title", "tone")
)
_PHOTO_CUES_RE = _cue_pattern(
    (
        "photo",
        "image",
        "picture",
        "look",
        "real",
        "realistic",
        "teeth",
        "face",
        "person",
        "background",
        "backdrop",
        "material",
    )
)


async def run_revise(deps: Deps, run: Run) -> Post | None:
    """Follow the run's comment on its parent post.

    Returns the post's next version, or None when the run failed or the comment was about the
    photo only; the outcome is recorded on the run. A comment about the photo is saved on the
    session's latest round, and a revise_prompt run for the session is started and awaited.
    The one case that raises is cancellation: the run is recorded as interrupted and the
    CancelledError is raised again.
    """

    async def work() -> Post | None:
        state = await run_workflow(_words_workflow(deps, run), run.id, run.comment)
        scope = state["scope"]
        post = None
        if scope != "photo":
            state = await run_workflow(_post_workflow(deps, run), run.id, run.comment, state)
            post = saved_post(deps.store, state)
        if scope != "words":
            await _send_to_session(deps, run, state["session_id"])
        return post

    return await settle(deps.store, run, work)


async def _send_to_session(deps: Deps, run: Run, session_id: str) -> None:
    """Save the comment as feedback on the session's latest round, then revise the session's
    prompt from it in a revise_prompt run of its own.

    When the session already has a run going, the hand-off is skipped and the run's last
    step says so.
    """
    store = deps.store
    session = store.get_session(session_id)
    if session is None:
        raise ValueError(SESSION_NOT_FOUND)
    active = store.get_run(session.active_run_id) if session.active_run_id else None
    if active is not None and active.status == "running":
        events = store.list_events(run.id)
        if events and events[-1].id is not None:
            last = events[-1]
            store.update_step_note(last.id, f"{last.note} {SESSION_BUSY}".strip())
        return
    # A session with no rounds yet takes the comment on round 0.
    store.save_round_feedback(
        RoundFeedback(session_id=session.id, round=session.rounds, text=run.comment)
    )
    revision = store.create_run(
        "revise_prompt",
        deps.kit.id,
        brief=session.brief,
        comment=run.comment,
        session_id=session.id,
    )
    session = update_session(store, session, active_run_id=revision.id)
    await run_revise_prompt(deps, revision, session)


def _words_workflow(deps: Deps, run: Run) -> Workflow:
    """load_context → rewrite_words."""
    store, kit = deps.store, deps.kit
    recorder = StepRecorder(store, run.id)
    prompt_writer = build_prompt_writer(deps.llm)

    async def load_context(ctx: Context) -> str:
        async with recorder.step("load_context") as info:
            previous = _parent_post(store, run)
            session = store.get_session(previous.session_id) if previous.session_id else None
            # The references of the post's session, which a post with no session lacks (v5).
            designer = designer_references(store, session.id if session else None)
            context: dict[str, Any] = {
                "task": "words",
                "brief": previous.brief,
                "brand": brand_block(kit),
                "current": _words_of(previous.spec),
                "comment": run.comment,
                # The prompt writer's layout rule names these (v3.1); only type_only is taken.
                "allowed_layouts": allowed_templates(kit),
            }
            # Only a session with references carries them, so one without reads as before.
            if designer:
                context["designer_references"] = designer_reference_context(designer)
            ctx.state["prompt_writer_context"] = context
            ctx.state["prompt_images"] = designer_reference_images(store, designer)
            ctx.state["previous_post"] = previous.model_dump(mode="json")
            ctx.state["session_id"] = session.id if session else None
            info.note = f"Revising version {previous.version}."
            info.note += designer_references_sent(len(designer))
            # The prompt writer receives the post's brief as its message, with the references
            # after it.
            return previous.brief

    async def rewrite_words(
        ctx: Context,
        node_input: str,
        previous_post: dict[str, Any],
        session_id: str | None,
        prompt_images: list[dict[str, str]],
    ) -> Event:
        async with recorder.step("rewrite_words") as info:
            info.provider = describe_llm(deps.llm)
            message = with_images(node_input, prompt_images)
            draft = await ask_prompt_writer(ctx, prompt_writer, message, info)
            # A post with no session has nowhere to send a request for a new photo.
            scope = draft.scope if session_id else "words"
            overruled = scope == "words" and session_id is not None and _reads_as_photo(run.comment)
            if overruled:
                scope = "photo"
            previous = Post.model_validate(previous_post)
            photo_path = existing_media(store, previous.photo_path)
            spec, notes = _revised_spec(previous.spec, draft, has_photo=photo_path is not None)
            if scope == "words":
                info.note = spec.reason_words
            else:
                info.note = f"{READS_AS_PHOTO} {SENT_TO_SESSION}" if overruled else SENT_TO_SESSION
            spec_data = spec.model_dump(mode="json")
            return Event(
                output=spec_data,
                state={
                    "scope": scope,
                    "design_spec": spec_data,
                    "photo_path": photo_path if spec.layout in NEEDS_PHOTO else None,
                    "post_notes": notes,
                },
            )

    return Workflow(
        name="revise_words",
        edges=[
            (
                "START",
                load_context,
                # A step that calls ctx.run_node must be rerunnable, as ADK requires.
                node(rewrite_words, name="rewrite_words", rerun_on_resume=True),
            )
        ],
    )


def _post_workflow(deps: Deps, run: Run) -> Workflow:
    """render → save_post: the revised words over the previous version's photo."""
    store, kit = deps.store, deps.kit
    recorder = StepRecorder(store, run.id)

    async def render(
        design_spec: dict[str, Any], photo_path: str | None, post_notes: list[str]
    ) -> Event:
        async with recorder.step("render") as info:
            return await render_post(deps, design_spec, photo_path, post_notes, info)

    async def save_post(
        previous_post: dict[str, Any],
        design_spec: dict[str, Any],
        photo_path: str | None,
        post_id: str,
        image_path: str,
        render_report: dict[str, Any],
        post_notes: list[str],
    ) -> Event:
        async with recorder.step("save_post"):
            previous = Post.model_validate(previous_post)
            spec = DesignSpec.model_validate(design_spec)
            root_post_id, version = place_in_history(store, post_id, previous)
            post = Post(
                id=post_id,
                root_post_id=root_post_id,
                version=version,
                run_id=run.id,
                brand_id=kit.id,
                brief=previous.brief,
                image_path=image_path,
                photo_path=photo_path,
                photo_provider=previous.photo_provider if photo_path else "",
                caption=spec.caption,
                spec=spec,
                render_report=RenderReport.model_validate(render_report),
                notes=post_notes,
                session_id=previous.session_id,
                sample_id=previous.sample_id if photo_path else None,
                direction_title=previous.direction_title,
            )
            store.save_post(post)
            return Event(output={"post_id": post.id, "version": post.version})

    return Workflow(name="revise_post", edges=[("START", render, save_post)])


def _parent_post(store: Store, run: Run) -> Post:
    parent = store.get_post(run.parent_post_id) if run.parent_post_id else None
    if parent is None:
        raise ValueError(PARENT_NOT_FOUND)
    return parent


def _reads_as_photo(comment: str) -> bool:
    """Whether a comment the model took as about the words names only what is in the photo."""
    return not _WORDS_CUES_RE.search(comment) and _PHOTO_CUES_RE.search(comment) is not None


def _words_of(spec: DesignSpec) -> dict[str, Any]:
    """The post's spec shaped like a prompt version, which the prompt writer reads as current."""
    return {
        "photo_prompt": spec.photo_prompt,
        "layout": spec.layout,
        "mode": spec.mode,
        "headline": spec.headline,
        "subline": spec.subline,
        "caption": spec.caption,
        "hashtags": list(spec.hashtags),
        "reason_prompt": spec.reason_layout,
        "reason_words": spec.reason_words,
    }


def _revised_spec(
    previous: DesignSpec, draft: PromptDraft, *, has_photo: bool
) -> tuple[DesignSpec, list[str]]:
    """The previous spec with the draft's words and mode, and a note for the post when the
    layout had to change. The previous layout and its composition stay unless the draft asks
    for words only. Whatever the draft says, the photo stays as it was.
    """
    notes: list[str] = []
    layout = "type_only" if draft.layout == "type_only" else previous.layout
    if layout in NEEDS_PHOTO and not has_photo:
        layout = "type_only"
        notes.append(NO_PHOTO_TO_REUSE)
    shows_photo = layout in NEEDS_PHOTO
    spec = previous.model_copy(
        update={
            "layout": layout,
            # The composition belongs to its template; a new template starts from its defaults.
            "composition": previous.composition if layout == previous.layout else None,
            "mode": draft.mode,
            "headline": draft.headline.strip(),
            "subline": draft.subline.strip(),
            "caption": draft.caption.strip(),
            "hashtags": clean_hashtags(draft.hashtags),
            "photo_prompt": previous.photo_prompt if shows_photo else "",
            "reuse_photo": shows_photo,
            "reason_words": draft.reason_words.strip(),
        }
    )
    return spec, notes
