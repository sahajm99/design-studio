"""The edit run (v5): one request of the editor's Ask mode.

ask_editor.

The designer types what to change in the editor, with a file when the change needs one. The
one step renders the layout on the canvas as the editor's Preview renders it, so the editor
agent judges positions from a picture and not from numbers alone, and asks the agent for
edits from a fixed set, or for one question; an answer that does not fit is asked for once
more. The agent never changes the layout itself: the route applies its edits and then the
guardrails to the layout the page sent (`apply_edits`, `apply_guardrails`), and nothing is
saved until the designer uses the layout. The run keeps no run in flight on its session,
since it changes nothing there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from google.adk import Context, Event, Workflow
from google.adk.agents import LlmAgent
from google.adk.workflow import node
from google.adk.workflow._errors import DynamicNodeFailError
from google.genai import types
from pydantic import ValidationError

from studio.contracts import (
    Block,
    BrandKit,
    Composition,
    CustomLayout,
    DesignSpec,
    EditorAnswer,
    Mode,
    PhotoBox,
    Run,
)
from studio.models import EDITOR_QUESTION, describe_llm
from studio.render import available_faces
from studio.render.custom import HEADLINE_PX, SUBLINE_PX
from studio.workflows.agents import build_editor_agent
from studio.workflows.layouts import PHOTO_FILE_MISSING
from studio.workflows.shared import (
    EMPTY_ANSWER,
    Deps,
    _first_validation_message,
    quoted,
    run_workflow,
    settle,
    with_images,
)
from studio.workflows.steps import StepInfo, StepRecorder

_ATTEMPTS = 2
_MAX_RETRY_NOTE_CHARS = 200
# The session state key the editor's instruction reads after a rejected answer.
_RETRY_NOTE = "editor_retry_note"
# The editor's picture of the layout is written here and removed once it has been sent.
_PICTURES_DIR = "editor"

# The lines before the pictures in the editor's message.
LAYOUT_NOW = "The layout as it is now."
UPLOADED_FILE = "The uploaded file."
# Why the run failed when no answer fitted.
EDITOR_INVALID = (
    f"The model's answer did not match the editor's answer after {_ATTEMPTS} attempts."
)
# The question asked for an answer with nothing in it, or a question left blank: the
# stand-in's own, so demo mode and the model ask alike.
ASK_AGAIN = EDITOR_QUESTION


class EditorInvalid(ValueError):
    """The editor agent's answers did not fit its schema."""


async def run_edit(
    deps: Deps,
    run: Run,
    *,
    request: str,
    layout: CustomLayout,
    words: dict[str, str],
    mode: Mode,
    photo: Path,
    upload_id: str = "",
    upload_picture: Path | None = None,
) -> EditorAnswer | None:
    """Ask the editor agent what the request changes in the layout, and record the run.

    `words` holds the headline and the subline and `mode` the mode the picture is drawn in and
    the context gives: the editor's own, or the session's saved ones where the page sent
    none. `photo` is the photo the editor works on. `upload_id` and `upload_picture` are the
    file that came with the request, when one did: its id for the context and the file the
    agent sees. Returns the answer, as a question when the agent left its question blank or
    gave a usable answer with nothing in it, or None when the run failed, which the run and
    its step record. Like every run, it raises only when cancelled.
    """

    async def work() -> EditorAnswer:
        workflow = _edit_workflow(
            deps, run, request, layout, words, mode, photo, upload_id, upload_picture
        )
        state = await run_workflow(workflow, run.id, request)
        return EditorAnswer.model_validate(state["editor_answer"])

    return await settle(deps.store, run, work)


def _edit_workflow(
    deps: Deps,
    run: Run,
    request: str,
    layout: CustomLayout,
    words: dict[str, str],
    mode: Mode,
    photo: Path,
    upload_id: str,
    upload_picture: Path | None,
) -> Workflow:
    """ask_editor: the picture, the context and the agent's answer, in one step."""
    store, kit = deps.store, deps.kit
    recorder = StepRecorder(store, run.id)
    editor = build_editor_agent(deps.llm)

    async def ask_editor(ctx: Context) -> Event:
        async with recorder.step("ask_editor") as info:
            info.provider = describe_llm(deps.llm)
            if not photo.is_file():
                # Said plainly, as the compose run says it, rather than as the renderer's error.
                raise ValueError(PHOTO_FILE_MISSING)
            picture = store.work_dir / _PICTURES_DIR / f"{run.id}.png"
            try:
                # The Preview's own rendering, so the agent sees what the designer would.
                await deps.renderer.render(_spec(layout, words, mode), kit, photo, picture)
                ctx.state["editor_context"] = _editor_context(kit, layout, words, mode, upload_id)
                images = [{"label": LAYOUT_NOW, "path": str(picture)}]
                if upload_picture is not None:
                    images.append({"label": UPLOADED_FILE, "path": str(upload_picture)})
                answer = await _ask_editor(ctx, editor, with_images(request, images), info)
            finally:
                picture.unlink(missing_ok=True)
            if answer.usable and not answer.edits and not answer.summary.strip():
                # Every field has a default, so a sparse answer fits the schema while saying
                # nothing; it asks rather than claiming a change.
                answer = answer.model_copy(update={"usable": False, "question": ASK_AGAIN})
            elif not answer.usable and not answer.question.strip():
                answer = answer.model_copy(update={"question": ASK_AGAIN})
            info.note = answer.summary if answer.usable else answer.question
            data = answer.model_dump(mode="json")
            return Event(output=data, state={"editor_answer": data})

    return Workflow(
        name="edit",
        # A step that calls ctx.run_node must be rerunnable, as ADK requires.
        edges=[("START", node(ask_editor, name="ask_editor", rerun_on_resume=True))],
    )


