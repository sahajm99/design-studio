"""The studio's pages and JSON endpoints.

Every handler reads what it needs from `request.app.state`, which the app's
lifespan (in `studio/main.py`) fills in before any request can arrive.
"""

from __future__ import annotations

import asyncio
import difflib
import io
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, get_args
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from PIL import Image, ImageOps
from pydantic import BaseModel, Field, StringConstraints, ValidationError

from studio.brand import logo_for_mode
from studio.contracts import (
    AutoSettings,
    AutoState,
    BrandKit,
    Choice,
    Composition,
    CustomLayout,
    Direction,
    DirectionFormat,
    EditorAnswer,
    LayoutCandidate,
    LayoutId,
    LayoutTemplate,
    LogoPosition,
    Mode,
    PhotoSide,
    Post,
    PromptVersion,
    QualityBar,
    QualityBarSource,
    Reaction,
    ResearchReport,
    RoundFeedback,
    Run,
    RunEvent,
    RunKind,
    Sample,
    Scrim,
    SessionMode,
    SessionReference,
    StudioSession,
    StyleCard,
    TextAlign,
    TextPosition,
    Upload,
    new_id,
    now,
)
from studio.library.analysis import AnalysisJob
from studio.library.base import ImageFetcher, ReferenceSource
from studio.library.importer import import_references
from studio.library.sources import (
    MAX_LINKS,
    BoardHtmlSource,
    FolderSource,
    LinkListSource,
    is_web_link,
)
from studio.library.taste import build_taste_profile
from studio.render import (
    NEEDS_PHOTO,
    allowed_templates,
    apply_edits,
    apply_guardrails,
    available_faces,
    blocks_for_template,
    default_layout,
    describe,
    edited_words,
    face_named,
    shortlist,
)
from studio.render.custom import upload_file
from studio.render.faces import Face
from studio.store import Store
from studio.web.jobs import RunJobs
from studio.web.uploads import (
    MAX_UPLOAD_BYTES,
    UPLOAD_REFUSED,
    UploadRejected,
    agent_copy_path,
    read_image_upload,
    save_brand_upload,
    upload_extension,
    write_agent_copy,
)
from studio.workflows import (
    Deps,
    analyse_reference,
    directions_alike,
    pick_source_photo,
    quality_bar,
    run_auto,
    run_compose,
    run_draft,
    run_edit,
    run_finish,
    run_revise,
    run_revise_prompt,
    run_samples,
    run_scout,
)
from studio.workflows.shared import QUALITY_BAR_SETTING, designer_references, plural

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

_MAX_BRIEF_CHARS = 2000
_MAX_COMMENT_CHARS = 2000
_MAX_HASHTAGS = 8
_PROMPT_EXCERPT_CHARS = 90
_NOT_FOUND = "Not found."

STEP_LABELS: dict[str, str] = {
    "load_context": "Gather context",
    "art_director": "Design the post",
    "get_photo": "Get the photo",
    "render": "Render",
    "save_post": "Save",
    "write_prompt": "Write the prompt",
    "save_draft": "Save the draft",
    "load_prompt": "Load the prompt",
    "generate_samples": "Make the samples",
    "review_samples": "Review the samples",
    "save_round": "Save the round",
    "load_feedback": "Read the feedback",
    "rewrite_prompt": "Rewrite the prompt",
    "load_pick": "Load the chosen photo",
    "rewrite_words": "Rewrite the words",
    "rank_samples": "Rank the samples",
    "shortlist": "Choose fitting layouts",
    "render_layouts": "Render the layouts",
    "save_layouts": "Save the layouts",
    "plan": "Plan the run",
    "draft": "Draft the prompt",
    "round_loop": "Make and judge the rounds",
    "compose": "Compose the layouts",
    "final_check": "Check the finished post",
    "finish": "Finish the post",
    "report": "Report",
    # v4: the scout run, and the auto run's research stage.
    "check_brief": "Check the brief",
    "plan_queries": "Plan the searches",
    "search": "Search the web",
    "propose": "Propose directions",
    "save_directions": "Save the directions",
    "research": "Research",
    # v5: the editor's Ask mode.
    "ask_editor": "Ask the editor",
}

# A step name two kinds of run share, labelled for one of them. The auto run's last step and
# the scout's research prose are both called "report".
_STEP_LABELS_BY_KIND: dict[str, dict[str, str]] = {
    "scout": {"report": "Write the research"},
}

_RUN_KIND_LABELS: dict[str, str] = {
    "create": "New post",
    "revise": "Revision",
    "draft": "Drafting the prompt",
    "samples": "Making samples",
    "revise_prompt": "Revising the prompt",
    "finish": "Finishing the post",
    "compose": "Composing the layout",
    "auto": "Automatic session",
    "scout": "Researching",
    "edit": "Editing the layout",
}

# {samples} is the session's sample count with its noun: "1 sample", "3 samples".
_RUN_STATUS_TEXT: dict[str, str] = {
    "draft": "Writing the prompt…",
    "samples": "Making {samples}…",
    "revise_prompt": "Rewriting the prompt…",
    "finish": "Finishing the post…",
    "compose": "Rendering the layouts…",
    "scout": "Researching…",
}

_AUTO_STATUS_TEXT = "Auto: round {round} of {rounds}, {photos} of {budget}"
# Once a decision has ended the rounds, the run composes, checks and finishes the post.
_AUTO_FINISHING_TEXT = "Auto: {rounds} done, {photos} of {budget}, finishing"

_AUTHOR_LABELS: dict[str, str] = {
    "agent": "the agent",
    "designer": "you",
    "agent_after_feedback": "the agent, after your feedback",
}

_ARCHIVE_FILTERS: tuple[str, ...] = ("all", "liked", "disliked", "picked", "unrated")
_ARCHIVE_REACTION_FILTERS: dict[str, Reaction] = {"liked": "liked", "disliked": "disliked", "unrated": "none"}
_ARCHIVE_FILTER_LABELS: dict[str, str] = {"unrated": "Not rated"}

_HASHTAG_SPLIT_RE = re.compile(r"[\s,]+")

_SESSION_BUSY = "The session is busy."
# The photo prompt of the version the editor saves for a session that has none yet.
_EDITOR_PHOTO_PROMPT = "The chosen photo, kept as it is."

# The upload checks (size, type, pixels) and their message live in studio/web/uploads.py.

# A library upload's refused file, one sentence each after "{n} refused: ".
_FILE_REFUSED = "{name} is not an image the studio can use."
_REFUSED_FILES = "{n} refused: {sentences}"
# A paste's lines that brought no link in, also said in the import summary.
_NOT_WEB_LINKS = "{n} lines were not web links."
_NOT_WEB_LINK = "1 line was not a web link."
_LINKS_LIMITED = "Only the first {limit} links were used."

# The quality bar's line on the Library page, for the kit's example and for the designer's choice.
_QUALITY_BAR_KIT_LINE = "The brand's ideal example from the kit."
_QUALITY_BAR_SET_LINE = "Set by you from {label} on {date}."
# The label each kind of choice is saved with; an upload is labelled with its file's name.
_QUALITY_BAR_POST_LABEL = "a post"
_QUALITY_BAR_SAMPLE_LABEL = "a photo"
_QUALITY_BAR_UPLOAD_LABEL = "an upload"
# The quality bar goes with every critic's message, so its copy is kept to this many pixels
# on its longer side.
_MAX_QUALITY_BAR_SIDE = 2048
# The status line on the post and archive pages after "Set as the quality bar" (`?bar=set`).
_QUALITY_BAR_IS_SET = "The quality bar is set."
# The studio's GET pages that "Set as the quality bar" goes back to: a post, a session, a
# session's editor, the archive, the library and the Editor tab. Any other referring path
# (a POST-only one such as /archive/delete or /posts/{id}/revise among them) is not.
_QUALITY_BAR_RETURN_PAGES = re.compile(
    r"/posts/[^/]+|/sessions/[^/]+(?:/editor)?|/archive|/library|/editor"
)

# The most blocks an editor preview may carry: the page starts with three or four.
_MAX_EDITOR_BLOCKS = 12
_TOO_MANY_BLOCKS = "Too many blocks."
# v5: the editor's Ask mode. A request is one line of at most this many characters, and an
# editor agent that cannot answer is reported with this line, its run showing why.
_MAX_ASK_CHARS = 300
_EDITOR_FAILED = "The editor could not answer."

# The two files a face can have, as /brand/font names them.
_FontStyle = Literal["regular", "italic"]

# The Research part's status line (v4), one for each kind of research.
_RESEARCH_GROUNDED = "Grounded in {sources}."
_RESEARCH_UNGROUNDED = "Not grounded: {problem}"
# Follows the line above only when the session has directions for it to speak of.
_RESEARCH_UNGROUNDED_DIRECTIONS = (
    "These directions come from the brand's occasions and the model's own knowledge."
)
_RESEARCH_DEMO = "Demo research. It is made up; no search ran."
# The layouts by the names the session page's Layout choice gives them.
_LAYOUT_LABELS: dict[str, str] = {
    "hero": "Hero photo",
    "full_bleed": "Full bleed",
    "split": "Split",
    "corner": "Corner",
    "caption_strip": "Caption strip",
    "type_only": "Words only",
}
_DIRECTION_FORMATS: tuple[str, ...] = get_args(DirectionFormat)
# What "Edit and use" keeps of each field the designer types.
_MAX_DIRECTION_FIELD_CHARS = 400
_MAX_DIRECTION_FACTS = 6

# v5: the designer's references on a session. A session keeps six at most, and a note is one
# line of at most 80 characters, as SessionReference allows.
_MAX_SESSION_REFERENCES = 6
_MAX_REFERENCE_NOTE_CHARS = 80
# The session page's status line after references were sent, by the code its address
# carries: more files than the six a session keeps, or a file the studio cannot use.
_REFERENCES_LIMITED = "Six references kept; the rest were left out."
_REFERENCE_STATUS_LINES: dict[str, str] = {
    "limited": _REFERENCES_LIMITED,
    "refused": UPLOAD_REFUSED,
}
# The analyst cards a reference as it is uploaded, and is waited for this long at most.
_REFERENCE_CARD_SECONDS = 20
_REFERENCE_MIME_TYPES: dict[str, str] = {"png": "image/png", "jpg": "image/jpeg"}
# The post page's summary line ends with how many references the post's session has.
_MADE_WITH_REFERENCES = " Made with {references}."


class ChoiceBody(BaseModel):
    choice: Choice


class ReactionBody(BaseModel):
    reaction: Reaction
    comment: str = ""


class EditorPreviewBody(BaseModel):
    """The editor's Preview: the arrangement, and the words and mode it is drawn with."""

    sample_id: str
    headline: str
    subline: str
    mode: Mode
    layout: CustomLayout


class EditorAskBody(BaseModel):
    """The editor's Ask (v5): what the designer typed, the layout on the canvas, and the file
    uploaded with the request, if any, as its upload id. `sample` is the photo the editor
    works on, the session's chosen one when it is left out; `from`, the layout candidate the
    editor opened from, comes from the editor's address, and the answer does not depend on it.
    `headline`, `subline` and `mode` are the editor's own fields, as the preview sends them;
    the session's current version stands in for any left out."""

    request: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=_MAX_ASK_CHARS)
    ]
    layout: CustomLayout
    upload_id: str | None = None
    sample: str | None = None
    from_: str | None = Field(default=None, alias="from")
    headline: str | None = None
    subline: str | None = None
    mode: Mode | None = None


# ------------------------------------------------------------------- helpers

# The studio's own pages take these colours from the active kit (the kit's colour name, the
# CSS variable, the design system's value for a kit that lacks the name). The status colours
# are the stylesheet's own, so an outcome reads the same whatever the kit.
_CHROME_TOKENS: tuple[tuple[str, str, str], ...] = (
    ("heading_blue", "--heading", "#0F4B7B"),
    ("ink", "--ink", "#18181B"),
    ("ink_soft", "--ink-soft", "#52525B"),
    ("ink_faint", "--ink-faint", "#A1A1AA"),
    ("line", "--ink-line", "#E4E4E7"),
    ("wash", "--ink-wash", "#F4F4F5"),
    ("porcelain", "--canvas", "#F3F4F7"),
    ("white", "--white", "#FFFFFF"),
)


def chrome_tokens(kit: BrandKit) -> dict[str, str]:
    """The chrome's CSS variables from the kit's colours, with the system's value for any
    colour the kit does not name. The base template writes them on :root.

    One more key, --kit-heading, repeats --heading's value: the dark theme lightens the kit's
    heading colour from it, so the light --heading stays the kit's own."""
    palette = {colour.name: colour.hex for colour in kit.colours}
    tokens = {variable: palette.get(name, default) for name, variable, default in _CHROME_TOKENS}
    tokens["--kit-heading"] = tokens["--heading"]
    return tokens


def _base_context(request: Request) -> dict[str, Any]:
    """The bar, banner, colour and type values every page template needs."""
    deps: Deps = request.app.state.deps
    return {
        "brand_name": deps.kit.name,
        "demo_mode": deps.settings.demo_mode,
        "has_photo_source": deps.photo_provider is not None,
        "chrome_tokens": chrome_tokens(deps.kit),
        # The kit's family, served by /brand/font; its italic file is optional.
        "brand_font": deps.kit.typography.family,
        "brand_font_italic": deps.kit.typography.italic_file is not None,
    }


def _page(request: Request, name: str, context: dict[str, Any], *, status_code: int = 200) -> Response:
    return templates.TemplateResponse(
        request=request, name=name, context={**_base_context(request), **context}, status_code=status_code
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail=_NOT_FOUND)


def _require_session(store: Store, session_id: str) -> StudioSession:
    session = store.get_session(session_id)
    if session is None:
        raise _not_found()
    return session


def _session_busy(store: Store, session: StudioSession) -> bool:
    """True while a run started from this session is still going, so a second press cannot start another."""
    if session.active_run_id is None:
        return False
    run = store.get_run(session.active_run_id)
    return run is not None and run.status == "running"


def _midnight_utc() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _clamp_sample_count(raw: int, max_samples: int) -> int:
    return max(1, min(max_samples, raw))


def _auto_settings(max_rounds: int, photo_budget: int, stop_score: float) -> AutoSettings:
    """An auto session's limits from the Studio form, each clamped to the range it allows."""
    return AutoSettings(
        max_rounds=max(1, min(5, max_rounds)),
        photo_budget=max(1, min(12, photo_budget)),
        stop_score=max(1.0, min(5.0, stop_score)),
    )


def _parse_hashtags(raw: str) -> list[str]:
    """Hashtags from one free-text field, split on whitespace or commas."""
    cleaned: list[str] = []
    for part in _HASHTAG_SPLIT_RE.split(raw.strip()):
        compact = part.strip()
        if not compact.strip("#"):
            continue
        cleaned.append(compact if compact.startswith("#") else f"#{compact}")
    return cleaned[:_MAX_HASHTAGS]


def _normalise_textarea(value: str) -> str:
    """A textarea's value with its line breaks as the stored text uses them.

    Browsers send a line break as `\r\n`; a stored prompt or caption holds plain `\n`.
    """
    return value.replace("\r\n", "\n")


def _duration_text(event: RunEvent) -> str:
    if event.ended_at is None:
        return ""
    seconds = (event.ended_at - event.started_at).total_seconds()
    return f"{seconds:.1f}s"


def _step_label(kind: str, step: str) -> str:
    """The step's name in plain words, for a run of this kind."""
    return _STEP_LABELS_BY_KIND.get(kind, {}).get(step) or STEP_LABELS.get(step, step)


def _latest_step_note(store: Store, run: Run | None) -> str:
    """The note of the most recently started step of `run`, or "" when there is none."""
    if run is None:
        return ""
    events = store.list_events(run.id)
    return events[-1].note if events else ""


def _child_note(store: Store, run: Run | None) -> str:
    """What the stage in flight of the auto run `run` is doing, or "" when `run` is not an
    auto run or its newest stage is not running.

    It names the stage and its latest step, with the step's note when it has one, for
    example "Making samples: Make the samples · 1 of 2 made…". Before the stage's first
    step, it names the stage alone.
    """
    if run is None or run.kind != "auto":
        return ""
    stages = store.list_runs_for_parent(run.id)
    stage = stages[-1] if stages else None
    if stage is None or stage.status != "running":
        return ""
    kind_label = _RUN_KIND_LABELS.get(stage.kind, stage.kind)
    events = store.list_events(stage.id)
    if not events:
        return kind_label
    step = events[-1]
    note = f" · {step.note}" if step.note else ""
    return f"{kind_label}: {_step_label(stage.kind, step.step)}{note}"


def _word_diff_html(old_text: str, new_text: str) -> Markup:
    """`new_text` as safe HTML: words it adds over `old_text` wrapped in <ins>, words it
    drops wrapped in <del>. Compared with difflib at word granularity, as the spec asks;
    every word is escaped before it goes into a tag, so the result is safe to render as-is.
    """
    old_words = old_text.split()
    new_words = new_text.split()
    matcher = difflib.SequenceMatcher(a=old_words, b=new_words, autojunk=False)
    chunks: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            chunks.append(str(escape(" ".join(new_words[j1:j2]))))
            continue
        if tag in ("delete", "replace"):
            chunks.append(f"<del>{escape(' '.join(old_words[i1:i2]))}</del>")
        if tag in ("insert", "replace"):
            chunks.append(f"<ins>{escape(' '.join(new_words[j1:j2]))}</ins>")
    return Markup(" ".join(chunk for chunk in chunks if chunk))


def _meta_text(duration: str, provider: str, attempts: int) -> str:
    """Duration, provider and attempts (when more than one) as one plain sentence fragment."""
    parts = [part for part in (duration, f"with {provider}" if provider else "") if part]
    text = " ".join(parts)
    if attempts > 1:
        extra = f"{attempts} attempts"
        text = f"{text}, {extra}" if text else extra
    return text


def _step_rows(events: list[RunEvent], kind: str) -> list[dict[str, Any]]:
    """One row per step of a run of this kind, in plain words, for the run page."""
    rows = []
    for event in events:
        duration = _duration_text(event)
        rows.append(
            {
                "name": event.step,
                "label": _step_label(kind, event.step),
                "status": event.status,
                "meta": _meta_text(duration, event.provider, event.attempts),
                "note": event.note,
                "error": event.error,
            }
        )
    return rows


def _took_text(run: Run) -> str:
    """How long the run took, "48.2s" or "5m 12s", or "" while it is still going."""
    if run.finished_at is None:
        return ""
    seconds = max(0.0, (run.finished_at - run.created_at).total_seconds())
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}m {rest:02d}s"


def _run_rows(runs: list[Run]) -> list[dict[str, Any]]:
    """One row per run, with its kind in plain words and how long it took, for the runs table.

    A stage of an automatic session follows the run it belongs to, oldest first, marked as a
    child; a stage whose run is not in the list keeps its own place.
    """
    listed = {run.id for run in runs}
    stages: dict[str, list[Run]] = {}
    for run in runs:
        if run.parent_run_id in listed:
            stages.setdefault(run.parent_run_id, []).append(run)

    def row(run: Run, *, child: bool) -> dict[str, Any]:
        kind_label = _RUN_KIND_LABELS.get(run.kind, run.kind)
        return {"run": run, "kind_label": kind_label, "took": _took_text(run), "child": child}

    rows = []
    for run in runs:
        if run.parent_run_id in listed:
            continue
        rows.append(row(run, child=False))
        own = sorted(stages.get(run.id, []), key=lambda stage: stage.created_at)
        rows.extend(row(stage, child=True) for stage in own)
    return rows


def _child_rows(store: Store, run_id: str) -> list[dict[str, Any]]:
    """The runs that are stages of the automatic session `run_id`, oldest first, for the run page."""
    return [
        {
            "id": child.id,
            "kind": child.kind,
            "kind_label": _RUN_KIND_LABELS.get(child.kind, child.kind),
            "status": child.status,
        }
        for child in store.list_runs_for_parent(run_id)
    ]


def _studio_context(deps: Deps, *, error: str | None = None, brief_value: str = "") -> dict[str, Any]:
    return {
        "posts": deps.store.list_posts(),
        "sessions": deps.store.list_sessions(),
        "photos_today": deps.store.count_photos_since(_midnight_utc()),
        # v4: "Research first" and the month's searches, unless the kit turns research off.
        "research_enabled": deps.kit.research.enabled,
        "searches_this_month": deps.store.search_credits_this_month(),
        "search_limit": deps.settings.research_monthly_limit,
        "max_samples": deps.settings.max_samples,
        "error": error,
        "brief_value": brief_value,
    }


def _editor_start_candidate(store: Store, post: Post) -> str | None:
    """The layout the editor opens the post's photo with: the session's chosen layout, when it
    is a layout of that photo. None when it is not, or when there is none."""
    session = store.get_session(post.session_id) if post.session_id else None
    candidate_id = session.picked_candidate_id if session else None
    candidate = store.get_layout_candidate(candidate_id) if candidate_id else None
    if candidate is None or candidate.sample_id != post.sample_id:
        return None
    return candidate.id


def _post_context(
    store: Store, post: Post, *, error: str | None = None, message: str | None = None
) -> dict[str, Any]:
    versions = store.list_versions(post.root_post_id)
    reference_thumbs = [
        reference
        for reference in (store.get_reference(ref_id) for ref_id in post.spec.reference_ids)
        if reference is not None
    ]
    sample = store.get_sample(post.sample_id) if post.sample_id else None
    return {
        "post": post,
        "versions": versions,
        "reference_thumbs": reference_thumbs,
        "sample": sample,
        "editor_from": _editor_start_candidate(store, post),
        "summary_line": _summary_line(store, post),
        "error": error,
        "message": message,
    }


def _summary_line(store: Store, post: Post) -> str:
    """The post page's summary line: the auto session's summary, when an auto session made
    the post, then how many references the post's session has, counted now (v5). "" when
    there is neither."""
    count = len(designer_references(store, post.session_id))
    if not count:
        return post.auto_summary
    made_with = _MADE_WITH_REFERENCES.format(references=plural(count, "reference"))
    # A post made by hand has no auto summary, so its line is the count alone.
    return f"{post.auto_summary}{made_with}".strip()


def _library_context(
    deps: Deps,
    analysis_job: AnalysisJob,
    *,
    import_summary: Any = None,
    refused_text: str = "",
    quality_bar_error: str | None = None,
) -> dict[str, Any]:
    references = deps.store.list_references()
    return {
        "has_board": bool(deps.kit.inspiration_board),
        "references": references,
        "taste": build_taste_profile(references),
        "progress": analysis_job.progress(),
        "import_summary": import_summary,
        "refused_text": refused_text,
        "quality_bar": _quality_bar_view(deps),
        "quality_bar_error": quality_bar_error,
        # v5: the brand's shelf of uploads, newest first, under the quality bar.
        "uploads": deps.store.list_uploads(deps.kit.id),
    }


def _refused_text(names: list[str]) -> str:
    """The import summary's line for the files a library upload refused, or "" for none."""
    if not names:
        return ""
    sentences = " ".join(_FILE_REFUSED.format(name=name) for name in names)
    return _REFUSED_FILES.format(n=len(names), sentences=sentences)


def _links_left_out_text(lines: list[str]) -> str:
    """The import summary's line for the pasted lines that brought no link in: the non-blank
    ones that are not web links, and the links past the twentieth. "" when neither applies.

    It reads the lines as `LinkListSource` does: stripped, and a repeated link counts once.
    """
    entries = [line.strip() for line in lines if line.strip()]
    not_links = sum(1 for entry in entries if not is_web_link(entry))
    links = {entry for entry in entries if is_web_link(entry)}
    sentences: list[str] = []
    if not_links:
        sentences.append(_NOT_WEB_LINK if not_links == 1 else _NOT_WEB_LINKS.format(n=not_links))
    if len(links) > MAX_LINKS:
        sentences.append(_LINKS_LIMITED.format(limit=MAX_LINKS))
    return " ".join(sentences)


def _build_source(deps: Deps, source: str) -> ReferenceSource:
    if source == "board":
        if not deps.kit.inspiration_board:
            raise HTTPException(status_code=400, detail="This brand kit has no inspiration board.")
        return BoardHtmlSource(Path(deps.kit.root) / deps.kit.inspiration_board)
    if source == "inbox":
        return FolderSource(deps.store.inbox_dir)
    raise HTTPException(status_code=400, detail="Choose a source to import from.")


async def _import_and_analyse(request: Request, source: ReferenceSource) -> Any:
    """Import the source's items into the library, start the analysis job, and give back the summary."""
    deps: Deps = request.app.state.deps
    fetcher: ImageFetcher = request.app.state.fetcher
    summary = await import_references(source, deps.store, fetcher)
    analysis_job: AnalysisJob = request.app.state.analysis_job
    analysis_job.start()
    return summary


def _inbox_name(inbox: Path, filename: str, extension: str) -> str:
    """The name a library upload is saved under in the inbox: a plain-letters form of the
    file's name, so the reference's label says what it was. An id is added only when a file
    in the inbox already has that name; a name with no plain letters is the id alone."""
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(filename).stem).strip("-")[:40]
    if not stem:
        return f"{new_id()}.{extension}"
    name = f"{stem}.{extension}"
    return f"{stem}-{new_id()}.{extension}" if (inbox / name).exists() else name


# ---------------------------------------------------------------- quality bar


def _in_data_folder(store: Store, path: Path) -> bool:
    try:
        store.relative(path)
    except ValueError:
        return False
    return True


def _quality_bar_view(deps: Deps) -> dict[str, Any]:
    """The quality bar as the pages show it: the image's address, the line that says where it
    came from, and whether it is the designer's choice. No address when there is no bar.

    `quality_bar()` decides which image it is; the kit's file lies outside the data folder
    and is served by /brand/quality-bar, the designer's copy inside it, under /media/.
    """
    store = deps.store
    path, _ = quality_bar(store, deps.kit)
    if path is None:
        return {"url": None, "line": "", "is_designer": False}
    if not _in_data_folder(store, path):
        return {"url": "/brand/quality-bar", "line": _QUALITY_BAR_KIT_LINE, "is_designer": False}
    line = ""
    try:
        chosen = QualityBar.model_validate(store.get_setting(QUALITY_BAR_SETTING))
    except ValidationError:
        pass
    else:
        set_at = chosen.set_at
        date = f"{set_at.day} {set_at:%B %Y}"
        line = _QUALITY_BAR_SET_LINE.format(label=chosen.label or _QUALITY_BAR_UPLOAD_LABEL, date=date)
    return {"url": f"/media/{store.relative(path)}", "line": line, "is_designer": True}