async def _ask_editor(
    ctx: Context, agent: LlmAgent, message: types.Content, info: StepInfo
) -> EditorAnswer:
    """The editor's answer, asked for again once with the problem fed back.

    Only an answer that does not fit the schema, or a blank one, is asked for again; then
    EditorInvalid is raised. Any other failure, such as the model service being down, fails
    the step as it is, as for the prompt writer.
    """
    for attempt in range(1, _ATTEMPTS + 1):
        info.attempts = attempt
        try:
            answer = await ctx.run_node(agent, node_input=message)
        except DynamicNodeFailError as failure:
            if not isinstance(failure.error, ValidationError):
                raise failure.error from None
            problem = _first_validation_message(failure.error)
        else:
            if answer is not None:
                return EditorAnswer.model_validate(answer)
            problem = EMPTY_ANSWER
        # The editor's instruction reads this on the next attempt.
        ctx.state[_RETRY_NOTE] = problem[:_MAX_RETRY_NOTE_CHARS]
    raise EditorInvalid(EDITOR_INVALID)


def _spec(layout: CustomLayout, words: dict[str, str], mode: Mode) -> DesignSpec:
    """The layout as a custom design spec with the post's words and mode, as the Preview
    renders it."""
    return DesignSpec(
        layout="custom",
        composition=Composition(template="custom", custom=layout),
        mode=mode,
        headline=words.get("headline", ""),
        subline=words.get("subline", ""),
    )


# --------------------------------------------------------------------- the context


def _editor_context(
    kit: BrandKit, layout: CustomLayout, words: dict[str, str], mode: Mode, upload_id: str
) -> dict[str, Any]:
    """What the editor reads beside the request and the pictures: the brand's name and rules;
    the layout, its blocks numbered from 0 with each one's kind, words, box in percent of the
    canvas and style, the photo's fit, offsets and box, and the canvas colour; the palette
    and the faces a post may use; the post's words and mode; and the upload's id, only when a
    file came with the request. Colours, faces, weights and sizes are the ones each block
    draws with, so a block that keeps the brand's shows them too. The designer's words are
    quoted, so none can end the context block early; the request is not in the context."""
    roles = kit.modes[mode]
    context: dict[str, Any] = {
        "brand": {"name": kit.name, "rules": list(kit.rules)},
        "layout": {
            "blocks": [
                _context_block(number, block, kit, words, mode)
                for number, block in enumerate(layout.blocks)
            ],
            "photo": {
                "fit": layout.photo_fit,
                "offset_x": layout.photo_offset_x,
                "offset_y": layout.photo_offset_y,
                # No box of its own means the photo covers the whole canvas.
                "box": _box(layout.photo or PhotoBox(), ("x", "y", "w", "h")),
            },
            "background": layout.background or roles.background,
        },
        "palette": [
            {"name": colour.name, "hex": colour.hex, "use": colour.use} for colour in kit.colours
        ],
        "fonts": [face.name for face in available_faces(kit)],
        "words": {kind: quoted(words.get(kind, "")) for kind in ("headline", "subline")},
        "mode": mode,
    }
    if upload_id:
        context["upload_id"] = upload_id
    return context


def _context_block(
    number: int, block: Block, kit: BrandKit, words: dict[str, str], mode: Mode
) -> dict[str, Any]:
    """One block as the editor reads it: its number and kind; its words (the post's for the
    headline and the subline, its own for a text block); its box, with a height only for a
    shade or an image, since the words and the logo take theirs from their width; and its
    style."""
    entry: dict[str, Any] = {"number": number, "kind": block.kind}
    if block.kind in ("headline", "subline"):
        entry["words"] = quoted(words.get(block.kind, ""))
    elif block.kind == "text":
        entry["words"] = quoted(block.text)
    own_height = block.kind in ("shade", "image")
    entry["box"] = _box(block, ("x", "y", "w", "h") if own_height else ("x", "y", "w"))
    entry["style"] = _context_style(block, kit, mode)
    return entry


def _context_style(block: Block, kit: BrandKit, mode: Mode) -> dict[str, Any]:
    """A block's style as it draws: the words' face, weight, italics, size, alignment and
    colour; a shade's colour and opacity; an image's fit, opacity and whether it keeps its
    aspect; and whether the logo is an upload in place of the kit's file."""
    roles = kit.modes[mode]
    typography = kit.typography
    if block.kind in ("headline", "subline", "text"):
        headline = block.kind == "headline"
        return {
            "font": block.font or typography.family,
            "weight": block.weight
            or (typography.headline_weight if headline else typography.body_weight),
            "italic": block.italic,
            "size_px": block.size_px or (SUBLINE_PX if block.kind == "subline" else HEADLINE_PX),
            "align": block.align,
            "colour": block.colour or (roles.headline if headline else roles.body),
        }
    if block.kind == "shade":
        return {"colour": block.colour or roles.background, "opacity": block.opacity}
    if block.kind == "image":
        return {"fit": block.fit, "opacity": block.opacity, "keep_aspect": block.keep_aspect}
    return {"uploaded": bool(block.image_path)}


def _box(item: Block | PhotoBox, sides: tuple[str, ...]) -> dict[str, float]:
    """The sides of a block's or the photo's box, to one decimal place."""
    return {side: round(getattr(item, side), 1) for side in sides}