def _write_quality_bar(store: Store, source: Path | bytes) -> str:
    """Save the image as a PNG of its own in the quality-bar folder, upright and at most
    _MAX_QUALITY_BAR_SIDE pixels on its longer side, and give back its path relative to the
    data folder. The copy keeps the bar when the photo it came from is deleted."""
    path = store.quality_bar_dir / f"{new_id()}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(source) if isinstance(source, bytes) else source) as image:
        picture = ImageOps.exif_transpose(image)
        picture.thumbnail((_MAX_QUALITY_BAR_SIDE, _MAX_QUALITY_BAR_SIDE))
        has_alpha = picture.mode in ("RGBA", "LA", "PA") or "transparency" in picture.info
        picture.convert("RGBA" if has_alpha else "RGB").save(path, format="PNG")
    return store.relative(path)


async def _set_quality_bar(
    store: Store, source: Path | bytes, kind: QualityBarSource, label: str
) -> bool:
    """Copy the image into the quality-bar folder and make it the studio's quality bar.

    False, with the bar left as it was, when the image cannot be decoded in full: a file can
    pass the upload's checks and still be cut short.
    """
    try:
        # Decoding and encoding a large image is blocking work, so it runs off the event loop.
        relative = await asyncio.to_thread(_write_quality_bar, store, source)
    except (OSError, ValueError, Image.DecompressionBombError):
        return False
    store.set_setting(QUALITY_BAR_SETTING, QualityBar(source=kind, image_path=relative, label=label))
    return True


def _media_file(store: Store, relative: str | None) -> Path:
    """The file at `relative` in the data folder. 404 when there is none."""
    if not relative:
        raise _not_found()
    try:
        path = store.media_path(relative)
    except ValueError:
        raise _not_found() from None
    if not path.is_file():
        raise _not_found()
    return path


def _referring_page(request: Request, fallback: str) -> str:
    """The page on this site the form was sent from, or `fallback` when the browser did not say."""
    parts = urlsplit(request.headers.get("referer", ""))
    # A path starting with "//" (or "/\", which browsers read the same) would name another site.
    if (
        parts.netloc != request.url.netloc
        or not parts.path.startswith("/")
        or parts.path.startswith(("//", "/\\"))
    ):
        return fallback
    return f"{parts.path}?{parts.query}" if parts.query else parts.path


def _quality_bar_return(request: Request, fallback: str) -> str:
    """The page "Set as the quality bar" goes back to: the referring page when it is one of the
    studio's GET pages (`_QUALITY_BAR_RETURN_PAGES`), else `fallback`. A page drawn by a POST,
    such as the archive after a delete, has a POST-only address, so it is never gone back to."""
    back = _referring_page(request, fallback)
    return back if _QUALITY_BAR_RETURN_PAGES.fullmatch(urlsplit(back).path) else fallback


def _with_bar_status(url: str, *, is_set: bool) -> str:
    """`url` with `bar=set` in its query when the bar was set, so the page says so once, and
    without any `bar` it carried before otherwise. The rest of its query is kept."""
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    query = [(key, value) for key, value in pairs if key != "bar"]
    if is_set:
        query.append(("bar", "set"))
    return f"{parts.path}?{urlencode(query)}" if query else parts.path


# --------------------------------------------------------------- session page


def _version_label(version: PromptVersion) -> str:
    author = _AUTHOR_LABELS.get(version.author, version.author)
    return f"Version {version.number} by {author}"


def _round_groups(store: Store, session_id: str) -> list[dict[str, Any]]:
    """Every round's samples, feedback and ranking, newest round first."""
    samples_by_round: dict[int, list[Sample]] = {}
    for sample in store.list_samples(session_id):
        samples_by_round.setdefault(sample.round, []).append(sample)
    feedback_by_round = {feedback.round: feedback for feedback in store.list_round_feedback(session_id)}

    groups = []
    for number in sorted(samples_by_round, reverse=True):
        samples = samples_by_round[number]
        review = store.get_round_review(session_id, number)
        scored = [sample for sample in samples if sample.review is not None]
        groups.append(
            {
                "round": number,
                "samples": samples,
                "feedback": feedback_by_round.get(number),
                "review": review,
                "rank_total": len(review.order) if review else len(scored),
                # A round the critic could not rank, but had more than one sample to rank.
                "ranking_skipped": review is None and len(scored) > 1,
            }
        )
    return groups


def _auto_status(run: Run | None, session: StudioSession) -> str | None:
    """How far the auto run has got, while it is in flight for this session, or None.

    It follows the run in flight, not the session's mode, which a restart can leave behind.
    Once a decision has ended the rounds, it counts the rounds done instead of naming one
    that will not run.
    """
    if run is None or run.kind != "auto" or run.status != "running":
        return None
    settings = session.auto_settings or AutoSettings()
    auto_state = session.auto_state or AutoState()
    budget = plural(settings.photo_budget, "photo")
    if auto_state.stopped_by is not None:
        return _AUTO_FINISHING_TEXT.format(
            rounds=plural(auto_state.rounds_done, "round"),
            photos=auto_state.photos_used,
            budget=budget,
        )
    return _AUTO_STATUS_TEXT.format(
        round=min(auto_state.rounds_done + 1, settings.max_rounds),
        rounds=settings.max_rounds,
        photos=auto_state.photos_used,
        budget=budget,
    )


def _active_run_status(run: Run | None, session: StudioSession) -> str | None:
    """What to show while a run is in flight for this session, or None when none is.

    An auto run shows how far it has got in place of the generic line.
    """
    if run is None or run.status != "running":
        return None
    auto_status = _auto_status(run, session)
    if auto_status is not None:
        return auto_status
    template = _RUN_STATUS_TEXT.get(run.kind, "Working…")
    return template.format(samples=plural(session.sample_count, "sample"))


def _failed_run_context(
    store: Store, session: StudioSession, rounds: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The session's newest run, for a retry callout, when it failed or was interrupted.

    A stage of an automatic session stands for the auto run it belongs to, so the callout
    shows that run's status, error and page. The editor's Ask runs (v5) are left out: a
    failed one is answered in the editor, and a later one must not hide a real failure.
    """
    runs = [run for run in store.list_runs_for_session(session.id) if run.kind != "edit"]
    run = runs[0] if runs else None
    if run is not None and run.parent_run_id:
        run = store.get_run(run.parent_run_id) or run
    if run is None or run.status not in ("failed", "interrupted"):
        return None
    latest_feedback = rounds[0]["feedback"] if rounds else None
    return {"run": run, "retry_text": latest_feedback.text if latest_feedback else ""}


def _current_candidates(store: Store, session: StudioSession, sample: Sample) -> list[LayoutCandidate]:
    """The sample's layout candidates made with the session's current words.

    Candidates made from an older prompt version carry old words, so the page leaves them
    out; they stay on disk and in the store.
    """
    return [
        candidate
        for candidate in store.list_layout_candidates(session.id, sample.id)
        if candidate.prompt_version_id == session.current_prompt_version_id
    ]


def _remaining_compositions(deps: Deps, session: StudioSession, sample: Sample) -> list[Composition]:
    """The shortlist's compositions for `sample` that are not yet a current-words candidate in this session.

    Used both to decide whether "More layouts" has anything to show, and to build the
    list `run_compose` renders when it is pressed. The hint is the session's current prompt
    version's layout, matching `run_compose`'s own `load_pick` step; compositions are
    compared by their fields, not by identity, so a composition already rendered, however
    it got there, is never rendered again. A session without a usable current version (should
    not happen once a sample can be picked) falls back to "hero" rather than raising, since
    this also runs on every session-page load.
    """
    store = deps.store
    version = (
        store.get_prompt_version(session.current_prompt_version_id)
        if session.current_prompt_version_id
        else None
    )
    hint: LayoutTemplate = version.layout if version else "hero"
    full = shortlist(sample.review, hint, allowed_templates(deps.kit))
    seen = {candidate.composition.model_dump_json() for candidate in _current_candidates(store, session, sample)}
    return [composition for composition in full if composition.model_dump_json() not in seen]


def _layout_context(deps: Deps, session: StudioSession) -> dict[str, Any]:
    """The Layout part: the picked sample's candidates so far, and whether more are left."""
    store = deps.store
    sample = store.get_sample(session.picked_sample_id) if session.picked_sample_id else None
    candidates = _current_candidates(store, session, sample) if sample else []
    has_more = sample is not None and sample.image_path is not None and bool(
        _remaining_compositions(deps, session, sample)
    )
    return {
        "candidates": [
            {"candidate": candidate, "caption": describe(candidate.composition)} for candidate in candidates
        ],
        "has_more_layouts": has_more,
        "allowed_templates": allowed_templates(deps.kit),
        "show_layout": session.status == "composing" or bool(candidates),
    }


# ---------------------------------------------------- research and directions


def _web_link(url: str) -> str | None:
    """The address as the page may link it: a web address only, so a source's address can
    never run script on the page. None for anything else; the page shows the title alone."""
    address = url.strip()
    try:
        parts = urlsplit(address)
    except ValueError:
        return None
    return address if parts.scheme in ("http", "https") and parts.netloc else None


def _research_status_line(research: ResearchReport, *, has_directions: bool) -> str:
    """The Research part's first line: grounded in how many sources, made up, or why not
    grounded, and then, when there are directions, where they came from instead."""
    if research.status == "grounded":
        return _RESEARCH_GROUNDED.format(sources=plural(len(research.sources), "source"))
    if research.status == "demo":
        return _RESEARCH_DEMO
    line = _RESEARCH_UNGROUNDED.format(problem=research.problem.strip())
    if has_directions:
        line = f"{line} {_RESEARCH_UNGROUNDED_DIRECTIONS}"
    return " ".join(line.split())


def _research_view(
    research: ResearchReport | None, *, has_directions: bool
) -> dict[str, Any] | None:
    """The research as the Research part shows it, or None when the session has none.

    The report and the sources' titles are web text: the template escapes them, and a source
    whose address is not a web address is shown without a link.
    """
    if research is None:
        return None
    return {
        "status": research.status,
        "status_line": _research_status_line(research, has_directions=has_directions),
        "text": research.text.strip(),
        "queries": [query.strip() for query in research.queries if query.strip()],
        "sources": [
            {
                "number": source.number,
                "title": source.title.strip() or source.url,
                "domain": source.domain,
                "url": _web_link(source.url),
            }
            for source in research.sources
        ],
        "via_tavily": research.provider == "tavily",
    }


def _direction_rows(session: StudioSession) -> list[dict[str, Any]]:
    """One card per direction, with its format and layout in words and the source chips that
    match a numbered source of the research."""
    numbers = {source.number for source in session.research.sources} if session.research else set()
    return [
        {
            "direction": direction,
            "format_label": direction.format.capitalize(),
            "layout_label": _LAYOUT_LABELS.get(direction.layout, direction.layout),
            # Subject, framing and mood on one line, each without its closing full stop.
            "look": " · ".join(
                part.strip().rstrip(".")
                for part in (direction.subject, direction.framing, direction.mood)
                if part.strip()
            ),
            "chips": [number for number in direction.source_numbers if number in numbers],
            "facts_text": "\n".join(direction.facts),
            "recommended": direction.number == session.recommended_direction,
        }
        for direction in session.directions
    ]


def _no_directions_note(store: Store, session: StudioSession) -> str:
    """Why the latest scout run brought back no directions, as its propose step says, while the
    session waits with none; "" otherwise."""
    if session.directions or session.status != "choosing":
        return ""
    scout = next((run for run in store.list_runs_for_session(session.id) if run.kind == "scout"), None)
    if scout is None:
        return ""
    notes = [event.note for event in store.list_events(scout.id) if event.step == "propose"]
    return notes[-1] if notes else ""


def _research_parts_context(
    deps: Deps, session: StudioSession, *, scout_running: bool
) -> dict[str, Any]:
    """The session page's Research and Directions parts (v4)."""
    directions = session.directions
    research_view = _research_view(session.research, has_directions=bool(directions))
    return {
        "research_enabled": deps.kit.research.enabled,
        "scout_running": scout_running,
        "research_view": research_view,
        "show_research": scout_running or research_view is not None,
        # Once a direction is chosen, the research folds away under its heading.
        "research_open": scout_running or session.chosen_direction is None,
        "direction_rows": _direction_rows(session),
        "directions_alike": len(directions) > 1 and directions_alike(directions, deps.kit),
        "no_directions_note": _no_directions_note(deps.store, session),
        "show_directions": bool(directions)
        or session.chosen_direction is not None
        or session.status == "choosing",
        "direction_formats": [
            {"value": value, "label": value.capitalize()} for value in _DIRECTION_FORMATS
        ],
        "direction_layouts": [
            {"value": value, "label": _LAYOUT_LABELS.get(value, value)}
            for value in allowed_templates(deps.kit)
        ],
    }


def _find_direction(session: StudioSession, number: int) -> Direction:
    """The session's direction `number`. 404 when it has none by that number."""
    direction = next((item for item in session.directions if item.number == number), None)
    if direction is None:
        raise _not_found()
    return direction


def _one_line(value: str) -> str:
    """A typed field as one line, cut to the most a direction's field keeps."""
    return " ".join(value.split())[:_MAX_DIRECTION_FIELD_CHARS]


def _edited_direction(
    original: Direction,
    texts: dict[str, str],
    why: str,
    picture: str,
    layout: str,
    facts: str,
    allowed: list[LayoutTemplate],
) -> Direction:
    """The direction as "Edit and use" sent it, marked edited when anything differs.

    A text field left blank keeps the direction's own text, except the why, which may be
    cleared; a format or a layout the studio does not offer keeps the direction's own. The
    facts are one to a line. Its sources, and whether it needs the designer's own photo, stay
    as the direction had them.
    """
    changes: dict[str, Any] = {
        name: _one_line(value) or getattr(original, name) for name, value in texts.items()
    }
    changes["why"] = _one_line(why)
    changes["format"] = picture if picture in _DIRECTION_FORMATS else original.format
    changes["layout"] = layout if layout in allowed else original.layout
    lines = (_one_line(line) for line in _normalise_textarea(facts).split("\n"))
    changes["facts"] = [line for line in lines if line][:_MAX_DIRECTION_FACTS]
    edited = original.model_copy(update=changes)
    if edited.model_dump(exclude={"edited"}) == original.model_dump(exclude={"edited"}):
        return original.model_copy()
    return edited.model_copy(update={"edited": True})


def _start_scout(
    request: Request, session: StudioSession, *, from_research: bool = False
) -> RedirectResponse:
    """Start a scout run for the session, from its saved research for "Other directions", and
    go back to its page."""
    deps: Deps = request.app.state.deps
    run = deps.store.create_run("scout", deps.kit.id, brief=session.brief, session_id=session.id)
    session.active_run_id = run.id
    deps.store.save_session(session)

    jobs: RunJobs = request.app.state.jobs
    jobs.start(run.id, run_scout(deps, run, session, from_research=from_research))
    return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)


def _draft_with_direction(
    request: Request, session: StudioSession, direction: Direction | None
) -> RedirectResponse:
    """Make `direction` the session's chosen one (None for "Draft without a direction"), start
    the draft that follows it, and go back to the session's page. The rounds so far stay."""
    deps: Deps = request.app.state.deps
    session.chosen_direction = direction
    run = deps.store.create_run("draft", deps.kit.id, brief=session.brief, session_id=session.id)
    session.active_run_id = run.id
    deps.store.save_session(session)

    jobs: RunJobs = request.app.state.jobs
    jobs.start(run.id, run_draft(deps, run, session))
    return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)


def _session_context(
    deps: Deps, session: StudioSession, *, error: str | None = None, references_line: str = ""
) -> dict[str, Any]:
    """The session page. `references_line` is the status line after references were sent."""
    store = deps.store
    versions = store.list_prompt_versions(session.id)
    current = next((version for version in versions if version.id == session.current_prompt_version_id), None)
    previous = (
        next((version for version in versions if version.number == current.number - 1), None)
        if current
        else None
    )
    latest_agent_version = next((version for version in reversed(versions) if version.author != "designer"), None)
    active_run = store.get_run(session.active_run_id) if session.active_run_id else None
    is_running = active_run is not None and active_run.status == "running"
    # The auto run's own kind, not the session's mode, which a restart can leave behind.
    auto_running = is_running and active_run is not None and active_run.kind == "auto"
    # A scout run of its own, which the Research part follows; an auto run's research stage
    # is followed by the auto run's status line instead.
    scout_running = is_running and active_run is not None and active_run.kind == "scout"
    auto_state = session.auto_state
    rounds = _round_groups(store, session.id)
    return {
        "session": session,
        "current_version": current,
        "current_version_label": _version_label(current) if current else "",
        "hashtags_text": " ".join(current.hashtags) if current else "",
        "show_reset": latest_agent_version is not None,
        "max_samples": deps.settings.max_samples,
        "rounds": rounds,
        "post": store.get_post(session.post_id) if session.post_id else None,
        "is_running": is_running,
        "active_status": _active_run_status(active_run, session),
        "active_step_note": _latest_step_note(store, active_run) if is_running else "",
        "auto_running": auto_running,
        "auto_decisions": auto_state.decisions if auto_state else [],
        # Once the auto run is over, however it ended (a restart included), its decisions
        # stay on the page under "How it was made".
        "show_auto_trace": auto_state is not None and not auto_running,
        "final_review": auto_state.final_review if auto_state else None,
        # "" when the final check was skipped.
        "picked_layout": auto_state.picked_layout if auto_state else "",
        "quality_bar_url": _quality_bar_view(deps)["url"],
        "failed_run": _failed_run_context(store, session, rounds),
        "photos_today": store.count_photos_since(_midnight_utc()),
        "previous_version": previous,
        "diff_html": _word_diff_html(previous.photo_prompt, current.photo_prompt)
        if previous and current
        else None,
        "source_sample": store.get_sample(session.source_sample_id) if session.source_sample_id else None,
        "draft_button_label": "Draft again" if session.current_prompt_version_id else "Draft",
        # v5: the designer's references, oldest first, beside the quality bar.
        "references": designer_references(store, session.id),
        "references_line": references_line,
        **_layout_context(deps, session),
        **_research_parts_context(deps, session, scout_running=scout_running),
        "error": error,
    }


# ----------------------------------------------------------------- archive


def _list_archive_for_filter(store: Store, filter_value: str) -> tuple[str, list[Sample]]:
    active_filter = filter_value if filter_value in _ARCHIVE_FILTERS else "all"
    status = "picked" if active_filter == "picked" else None
    reaction = _ARCHIVE_REACTION_FILTERS.get(active_filter)
    return active_filter, store.list_archive(reaction=reaction, status=status)


def _archive_context(
    store: Store, samples: list[Sample], active_filter: str, *, message: str | None = None
) -> dict[str, Any]:
    version_cache: dict[str, PromptVersion | None] = {}
    rows = []
    for sample in samples:
        if sample.prompt_version_id not in version_cache:
            version_cache[sample.prompt_version_id] = store.get_prompt_version(sample.prompt_version_id)
        version = version_cache[sample.prompt_version_id]
        prompt_text = version.photo_prompt if version else ""
        rows.append(
            {
                "sample": sample,
                "prompt_excerpt": prompt_text[:_PROMPT_EXCERPT_CHARS],
                "prompt_full": prompt_text,
                "prompt_is_long": len(prompt_text) > _PROMPT_EXCERPT_CHARS,
            }
        )
    return {
        "rows": rows,
        "active_filter": active_filter,
        "filters": [
            {"name": name, "label": _ARCHIVE_FILTER_LABELS.get(name, name.capitalize())}
            for name in _ARCHIVE_FILTERS
        ],
        "disliked_total": len(store.list_archive(reaction="disliked")),
        "message": message,
    }


def _delete_result_message(deleted: int, skipped: int) -> str:
    message = f"Deleted {deleted} photo{'s' if deleted != 1 else ''}."
    if skipped:
        message += f" Skipped {skipped} photo{'s' if skipped != 1 else ''} used by a post."
    return message


def _session_from_archive(deps: Deps, sample_id: str) -> tuple[StudioSession, Sample]:
    """A new session started from an archived photo, and the photo. 404 when it has no image."""
    sample = deps.store.get_sample(sample_id)
    if sample is None or sample.image_path is None or sample.status == "deleted":
        raise _not_found()
    session = StudioSession(brand_id=deps.kit.id, brief="", source_sample_id=sample.id)
    deps.store.save_session(session)
    return session, sample


def _editor_redirect(
    session: StudioSession, sample: Sample, from_candidate: str | None = None
) -> RedirectResponse:
    """To the editor on the session's photo, starting from the layout `from_candidate` when given."""
    url = f"/sessions/{session.id}/editor?sample={sample.id}"
    if from_candidate:
        url += f"&from={from_candidate}"
    return RedirectResponse(url=url, status_code=303)


def _open_in_editor(
    deps: Deps,
    sample_id: str,
    *,
    from_candidate: str | None = None,
    words_of: Post | None = None,
) -> RedirectResponse:
    """A new session that starts from an already-made photo, opened at once in the editor.

    The photo is copied into the session as its round 0 pick straight away, so the editor
    has a photo of the session's own to work on. `from_candidate`, a layout of that photo,
    is where the editor starts. `words_of`, a post, gives the session the post's words and
    mode first, so the editor opens with them instead of blank words in dark mode.
    """
    store = deps.store
    session, sample = _session_from_archive(deps, sample_id)
    if words_of is not None:
        spec = words_of.spec
        _save_editor_words(store, session, spec.headline, spec.subline, spec.mode)
        store.save_session(session)
    copy = pick_source_photo(store, session, sample, None)
    return _editor_redirect(session, copy, from_candidate)


def _post_photo(store: Store, post: Post) -> Sample | None:
    """The photo the post shows, or None for a words-only post or one whose photo has no image."""
    if post.spec.layout not in NEEDS_PHOTO or not post.sample_id:
        return None
    sample = store.get_sample(post.sample_id)
    return sample if sample is not None and sample.image_path is not None else None


def _editor_picker_context(store: Store, *, error: str | None = None) -> dict[str, Any]:
    """The Editor tab: the posts that show a photo, and the archive's photos, each file once,
    newest first."""
    posts = [post for post in store.list_posts() if _post_photo(store, post) is not None]
    photos: list[Sample] = []
    seen: set[str] = set()
    for sample in store.list_archive():
        if sample.image_path is None or sample.image_path in seen:
            continue
        seen.add(sample.image_path)
        photos.append(sample)
    return {"posts": posts, "photos": photos, "error": error}


# ---------------------------------------------------------- editor and uploads


def _current_version(store: Store, session: StudioSession) -> PromptVersion | None:
    """The session's current prompt version, or None when it has none yet."""
    version_id = session.current_prompt_version_id
    return store.get_prompt_version(version_id) if version_id else None


def _editor_sample(store: Store, session: StudioSession, sample_id: str) -> Sample:
    """The session's photo the editor works on. 404 when it is missing, deleted or has no image."""
    sample = store.get_sample(sample_id) if sample_id else None
    if (
        sample is None
        or sample.session_id != session.id
        or sample.status == "deleted"
        or sample.image_path is None
    ):
        raise _not_found()
    return sample


def _starting_layout(store: Store, session: StudioSession, candidate_id: str) -> CustomLayout:
    """Where the editor starts: the candidate `candidate_id` as blocks, or the default.

    The candidate is one of this session's, or, for a session started from a photo, a layout
    of that photo made in its own session (the Editor tab opens a post's arrangement that
    way). A custom candidate gives back its own arrangement. A template candidate becomes
    blocks that follow its template, with its shade when it drew one.
    """
    candidate = store.get_layout_candidate(candidate_id) if candidate_id else None
    of_source_photo = (
        candidate is not None
        and session.source_sample_id is not None
        and candidate.sample_id == session.source_sample_id
    )
    if candidate is None or (candidate.session_id != session.id and not of_source_photo):
        return default_layout()
    composition = candidate.composition
    if composition.template == "custom":
        return composition.custom or default_layout()
    return blocks_for_template(composition, scrim_added=candidate.render_report.scrim_added)


def _editor_context(
    deps: Deps, session: StudioSession, sample: Sample, layout: CustomLayout
) -> dict[str, Any]:
    """The editor page: the photo, the current version's words and mode, and what the canvas needs.

    The script reads the arrangement, the palette, the colours of each mode's roles, the post
    size, the kit's type settings and the faces a post may use from `editor_data`, so it draws
    what the renderer draws.
    """
    kit = deps.kit
    version = _current_version(deps.store, session)
    words: dict[str, str] = {
        "headline": version.headline if version else "",
        "subline": version.subline if version else "",
        "mode": version.mode if version else "dark",
    }
    typography = kit.typography
    return {
        "session": session,
        "sample": sample,
        "palette": kit.colours,
        **words,
        "editor_data": {
            "session_id": session.id,
            "sample_id": sample.id,
            "layout": layout.model_dump(mode="json"),
            **words,
            "palette": [{"name": colour.name, "hex": colour.hex} for colour in kit.colours],
            "roles": {mode: kit.mode_hex(mode) for mode in ("dark", "light")},
            "post_size": kit.post_size.model_dump(),
            "typography": {
                "headline_weight": typography.headline_weight,
                "body_weight": typography.body_weight,
                "headline_tracking": typography.headline_tracking,
            },
            "faces": [_face_entry(face) for face in available_faces(kit)],
        },
    }


def _face_entry(face: Face) -> dict[str, str | None]:
    """A face as the editor lists it: its name and the addresses of its font files.

    The italic address is None for a face without italics.
    """
    name = quote(face.name, safe="")
    return {
        "name": face.name,
        "regular_url": f"/brand/font?face={name}&style=regular",
        "italic_url": f"/brand/font?face={name}&style=italic" if face.italic else None,
    }


def _save_editor_words(
    store: Store, session: StudioSession, headline: str, subline: str, mode: Mode
) -> None:
    """Make the editor's words and mode the session's current version, when they differ from it.

    The new designer version copies the current one with those three fields. With no version
    yet, it keeps the chosen photo as it is, with no caption or hashtags, in the hero layout.
    """
    current = _current_version(store, session)
    wanted = (headline, subline, mode)
    if current is not None and (current.headline, current.subline, current.mode) == wanted:
        return
    number = len(store.list_prompt_versions(session.id)) + 1
    if current is None:
        version = PromptVersion(
            session_id=session.id,
            number=number,
            author="designer",
            photo_prompt=_EDITOR_PHOTO_PROMPT,
            headline=headline,
            subline=subline,
            mode=mode,
        )
    else:
        words_changed = (current.headline, current.subline) != (headline, subline)
        version = current.model_copy(
            update={
                "id": new_id(),
                "number": number,
                "author": "designer",
                "headline": headline,
                "subline": subline,
                "mode": mode,
                # The agent's notes on its last change, and on words that are no longer its
                # own, do not describe this version.
                "changes": [],
                "reason_words": "" if words_changed else current.reason_words,
                "created_at": now(),
            }
        )
    store.save_prompt_version(version)
    session.current_prompt_version_id = version.id


def _upload_entry(upload: Upload) -> dict[str, Any]:
    """An upload as the editor's picker reads it: its id, its address under /media/, its name
    and its size in pixels."""
    return {
        "id": upload.id,
        "url": f"/media/{upload.image_path}",
        "name": upload.name,
        "width": upload.width,
        "height": upload.height,
    }


def _next_upload_index(store: Store, session: StudioSession) -> int:
    """The index of the session's next photo in round 0: one past the highest so far."""
    indexes = [sample.index for sample in store.list_samples(session.id) if sample.round == 0]
    return max(indexes, default=0) + 1


def _save_upload(store: Store, session: StudioSession, data: bytes, extension: str) -> Sample:
    """Save the designer's own photo into the session as a photo of round 0, and give it back."""
    path = store.uploads_dir / session.id / f"{new_id()}.{extension}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    sample = Sample(
        session_id=session.id,
        # No prompt made this photo, so the archive shows none for it.
        prompt_version_id="",
        round=0,
        index=_next_upload_index(store, session),
        image_path=store.relative(path),
        provider="upload",
        status="candidate",
    )
    store.save_sample(sample)
    return sample


def _brand_upload(store: Store, brand_id: str, upload_id: str) -> Upload | None:
    """The brand's upload by its id while its file is still in the brand's uploads folder;
    None for an unknown id, another brand's upload, or one whose file has gone."""
    upload = store.get_upload(upload_id) if upload_id else None
    if upload is None or upload.brand_id != brand_id:
        return None
    if upload_file(upload.image_path, store.uploads_dir, brand_id) is None:
        return None
    return upload


async def _upload_picture(store: Store, upload: Upload) -> Path:
    """The file the editor agent is sent for an upload (v5): its agent copy, upright and at
    most 2048 pixels on its longer side, written now when it has none, since an upload can
    be 15 MB and goes beside the layout's own picture; or the upload itself when no copy can
    be written. A copy goes with its upload when the upload is removed."""
    for transparent in (False, True):
        copy = agent_copy_path(upload.image_path, transparent=transparent)
        found = upload_file(copy, store.uploads_dir, upload.brand_id)
        if found is not None:
            return found
    try:
        relative = await asyncio.to_thread(_write_upload_copy, store, upload)
        return store.media_path(relative)
    except Exception as error:
        # Any failure, Pillow's or the disk's, only costs the agent a smaller picture.
        logger.warning("The agent copy of an upload was not written: %r", error)
        return store.media_path(upload.image_path)


def _write_upload_copy(store: Store, upload: Upload) -> str:
    """Write an upload's agent copy from its file, and give back the copy's media path.
    Reading and decoding are blocking work, so this runs off the event loop."""
    data = store.media_path(upload.image_path).read_bytes()
    return write_agent_copy(store, upload.image_path, data)


def _edited_layout(
    deps: Deps, layout: CustomLayout, answer: EditorAnswer, mode: Mode
) -> tuple[CustomLayout, list[str]]:
    """The layout with the editor agent's edits applied, then the guardrails, and every line
    saying what was dropped or changed: the edits' own lines first, then the guardrails'.

    An edit can name any of the brand's uploads, each placed with its own proportions."""
    store, kit = deps.store, deps.kit
    uploads = {upload.id: upload for upload in store.list_uploads(kit.id)}
    canvas = kit.post_size

    def upload_path(upload_id: str) -> str | None:
        upload = uploads.get(upload_id)
        return upload.image_path if upload is not None else None

    def upload_ratio(upload_id: str) -> float | None:
        upload = uploads.get(upload_id)
        if upload is None or not upload.width or not upload.height:
            return None
        # Percent runs across the canvas's width and down its height, so a picture's height
        # for each unit of width, in percent, is its own proportion times the canvas's.
        return (upload.height / upload.width) * (canvas.width / canvas.height)

    edited, lines = apply_edits(
        layout, answer.edits, upload_path=upload_path, upload_ratio=upload_ratio
    )
    guarded, guardrail_lines = apply_guardrails(edited, kit, mode, uploads_root=store.uploads_dir)
    return guarded, [*lines, *(line for line in guardrail_lines if line not in lines)]


# ------------------------------------------------------- session references (v5)


async def _attach_references(
    deps: Deps, session: StudioSession, files: list[UploadFile]
) -> list[str]:
    """Keep the chosen files as the session's references, in the order given, until it has
    six, and give back the codes of what the session page's status line should say.

    Each file is read and checked as every upload is, kept on the brand's shelf, given its
    agent copy and recorded on the session. A file the studio cannot use is skipped
    ("refused"); once the session has six, the files left are not read ("limited"). Then the
    analyst cards them all at once, so the wait is one card's at most, never six in a row.
    """
    store = deps.store
    room = _MAX_SESSION_REFERENCES - len(designer_references(store, session.id))
    codes: list[str] = []
    # Each reference with what its card is made from: bytes, their extension and a label.
    to_card: list[tuple[SessionReference, bytes, str, str]] = []
    for file in files:
        if not file.filename and not file.size:
            continue  # the empty part a browser sends when no file was chosen
        if len(to_card) >= room:
            codes.append("limited")
            break
        try:
            data, extension, width, height = await read_image_upload(file)
        except UploadRejected:
            if "refused" not in codes:
                codes.append("refused")
            continue
        upload = save_brand_upload(
            store,
            deps.kit.id,
            name=Path(file.filename or "").name,
            data=data,
            extension=extension,
            width=width,
            height=height,
            source="session",
        )
        reference = SessionReference(
            session_id=session.id, upload_id=upload.id, image_path=upload.image_path
        )
        # Recorded before the analyst is asked, so a card that never comes leaves it in place.
        store.add_session_reference(reference)
        card_data, card_extension = await _agent_copy(store, upload, data, extension)
        to_card.append((reference, card_data, card_extension, upload.name))
    cards = await asyncio.gather(
        *(_reference_card(deps, image, kind, label) for _, image, kind, label in to_card)
    )
    for (reference, *_), card in zip(to_card, cards, strict=True):
        if card is not None:
            reference.card = card
            store.update_session_reference(reference)
    return codes


async def _agent_copy(
    store: Store, upload: Upload, data: bytes, extension: str
) -> tuple[bytes, str]:
    """Write the reference's agent copy beside its upload, off the event loop, and give back
    what the analyst reads: the copy's bytes and extension. When no copy can be written, the
    original's, which the models are then sent too; a reference never fails for want of one."""
    try:
        relative = await asyncio.to_thread(write_agent_copy, store, upload.image_path, data)
        copy = store.media_path(relative)
        return copy.read_bytes(), copy.suffix.lstrip(".")
    except Exception as error:
        # Any failure, Pillow's or the disk's, only costs the models a smaller picture.
        logger.warning("The agent copy of a reference was not written: %r", error)
        return data, extension


async def _reference_card(
    deps: Deps, data: bytes, extension: str, label: str
) -> StyleCard | None:
    """The analyst's style card for one reference image, written as it is uploaded, through
    the Library's own analysis. None when no card comes: a model error, an answer that does
    not fit, or no answer within 20 seconds; the reference is kept either way. In demo mode
    the stand-in analyst answers."""
    try:
        return await asyncio.wait_for(
            analyse_reference(deps.llm, data, _REFERENCE_MIME_TYPES[extension], label=label),
            timeout=_REFERENCE_CARD_SECONDS,
        )
    except Exception as error:
        # Only the type: the text of a model error may hold a request address.
        logger.warning("The analyst could not card a session reference: %s", type(error).__name__)
        return None


def _references_address(session_id: str, codes: list[str], *, fragment: str = "") -> str:
    """The session page's address after references were sent, with each status code in its
    query (`references=limited`) and an optional fragment to land on."""
    query = urlencode([("references", code) for code in codes])
    address = f"/sessions/{session_id}?{query}" if query else f"/sessions/{session_id}"
    return f"{address}#{fragment}" if fragment else address


def _references_status_line(codes: list[str]) -> str:
    """The session page's status line for the codes its address carries, in a fixed order;
    "" for none. A code the studio does not know is ignored."""
    return " ".join(line for code, line in _REFERENCE_STATUS_LINES.items() if code in codes)


def _session_reference(
    store: Store, session: StudioSession, reference_id: str
) -> SessionReference:
    """The session's reference `reference_id`. 404 when the session has none by that id."""
    found = next(
        (item for item in store.list_session_references(session.id) if item.id == reference_id),
        None,
    )
    if found is None:
        raise _not_found()
    return found


def _reference_note(value: str) -> str:
    """A typed note as one line, cut to the 80 characters a reference's note holds."""
    return " ".join(value.split())[:_MAX_REFERENCE_NOTE_CHARS].strip()


# ---------------------------------------------------------------------- pages


@router.get("/")
async def index() -> RedirectResponse:
    return RedirectResponse(url="/studio")


@router.get("/studio")
async def studio_page(request: Request) -> Response:
    deps: Deps = request.app.state.deps
    return _page(request, "studio.html", _studio_context(deps))


@router.post("/sessions")
async def create_session(
    request: Request,
    brief: Annotated[str, Form()],
    sample_count: Annotated[int, Form()] = 3,
    mode: Annotated[SessionMode, Form()] = "manual",
    max_rounds: Annotated[int, Form()] = 3,
    photo_budget: Annotated[int, Form()] = 6,
    stop_score: Annotated[float, Form()] = 4.2,
    research: Annotated[bool | None, Form()] = None,
    research_choice: Annotated[bool, Form()] = False,
    references: Annotated[list[UploadFile] | None, File()] = None,
) -> Response:
    """Start a session from the brief: a draft by hand, or the whole session on its own in auto mode.

    With "Research first" ticked, and the kit allowing it, a manual session starts with the
    scout run instead of the draft, and waits for a direction; an auto run researches as its
    own stage. An unticked box sends nothing, so the page's script also sends the box's
    hidden companion (`research_choice`): with it, the box is followed as it is. With neither
    (no script ran), the field is absent, and an auto session researches by default.

    The reference images (v5) are kept on the session before its first run starts, so every
    agent of that run sees them; files past the sixth, and files the studio cannot use, are
    left out and the session page's status line says so.
    """
    deps: Deps = request.app.state.deps
    stripped = brief.strip()
    if not stripped:
        return _page(
            request,
            "studio.html",
            _studio_context(deps, error="Write a brief first.", brief_value=brief),
            status_code=400,
        )

    field_absent = research is None and not research_choice
    wanted = mode == "auto" if field_absent else bool(research)
    session = StudioSession(
        brand_id=deps.kit.id,
        brief=stripped[:_MAX_BRIEF_CHARS],
        sample_count=_clamp_sample_count(sample_count, deps.settings.max_samples),
        research_on=wanted and deps.kit.research.enabled,
    )
    if mode == "auto":
        session.mode = "auto"
        session.auto_settings = _auto_settings(max_rounds, photo_budget, stop_score)
        session.auto_state = AutoState()
    deps.store.save_session(session)
    codes = await _attach_references(deps, session, references or [])

    kind: RunKind = "auto" if mode == "auto" else "scout" if session.research_on else "draft"
    run = deps.store.create_run(kind, deps.kit.id, brief=session.brief, session_id=session.id)
    session.active_run_id = run.id
    deps.store.save_session(session)

    jobs: RunJobs = request.app.state.jobs
    if kind == "auto":
        jobs.start(run.id, run_auto(deps, run, session))
    elif kind == "scout":
        # The session stays waiting for its brief until the directions are saved.
        jobs.start(run.id, run_scout(deps, run, session))
    else:
        jobs.start(run.id, run_draft(deps, run, session))
    return RedirectResponse(url=_references_address(session.id, codes), status_code=303)


@router.get("/sessions/{session_id}")
async def session_page(
    request: Request,
    session_id: str,
    reference_codes: Annotated[list[str] | None, Query(alias="references")] = None,
) -> Response:
    """The session page; `references` in its address names what its status line says after
    references were sent."""
    deps: Deps = request.app.state.deps
    session = _require_session(deps.store, session_id)
    line = _references_status_line(reference_codes or [])
    return _page(request, "session.html", _session_context(deps, session, references_line=line))


@router.post("/sessions/{session_id}/references")
async def add_references(
    request: Request,
    session_id: str,
    references: Annotated[list[UploadFile] | None, File()] = None,
) -> RedirectResponse:
    """Add the session page's chosen files to the session's references, up to six in all, and
    go back to its References group, whose status line says what was left out."""
    deps: Deps = request.app.state.deps
    session = _require_session(deps.store, session_id)
    codes = await _attach_references(deps, session, references or [])
    return RedirectResponse(
        url=_references_address(session.id, codes, fragment="references"), status_code=303
    )


@router.post("/sessions/{session_id}/references/{reference_id}/note")
async def save_reference_note(
    request: Request, session_id: str, reference_id: str, note: Annotated[str, Form()] = ""
) -> RedirectResponse:
    """Save a reference's note as one line of at most 80 characters; blank clears it."""
    store: Store = request.app.state.store
    session = _require_session(store, session_id)
    reference = _session_reference(store, session, reference_id)
    reference.note = _reference_note(note)
    store.update_session_reference(reference)
    return RedirectResponse(url=f"/sessions/{session.id}#references", status_code=303)


@router.post("/sessions/{session_id}/references/{reference_id}/remove")
async def remove_reference(
    request: Request, session_id: str, reference_id: str
) -> RedirectResponse:
    """Take a reference off the session. Its upload stays on the brand's shelf, where the
    editor's picker and the Library still offer it."""
    store: Store = request.app.state.store
    session = _require_session(store, session_id)
    reference = _session_reference(store, session, reference_id)
    store.delete_session_reference(reference.id)
    return RedirectResponse(url=f"/sessions/{session.id}#references", status_code=303)


@router.post("/sessions/{session_id}/brief")
async def redraft_brief(request: Request, session_id: str, brief: Annotated[str, Form()]) -> Response:
    deps: Deps = request.app.state.deps
    session = _require_session(deps.store, session_id)
    if _session_busy(deps.store, session):
        return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)

    stripped = brief.strip()
    if not stripped:
        return _page(
            request,
            "session.html",
            _session_context(deps, session, error="Write a brief first."),
            status_code=400,
        )

    session.brief = stripped[:_MAX_BRIEF_CHARS]
    run = deps.store.create_run("draft", deps.kit.id, brief=session.brief, session_id=session.id)
    session.active_run_id = run.id
    deps.store.save_session(session)

    jobs: RunJobs = request.app.state.jobs
    jobs.start(run.id, run_draft(deps, run, session))
    return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)


@router.post("/sessions/{session_id}/directions/{number}/use")
async def use_direction(request: Request, session_id: str, number: int) -> Response:
    """Draft from direction `number` as it is: a copy becomes the session's chosen direction."""
    deps: Deps = request.app.state.deps
    session = _require_session(deps.store, session_id)
    if _session_busy(deps.store, session):
        return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)
    direction = _find_direction(session, number)
    return _draft_with_direction(request, session, direction.model_copy())


@router.post("/sessions/{session_id}/directions/{number}/edit")
async def edit_direction(
    request: Request,
    session_id: str,
    number: int,
    title: Annotated[str, Form()] = "",
    picture: Annotated[str, Form(alias="format")] = "",
    angle: Annotated[str, Form()] = "",
    subject: Annotated[str, Form()] = "",
    framing: Annotated[str, Form()] = "",
    mood: Annotated[str, Form()] = "",
    layout: Annotated[str, Form()] = "",
    headline_idea: Annotated[str, Form()] = "",
    facts: Annotated[str, Form()] = "",
    why: Annotated[str, Form()] = "",
) -> Response:
    """Draft from direction `number` as the designer edited it; the copy is marked edited
    when anything was changed."""
    deps: Deps = request.app.state.deps
    session = _require_session(deps.store, session_id)
    if _session_busy(deps.store, session):
        return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)
    original = _find_direction(session, number)
    texts = {
        "title": title,
        "angle": angle,
        "subject": subject,
        "framing": framing,
        "mood": mood,
        "headline_idea": headline_idea,
    }
    direction = _edited_direction(
        original, texts, why, picture, layout, facts, allowed_templates(deps.kit)
    )
    return _draft_with_direction(request, session, direction)


@router.post("/sessions/{session_id}/directions/again")
async def other_directions(request: Request, session_id: str) -> Response:
    """Three new directions from the session's saved research, with no new search."""
    deps: Deps = request.app.state.deps
    session = _require_session(deps.store, session_id)
    if _session_busy(deps.store, session) or session.research is None:
        return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)
    return _start_scout(request, session, from_research=True)


@router.post("/sessions/{session_id}/research/again")
async def research_again(
    request: Request, session_id: str, brief: Annotated[str, Form()] = ""
) -> Response:
    """A new scout run: fresh searches, then three new directions.

    Sent from the brief form, it takes the brief as edited there, so a brief that got a
    question back can be researched again once it is better.
    """
    deps: Deps = request.app.state.deps
    session = _require_session(deps.store, session_id)
    if _session_busy(deps.store, session) or not deps.kit.research.enabled:
        return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)
    stripped = brief.strip()
    if stripped:
        session.brief = stripped[:_MAX_BRIEF_CHARS]
    session.research_on = True
    return _start_scout(request, session)


@router.post("/sessions/{session_id}/directions/skip")
async def draft_without_direction(request: Request, session_id: str) -> Response:
    """Draft from the brief alone, as a session without research does."""
    deps: Deps = request.app.state.deps
    session = _require_session(deps.store, session_id)
    if _session_busy(deps.store, session):
        return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)
    return _draft_with_direction(request, session, None)


@router.post("/sessions/{session_id}/generate")
async def generate_samples_route(
    request: Request,
    session_id: str,
    photo_prompt: Annotated[str, Form()],
    headline: Annotated[str, Form()],
    subline: Annotated[str, Form()] = "",
    caption: Annotated[str, Form()] = "",
    hashtags: Annotated[str, Form()] = "",
    mode: Annotated[Mode, Form()] = "dark",
    layout: Annotated[LayoutId, Form()] = "hero",
    sample_count: Annotated[int, Form()] = 3,
) -> Response:
    deps: Deps = request.app.state.deps
    session = _require_session(deps.store, session_id)
    if _session_busy(deps.store, session):
        return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)
    current = (
        deps.store.get_prompt_version(session.current_prompt_version_id)
        if session.current_prompt_version_id
        else None
    )

    cleaned = (
        _normalise_textarea(photo_prompt).strip(),
        headline.strip(),
        subline.strip(),
        _normalise_textarea(caption).strip(),
        _parse_hashtags(hashtags),
        mode,
        layout,
    )
    current_values = (
        (
            current.photo_prompt,
            current.headline,
            current.subline,
            current.caption,
            current.hashtags,
            current.mode,
            current.layout,
        )
        if current is not None
        else None
    )

    if current_values != cleaned:
        version = PromptVersion(
            session_id=session.id,
            number=len(deps.store.list_prompt_versions(session.id)) + 1,
            author="designer",
            photo_prompt=cleaned[0],
            headline=cleaned[1],
            subline=cleaned[2],
            caption=cleaned[3],
            hashtags=cleaned[4],
            mode=mode,
            layout=layout,
            # The designer's changes still follow the direction the version they edit followed.
            direction_title=current.direction_title if current is not None else "",
        )
        deps.store.save_prompt_version(version)
        session.current_prompt_version_id = version.id

    session.sample_count = _clamp_sample_count(sample_count, deps.settings.max_samples)
    run = deps.store.create_run("samples", deps.kit.id, brief=session.brief, session_id=session.id)
    session.active_run_id = run.id
    deps.store.save_session(session)

    jobs: RunJobs = request.app.state.jobs
    jobs.start(run.id, run_samples(deps, run, session))
    return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)


@router.post("/sessions/{session_id}/reset")
async def reset_prompt_version(request: Request, session_id: str) -> Response:
    store: Store = request.app.state.store
    session = _require_session(store, session_id)

    versions = store.list_prompt_versions(session.id)
    latest_agent_version = next((version for version in reversed(versions) if version.author != "designer"), None)
    if latest_agent_version is not None:
        session.current_prompt_version_id = latest_agent_version.id
        store.save_session(session)
    return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)


@router.post("/sessions/{session_id}/feedback")
async def submit_round_feedback(request: Request, session_id: str, text: Annotated[str, Form()] = "") -> Response:
    deps: Deps = request.app.state.deps
    session = _require_session(deps.store, session_id)
    if _session_busy(deps.store, session):
        return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)

    stripped = text.strip()[:_MAX_COMMENT_CHARS]
    if stripped:
        deps.store.save_round_feedback(RoundFeedback(session_id=session.id, round=session.rounds, text=stripped))

    run = deps.store.create_run("revise_prompt", deps.kit.id, brief=session.brief, comment=stripped, session_id=session.id)
    session.active_run_id = run.id
    deps.store.save_session(session)

    jobs: RunJobs = request.app.state.jobs
    jobs.start(run.id, run_revise_prompt(deps, run, session))
    return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)


@router.post("/sessions/{session_id}/again")
async def generate_again(
    request: Request, session_id: str, text: Annotated[str, Form()] = ""
) -> Response:
    deps: Deps = request.app.state.deps
    session = _require_session(deps.store, session_id)
    if _session_busy(deps.store, session):
        return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)

    stripped = text.strip()[:_MAX_COMMENT_CHARS]
    if stripped:
        deps.store.save_round_feedback(RoundFeedback(session_id=session.id, round=session.rounds, text=stripped))

    run = deps.store.create_run("samples", deps.kit.id, brief=session.brief, session_id=session.id)
    session.active_run_id = run.id
    deps.store.save_session(session)

    jobs: RunJobs = request.app.state.jobs
    jobs.start(run.id, run_samples(deps, run, session))
    return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)


@router.post("/sessions/{session_id}/use/{sample_id}")
async def use_sample(request: Request, session_id: str, sample_id: str) -> Response:
    """Start composing this sample's photo into the layouts that fit it."""
    deps: Deps = request.app.state.deps
    session = _require_session(deps.store, session_id)
    if _session_busy(deps.store, session):
        return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)
    sample = deps.store.get_sample(sample_id)
    if sample is None:
        raise _not_found()

    run = deps.store.create_run("compose", deps.kit.id, brief=session.brief, session_id=session.id)
    session.active_run_id = run.id
    session.picked_sample_id = sample.id
    deps.store.save_session(session)

    jobs: RunJobs = request.app.state.jobs
    jobs.start(run.id, run_compose(deps, run, session, sample))
    return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)


@router.post("/sessions/{session_id}/compose/more")
async def compose_more(request: Request, session_id: str) -> Response:
    """Render whatever the shortlist still has for the picked sample, beyond what is shown."""
    deps: Deps = request.app.state.deps
    session = _require_session(deps.store, session_id)
    if _session_busy(deps.store, session):
        return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)
    sample = deps.store.get_sample(session.picked_sample_id) if session.picked_sample_id else None
    if sample is None:
        raise _not_found()

    remaining = _remaining_compositions(deps, session, sample)
    run = deps.store.create_run("compose", deps.kit.id, brief=session.brief, session_id=session.id)
    session.active_run_id = run.id
    deps.store.save_session(session)

    jobs: RunJobs = request.app.state.jobs
    jobs.start(run.id, run_compose(deps, run, session, sample, compositions=remaining))
    return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)


@router.post("/sessions/{session_id}/compose/preview")
async def compose_preview(
    request: Request,
    session_id: str,
    template: Annotated[LayoutTemplate, Form()],
    logo_position: Annotated[LogoPosition, Form()] = "top_centre",
    text_position: Annotated[TextPosition, Form()] = "bottom",
    text_align: Annotated[TextAlign, Form()] = "centre",
    photo_side: Annotated[PhotoSide, Form()] = "top",
    scrim: Annotated[Scrim, Form()] = "auto",
) -> Response:
    """Render one hand-adjusted composition for the picked sample."""
    deps: Deps = request.app.state.deps
    session = _require_session(deps.store, session_id)
    if _session_busy(deps.store, session):
        return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)
    sample = deps.store.get_sample(session.picked_sample_id) if session.picked_sample_id else None
    if sample is None:
        raise _not_found()

    composition = Composition(
        template=template,
        logo_position=logo_position,
        text_position=text_position,
        text_align=text_align,
        photo_side=photo_side,
        scrim=scrim,
    )
    run = deps.store.create_run("compose", deps.kit.id, brief=session.brief, session_id=session.id)
    session.active_run_id = run.id
    deps.store.save_session(session)

    jobs: RunJobs = request.app.state.jobs
    jobs.start(run.id, run_compose(deps, run, session, sample, compositions=[composition]))
    return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)


@router.get("/sessions/{session_id}/editor")
async def editor_page(
    request: Request,
    session_id: str,
    sample: str = "",
    from_candidate: Annotated[str, Query(alias="from")] = "",
) -> Response:
    """The editor: the designer places the words, the logo and shades over a session's photo."""
    deps: Deps = request.app.state.deps
    session = _require_session(deps.store, session_id)
    chosen = _editor_sample(deps.store, session, sample)
    layout = _starting_layout(deps.store, session, from_candidate)
    return _page(request, "editor.html", _editor_context(deps, session, chosen, layout))


@router.post("/sessions/{session_id}/editor/preview")
async def editor_preview(
    request: Request, session_id: str, body: EditorPreviewBody
) -> JSONResponse:
    """Render the editor's arrangement as a custom candidate and wait for it, about a second.

    Words or a mode that differ from the current version become a new designer version
    first, so the candidate, and the post made from it, carry them. The candidate keeps the
    arrangement as the guardrails leave it (a logo the render adds included), so the editor
    reopens it as drawn; what the guardrails changed still heads the render's lines.
    """
    deps: Deps = request.app.state.deps
    store = deps.store
    if len(body.layout.blocks) > _MAX_EDITOR_BLOCKS:
        return JSONResponse({"error": _TOO_MANY_BLOCKS}, status_code=422)
    session = _require_session(store, session_id)
    if _session_busy(store, session):
        return JSONResponse({"error": _SESSION_BUSY}, status_code=409)
    sample = _editor_sample(store, session, body.sample_id)
    _save_editor_words(store, session, body.headline.strip(), body.subline.strip(), body.mode)

    run = store.create_run("compose", deps.kit.id, brief=session.brief, session_id=session.id)
    session.active_run_id = run.id
    store.save_session(session)

    guarded, guardrail_lines = apply_guardrails(
        body.layout, deps.kit, body.mode, uploads_root=store.uploads_dir
    )
    composition = Composition(template="custom", custom=guarded)
    candidates = await run_compose(deps, run, session, sample, compositions=[composition])
    if not candidates:
        failed = store.get_run(run.id)
        return JSONResponse(
            {"error": failed.error if failed else None, "run_id": run.id}, status_code=500
        )
    candidate = candidates[0]
    if guardrail_lines:
        # The render applies the guardrails again to an arrangement that already keeps them,
        # so its own lines no longer say what they changed.
        report = candidate.render_report
        rest = [line for line in report.adjustments if line not in guardrail_lines]
        report.adjustments = [*guardrail_lines, *rest]
        store.save_layout_candidate(candidate)
    return JSONResponse(
        {
            "candidate_id": candidate.id,
            "image_url": f"/media/{candidate.image_path}",
            "fits": candidate.render_report.fits,
            "adjustments": candidate.render_report.adjustments,
            "run_id": run.id,
        }
    )


@router.post("/sessions/{session_id}/editor/ask")
async def editor_ask(request: Request, session_id: str, body: EditorAskBody) -> JSONResponse:
    """The editor's Ask mode (v5): the editor agent answers the designer's request with edits
    to the layout as sent, or with one question, and waits for it.

    The edit run renders the layout for the agent with the editor's words and mode (the
    session's saved ones where the page sent none), and records its one step. Code applies
    the edits to a copy, then the guardrails, and gives back the new layout with the agent's
    summary and a line for each edit that was dropped or changed. `words` is the headline and
    the subline when an edit changed either, which the page puts in its fields, else null; a
    question comes back with the layout as it was. Nothing is saved, and the session keeps no
    run in flight: the page loads the layout into the canvas, and "Use this layout" saves it
    after a preview, as before. An upload id that is not the brand's gives a 404; an agent
    that cannot answer gives a 502, and its run shows why.
    """
    deps: Deps = request.app.state.deps
    store = deps.store
    if len(body.layout.blocks) > _MAX_EDITOR_BLOCKS:
        return JSONResponse({"error": _TOO_MANY_BLOCKS}, status_code=422)
    session = _require_session(store, session_id)
    sample = _editor_sample(store, session, body.sample or session.picked_sample_id or "")
    upload = _brand_upload(store, deps.kit.id, body.upload_id) if body.upload_id else None
    if body.upload_id and upload is None:
        raise _not_found()
    version = _current_version(store, session)
    given = {"headline": body.headline, "subline": body.subline}
    saved = {"headline": version.headline, "subline": version.subline} if version else {}
    words = {
        kind: (text if text is not None else saved.get(kind, "")).strip()
        for kind, text in given.items()
    }
    mode: Mode = body.mode or (version.mode if version else "dark")
    picture = await _upload_picture(store, upload) if upload is not None else None

    run = store.create_run(
        "edit", deps.kit.id, brief=session.brief, comment=body.request, session_id=session.id
    )
    answer = await run_edit(
        deps,
        run,
        request=body.request,
        layout=body.layout,
        words=words,
        mode=mode,
        photo=store.media_path(sample.image_path),
        upload_id=upload.id if upload is not None else "",
        upload_picture=picture,
    )
    if answer is None:
        return JSONResponse({"error": _EDITOR_FAILED}, status_code=502)
    layout: CustomLayout = body.layout
    new_words: dict[str, str] | None = None
    dropped: list[str] = []
    if answer.usable:
        layout, dropped = _edited_layout(deps, body.layout, answer, mode)
        # The headline's and the subline's words are not in the layout, so they come back
        # on their own for the page's fields.
        new_words = edited_words(body.layout, answer.edits, words)
    return JSONResponse(
        {
            "usable": answer.usable,
            "question": answer.question if not answer.usable else "",
            "layout": layout.model_dump(mode="json"),
            "words": new_words,
            "summary": answer.summary if answer.usable else "",
            "dropped": dropped,
            "run_id": run.id,
        }
    )


@router.post("/sessions/{session_id}/upload")
async def upload_photo(
    request: Request, session_id: str, photo: Annotated[UploadFile, File()]
) -> Response:
    """Add the designer's own photo to the session as a photo of round 0, under "Your photos"."""
    deps: Deps = request.app.state.deps
    store = deps.store
    session = _require_session(store, session_id)
    # Reading one byte past the limit is enough to tell a file that is too large.
    data = await photo.read(MAX_UPLOAD_BYTES + 1)
    extension = upload_extension(data)
    if extension is None:
        return _page(
            request,
            "session.html",
            _session_context(deps, session, error=UPLOAD_REFUSED),
            status_code=400,
        )

    _save_upload(store, session, data, extension)
    return RedirectResponse(url=f"/sessions/{session.id}#rounds", status_code=303)


@router.post("/sessions/{session_id}/finish/{candidate_id}")
async def finish_session(request: Request, session_id: str, candidate_id: str) -> Response:
    """Finish the session with one rendered layout candidate, and open the post it makes."""
    deps: Deps = request.app.state.deps
    session = _require_session(deps.store, session_id)
    if _session_busy(deps.store, session):
        return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)
    candidate: LayoutCandidate | None = deps.store.get_layout_candidate(candidate_id)
    if candidate is None:
        raise _not_found()

    run = deps.store.create_run("finish", deps.kit.id, brief=session.brief, session_id=session.id)
    session.active_run_id = run.id
    deps.store.save_session(session)

    post = await run_finish(deps, run, session, candidate)
    if post is None:
        return RedirectResponse(url=f"/runs/{run.id}", status_code=303)
    return RedirectResponse(url=f"/posts/{post.id}", status_code=303)


@router.post("/sessions/{session_id}/stop")
async def stop_session_run(request: Request, session_id: str) -> Response:
    """Cancel whichever run is active for this session, if one is.

    A prompt revision handed off by a post comment runs inside the post's revise run, not
    as a task of its own, so when the active run has no task the session's running run
    that does is cancelled instead, and the revision stops with it.
    """
    store: Store = request.app.state.store
    session = _require_session(store, session_id)
    if session.active_run_id:
        jobs: RunJobs = request.app.state.jobs
        if not jobs.cancel(session.active_run_id):
            for run in store.list_runs_for_session(session.id):
                if run.status == "running" and jobs.cancel(run.id):
                    break
    return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)


@router.get("/api/sessions/{session_id}")
async def session_status(request: Request, session_id: str) -> JSONResponse:
    """What the session page polls: the run in flight and how far it has got, and the counts
    whose change means the page is drawn again."""
    store: Store = request.app.state.store
    session = store.get_session(session_id)
    if session is None:
        raise _not_found()
    run = store.get_run(session.active_run_id) if session.active_run_id else None
    picked = store.get_sample(session.picked_sample_id) if session.picked_sample_id else None
    return JSONResponse(
        {
            "session": session.model_dump(mode="json"),
            "run": run.model_dump(mode="json") if run is not None else None,
            "rounds": session.rounds,
            "samples": len(store.list_samples(session.id)),
            "candidates": len(_current_candidates(store, session, picked)) if picked else 0,
            "step_note": _latest_step_note(store, run),
            "child_note": _child_note(store, run),
            "auto_state": session.auto_state.model_dump(mode="json") if session.auto_state else None,
            "auto_status": _auto_status(run, session),
        }
    )


@router.post("/api/samples/{sample_id}/reaction")
async def set_sample_reaction(request: Request, sample_id: str, body: ReactionBody) -> JSONResponse:
    store: Store = request.app.state.store
    sample = store.get_sample(sample_id)
    if sample is None:
        raise _not_found()

    sample.reaction = body.reaction
    sample.comment = body.comment.strip()[:_MAX_COMMENT_CHARS]
    # A picked or deleted sample keeps its status; only a candidate and a rejected one move.
    if sample.status in ("candidate", "rejected"):
        sample.status = "rejected" if body.reaction == "disliked" else "candidate"
    store.save_sample(sample)
    return JSONResponse(sample.model_dump(mode="json"))


@router.get("/archive")
async def archive_page(request: Request, filter: str = "all", bar: str = "") -> Response:
    store: Store = request.app.state.store
    active_filter, samples = _list_archive_for_filter(store, filter)
    message = _QUALITY_BAR_IS_SET if bar == "set" else None
    return _page(
        request, "archive.html", _archive_context(store, samples, active_filter, message=message)
    )


@router.post("/archive/delete")
async def delete_selected_samples(
    request: Request,
    ids: Annotated[list[str], Form(default_factory=list)],
    filter: Annotated[str, Form()] = "all",
) -> Response:
    store: Store = request.app.state.store
    deleted = skipped = 0
    for sample_id in ids:
        try:
            ok = store.delete_sample(sample_id)
        except KeyError:
            continue
        if ok:
            deleted += 1
        else:
            skipped += 1

    active_filter, samples = _list_archive_for_filter(store, filter)
    message = _delete_result_message(deleted, skipped)
    return _page(request, "archive.html", _archive_context(store, samples, active_filter, message=message))


@router.post("/archive/delete-disliked")
async def delete_disliked_samples(request: Request, filter: Annotated[str, Form()] = "all") -> Response:
    store: Store = request.app.state.store
    deleted = skipped = 0
    for sample in store.list_archive(reaction="disliked"):
        if store.delete_sample(sample.id):
            deleted += 1
        else:
            skipped += 1

    active_filter, samples = _list_archive_for_filter(store, filter)
    message = _delete_result_message(deleted, skipped)
    return _page(request, "archive.html", _archive_context(store, samples, active_filter, message=message))


@router.post("/archive/{sample_id}/start")
async def start_session_from_archive(request: Request, sample_id: str) -> Response:
    """A new session that starts from an already-made photo, instead of a round of samples."""
    deps: Deps = request.app.state.deps
    session, _ = _session_from_archive(deps, sample_id)
    return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)


@router.post("/archive/{sample_id}/edit")
async def edit_from_archive(request: Request, sample_id: str) -> Response:
    """A new session that starts from an already-made photo, opened at once in the editor.

    The Editor tab's "Start from a photo" does the same; this stays for the archive's cards.
    """
    deps: Deps = request.app.state.deps
    return _open_in_editor(deps, sample_id)


@router.get("/editor")
async def editor_picker(request: Request) -> Response:
    """The Editor tab: start the editor from an upload, a post or a photo of the archive."""
    store: Store = request.app.state.store
    return _page(request, "editor_picker.html", _editor_picker_context(store))


@router.post("/editor/start")
async def start_in_editor(
    request: Request,
    post_id: Annotated[str, Form()] = "",
    sample_id: Annotated[str, Form()] = "",
    photo: Annotated[UploadFile | None, File()] = None,
) -> Response:
    """A new session behind the picker, opened at once in the editor on the chosen photo.

    A post opens on its photo with its words and mode, and with the session's chosen layout
    when that layout is one of the photo's; a photo of the archive opens as "Open in the
    editor" does; an upload becomes the session's photo of round 0, as on the session page,
    and its pick.
    """
    deps: Deps = request.app.state.deps
    store = deps.store
    if post_id:
        post = store.get_post(post_id)
        photo_sample = _post_photo(store, post) if post is not None else None
        if post is None or photo_sample is None:
            raise _not_found()
        candidate_id = _editor_start_candidate(store, post)
        return _open_in_editor(deps, photo_sample.id, from_candidate=candidate_id, words_of=post)
    if sample_id:
        return _open_in_editor(deps, sample_id)
    if photo is None:
        return RedirectResponse(url="/editor", status_code=303)

    # Reading one byte past the limit is enough to tell a file that is too large.
    data = await photo.read(MAX_UPLOAD_BYTES + 1)
    extension = upload_extension(data)
    if extension is None:
        return _page(
            request,
            "editor_picker.html",
            _editor_picker_context(store, error=UPLOAD_REFUSED),
            status_code=400,
        )
    session = StudioSession(brand_id=deps.kit.id, brief="")
    store.save_session(session)
    upload = _save_upload(store, session, data, extension)
    copy = pick_source_photo(store, session, upload, None)
    return _editor_redirect(session, copy)


@router.post("/editor/upload")
async def editor_upload(request: Request, image: Annotated[UploadFile, File()]) -> JSONResponse:
    """Keep the designer's image on the brand's shelf, for the editor to place.

    Gives back its id, address, name and size in pixels; a file the studio cannot use gives
    a 400 with the reason, which the editor shows.
    """
    deps: Deps = request.app.state.deps
    try:
        data, extension, width, height = await read_image_upload(image)
    except UploadRejected as rejected:
        return JSONResponse({"error": str(rejected)}, status_code=400)
    upload = save_brand_upload(
        deps.store,
        deps.kit.id,
        name=Path(image.filename or "").name,
        data=data,
        extension=extension,
        width=width,
        height=height,
        source="editor",
    )
    return JSONResponse(_upload_entry(upload))


@router.get("/api/uploads")
async def brand_uploads(request: Request) -> JSONResponse:
    """The brand's uploads, newest first, for the editor's picker."""
    deps: Deps = request.app.state.deps
    uploads = deps.store.list_uploads(deps.kit.id)
    return JSONResponse(
        {
            "uploads": [
                {**_upload_entry(upload), "created_at": upload.created_at.isoformat()}
                for upload in uploads
            ]
        }
    )


@router.post("/uploads/{upload_id}/remove")
async def remove_upload(request: Request, upload_id: str) -> RedirectResponse:
    """Take an upload off the brand's shelf: its file and its agent copy, every session
    reference that used it, then its row, and back to the Library.

    A saved post that places it keeps its rendered picture, but a later render leaves the
    image out, as the Library's hint says; the hint also says the references go.
    """
    deps: Deps = request.app.state.deps
    store = deps.store
    upload = store.get_upload(upload_id)
    if upload is None or upload.brand_id != deps.kit.id:
        raise _not_found()
    # Only files in the brand's uploads folder are deleted, whatever path the row holds.
    copies = [agent_copy_path(upload.image_path, transparent=alpha) for alpha in (False, True)]
    for image_path in (upload.image_path, *copies):
        path = upload_file(image_path, store.uploads_dir, upload.brand_id)
        if path is not None:
            path.unlink(missing_ok=True)
    store.delete_session_references_for_upload(upload.id)
    store.delete_upload(upload.id)
    return RedirectResponse(url="/library#uploads", status_code=303)


@router.get("/runs")
async def runs_page(request: Request) -> Response:
    store: Store = request.app.state.store
    return _page(request, "runs.html", {"rows": _run_rows(store.list_runs())})


@router.get("/runs/{run_id}")
async def run_page(request: Request, run_id: str) -> Response:
    store: Store = request.app.state.store
    run = store.get_run(run_id)
    if run is None:
        raise _not_found()
    steps = _step_rows(store.list_events(run_id), run.kind)
    return _page(
        request,
        "run.html",
        {
            "run": run,
            "kind_label": _RUN_KIND_LABELS.get(run.kind, run.kind),
            "steps": steps,
            "last_step_note": steps[-1]["note"] if steps else "",
            "children": _child_rows(store, run.id),
        },
    )


@router.get("/api/runs/{run_id}")
async def run_status(request: Request, run_id: str) -> JSONResponse:
    store: Store = request.app.state.store
    run = store.get_run(run_id)
    if run is None:
        raise _not_found()
    events = store.list_events(run_id)
    return JSONResponse(
        {
            "run": run.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in events],
            "children": _child_rows(store, run.id),
            "parent_run_id": run.parent_run_id,
        }
    )


@router.get("/posts/{post_id}")
async def post_page(request: Request, post_id: str, bar: str = "") -> Response:
    store: Store = request.app.state.store
    post = store.get_post(post_id)
    if post is None:
        raise _not_found()
    message = _QUALITY_BAR_IS_SET if bar == "set" else None
    return _page(request, "post.html", _post_context(store, post, message=message))


@router.post("/posts/{post_id}/revise")
async def revise_post(request: Request, post_id: str, comment: Annotated[str, Form()]) -> Response:
    deps: Deps = request.app.state.deps
    post = deps.store.get_post(post_id)
    if post is None:
        raise _not_found()

    stripped = comment.strip()
    if not stripped:
        return _page(
            request,
            "post.html",
            _post_context(deps.store, post, error="Write a comment first."),
            status_code=400,
        )

    run = deps.store.create_run(
        "revise", deps.kit.id, comment=stripped, parent_post_id=post.id, session_id=post.session_id
    )
    jobs: RunJobs = request.app.state.jobs
    jobs.start(run.id, run_revise(deps, run))
    return RedirectResponse(url=f"/runs/{run.id}", status_code=303)


@router.post("/posts/{post_id}/approve")
async def approve_post(request: Request, post_id: str) -> Response:
    store: Store = request.app.state.store
    try:
        store.approve_post(post_id)
    except KeyError:
        raise _not_found() from None
    return RedirectResponse(url=f"/posts/{post_id}", status_code=303)


@router.get("/library")
async def library_page(request: Request) -> Response:
    deps: Deps = request.app.state.deps
    analysis_job: AnalysisJob = request.app.state.analysis_job
    return _page(request, "library.html", _library_context(deps, analysis_job))


@router.post("/library/import")
async def import_library(request: Request, source: Annotated[str, Form()]) -> Response:
    deps: Deps = request.app.state.deps
    summary = await _import_and_analyse(request, _build_source(deps, source))
    analysis_job: AnalysisJob = request.app.state.analysis_job
    return _page(request, "library.html", _library_context(deps, analysis_job, import_summary=summary))


@router.post("/library/upload")
async def upload_references(
    request: Request, images: Annotated[list[UploadFile], File()]
) -> Response:
    """Put the designer's image files into the inbox, then import the inbox as its button does.

    Each file passes the session upload's checks, or is listed as refused in the summary.
    """
    deps: Deps = request.app.state.deps
    refused: list[str] = []
    for image in images:
        name = Path(image.filename or "").name
        # Reading one byte past the limit is enough to tell a file that is too large.
        data = await image.read(MAX_UPLOAD_BYTES + 1)
        if not name and not data:
            continue  # the empty part a browser sends when no file was chosen
        extension = upload_extension(data)
        if extension is None:
            refused.append(name or "Untitled")
            continue
        inbox = deps.store.inbox_dir
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / _inbox_name(inbox, name, extension)).write_bytes(data)

    summary = await _import_and_analyse(request, FolderSource(deps.store.inbox_dir))
    analysis_job: AnalysisJob = request.app.state.analysis_job
    return _page(
        request,
        "library.html",
        _library_context(
            deps, analysis_job, import_summary=summary, refused_text=_refused_text(refused)
        ),
    )


@router.post("/library/import-links")
async def import_links(request: Request, links: Annotated[str, Form()] = "") -> Response:
    """Fetch each pasted image link into the library, as the board's links are fetched.

    The summary also says how many lines were not web links, and when links past the
    twentieth were left out.
    """
    deps: Deps = request.app.state.deps
    lines = links.splitlines()
    summary = await _import_and_analyse(request, LinkListSource(lines))
    analysis_job: AnalysisJob = request.app.state.analysis_job
    return _page(
        request,
        "library.html",
        _library_context(
            deps, analysis_job, import_summary=summary, refused_text=_links_left_out_text(lines)
        ),
    )


@router.post("/quality-bar/upload")
async def upload_quality_bar(request: Request, image: Annotated[UploadFile, File()]) -> Response:
    """Make the designer's own image the quality bar.

    A file that fails the upload's checks, or cannot be decoded in full, is refused.
    """
    deps: Deps = request.app.state.deps
    # Reading one byte past the limit is enough to tell a file that is too large.
    data = await image.read(MAX_UPLOAD_BYTES + 1)
    label = Path(image.filename or "").name or _QUALITY_BAR_UPLOAD_LABEL
    usable = upload_extension(data) is not None
    if not usable or not await _set_quality_bar(deps.store, data, "upload", label):
        analysis_job: AnalysisJob = request.app.state.analysis_job
        return _page(
            request,
            "library.html",
            _library_context(deps, analysis_job, quality_bar_error=UPLOAD_REFUSED),
            status_code=400,
        )
    return RedirectResponse(url="/library", status_code=303)


@router.post("/quality-bar/from-post/{post_id}")
async def quality_bar_from_post(request: Request, post_id: str) -> Response:
    """Make the finished post the quality bar, and go back to the page that asked, which says
    so (`?bar=set`). An image that cannot be decoded leaves the bar as it was."""
    store: Store = request.app.state.store
    post = store.get_post(post_id)
    if post is None:
        raise _not_found()
    image = _media_file(store, post.image_path)
    is_set = await _set_quality_bar(store, image, "post", _QUALITY_BAR_POST_LABEL)
    back = _quality_bar_return(request, f"/posts/{post.id}")
    return RedirectResponse(url=_with_bar_status(back, is_set=is_set), status_code=303)


@router.post("/quality-bar/from-sample/{sample_id}")
async def quality_bar_from_sample(request: Request, sample_id: str) -> Response:
    """Make the photo the quality bar, and go back to the page that asked, which says so
    (`?bar=set`). An image that cannot be decoded leaves the bar as it was."""
    store: Store = request.app.state.store
    sample = store.get_sample(sample_id)
    if sample is None or sample.status == "deleted":
        raise _not_found()
    photo = _media_file(store, sample.image_path)
    is_set = await _set_quality_bar(store, photo, "sample", _QUALITY_BAR_SAMPLE_LABEL)
    back = _quality_bar_return(request, "/archive")
    return RedirectResponse(url=_with_bar_status(back, is_set=is_set), status_code=303)


@router.post("/quality-bar/reset")
async def reset_quality_bar(request: Request) -> Response:
    """Go back to the kit's example. The copied file stays where it is."""
    store: Store = request.app.state.store
    store.set_setting(QUALITY_BAR_SETTING, None)
    return RedirectResponse(url="/library", status_code=303)


@router.post("/api/references/{ref_id}/choice")
async def set_reference_choice(request: Request, ref_id: str, body: ChoiceBody) -> JSONResponse:
    store: Store = request.app.state.store
    try:
        store.set_choice(ref_id, body.choice)
    except KeyError:
        raise _not_found() from None
    taste = build_taste_profile(store.list_references())
    return JSONResponse(taste.model_dump(mode="json"))


@router.get("/api/library/status")
async def library_status(request: Request) -> JSONResponse:
    deps: Deps = request.app.state.deps
    analysis_job: AnalysisJob = request.app.state.analysis_job
    taste = build_taste_profile(deps.store.list_references())
    return JSONResponse(
        {"progress": analysis_job.progress().model_dump(mode="json"), "taste": taste.model_dump(mode="json")}
    )


@router.get("/media/{path:path}")
async def media(request: Request, path: str) -> FileResponse:
    store: Store = request.app.state.store
    try:
        resolved = store.media_path(path)
    except ValueError:
        raise _not_found() from None
    if not resolved.is_file():
        raise _not_found()
    return FileResponse(resolved)


@router.get("/brand/font")
async def brand_font(
    request: Request, face: str = "", style: _FontStyle = "regular"
) -> FileResponse:
    """A face's font file, so the editor's canvas sets the words in the real typeface.

    With no face named it is the kit's family, and with no style the upright file. A face
    the kit does not offer, or italics for a face without them, is not found.
    """
    deps: Deps = request.app.state.deps
    chosen = face_named(deps.kit, face)
    if chosen is None:
        raise _not_found()
    path = chosen.italic if style == "italic" else chosen.regular
    if path is None:
        raise _not_found()
    return FileResponse(path)


@router.get("/brand/logo")
async def brand_logo(request: Request, mode: Mode = "dark") -> FileResponse:
    """The logo file the renderer places on a dark or a light post, for the editor's canvas."""
    deps: Deps = request.app.state.deps
    return FileResponse(logo_for_mode(deps.kit, mode, deps.store.work_dir / "logos"))


@router.get("/brand/quality-bar")
async def brand_quality_bar(request: Request) -> FileResponse:
    """The kit's ideal example while it is the quality bar. A bar the designer chose is
    served from the data folder, under /media/, so this is not found while one is set."""
    deps: Deps = request.app.state.deps
    path, _ = quality_bar(deps.store, deps.kit)
    if path is None or _in_data_folder(deps.store, path):
        raise _not_found()
    return FileResponse(path)
