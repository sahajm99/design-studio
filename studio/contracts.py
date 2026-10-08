"""The typed objects every part of the studio passes around.

Each one is saved as JSON with the run that produced it. A change here changes
the contract between steps, so every module that reads the object is affected.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


def new_id() -> str:
    """A short unique id for runs and posts."""
    return uuid.uuid4().hex[:12]


def now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- brand kit

Mode = Literal["dark", "light"]


class BrandColour(BaseModel):
    name: str
    hex: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    use: str = ""


class ModeColours(BaseModel):
    """Which palette colours play which role on a dark or a light post."""

    background: str
    headline: str
    body: str


class FontFace(BaseModel):
    """One typeface a post may use: the kit's own, or a studio face the kit allows."""

    name: str
    regular_file: str
    italic_file: str | None = None


class Typography(BaseModel):
    family: str
    regular_file: str
    italic_file: str | None = None
    headline_weight: int = 700
    body_weight: int = 400
    headline_tracking: str = "-0.02em"
    # v3.1: more faces the kit allows beside its family, and whether the studio's own
    # open-licence faces may be offered in the editor. The kit decides; the default is no.
    fonts: list[FontFace] = Field(default_factory=list)
    studio_fonts: bool = False


class BrandLogos(BaseModel):
    lockup_file: str
    mark_file: str | None = None
    dark_background: Literal["knockout_white", "as_is"] = "as_is"


class PostSize(BaseModel):
    width: int = 1080
    height: int = 1350


# ---------------------------------------------------------- research policy (v4)

TrendAppetite = Literal["formats", "tone", "memes"]


class Occasion(BaseModel):
    """A date or a season the brand marks, as the kit writes it."""

    name: str
    when: str  # as the kit writes it: "October", "20 March"
    note: str = ""


class ResearchPolicy(BaseModel):
    """What the kit allows research to do. Defaults apply when brand.yaml has no research block."""

    enabled: bool = True
    field: str = ""  # the brand's field; empty means the scout reads the audience line instead
    location: str = ""  # used when the brief names no place
    trend_appetite: TrendAppetite = "formats"
    occasions: list[Occasion] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)  # topics research must not bring in


class BrandKit(BaseModel):
    """One brand's logo, type, colours and rules, read from its brand.yaml."""

    id: str
    root: str
    name: str
    audience: str
    feel: str
    rules: list[str] = Field(default_factory=list)
    colours: list[BrandColour]
    modes: dict[Mode, ModeColours]
    typography: Typography
    logos: BrandLogos
    post_size: PostSize = Field(default_factory=PostSize)
    inspiration_board: str | None = None
    # v2.1: the layout templates this brand allows. None means all of them.
    layouts: list[str] | None = None
    # v4: what research may do for this brand; defaults when brand.yaml has no research block.
    research: ResearchPolicy = Field(default_factory=ResearchPolicy)

    def hex(self, colour_name: str) -> str:
        for colour in self.colours:
            if colour.name == colour_name:
                return colour.hex
        raise KeyError(f"Brand kit '{self.id}' has no colour named '{colour_name}'")

    def mode_hex(self, mode: Mode) -> dict[str, str]:
        """The hex value of each role (background, headline, body) for a mode."""
        roles = self.modes[mode]
        return {
            "background": self.hex(roles.background),
            "headline": self.hex(roles.headline),
            "body": self.hex(roles.body),
        }


# ------------------------------------------------------- references and taste

Background = Literal["dark", "light", "colour"]
LayoutKind = Literal["centred_subject", "full_bleed_photo", "split", "type_only", "other"]
TextAmount = Literal["none", "few", "some", "many"]
Subject = Literal["product", "person", "place", "abstract", "other"]
ColourCount = Literal["one_or_two", "three_or_four", "five_plus"]

# The style-card fields that can be counted, in the order they are reported.
STYLE_FIELDS: tuple[str, ...] = ("background", "layout", "text_amount", "subject", "colour_count")

# How each style-card value reads in a sentence. The UI and the taste summary share these.
STYLE_LABELS: dict[str, dict[str, str]] = {
    "background": {
        "dark": "dark background",
        "light": "light background",
        "colour": "coloured background",
    },
    "layout": {
        "centred_subject": "single centred subject",
        "full_bleed_photo": "full-bleed photo",
        "split": "split layout",
        "type_only": "type only",
        "other": "other layout",
    },
    "text_amount": {
        "none": "no words",
        "few": "few words",
        "some": "some words",
        "many": "many words",
    },
    "subject": {
        "product": "product as subject",
        "person": "person as subject",
        "place": "place as subject",
        "abstract": "abstract subject",
        "other": "other subject",
    },
    "colour_count": {
        "one_or_two": "one or two colours",
        "three_or_four": "three or four colours",
        "five_plus": "five or more colours",
    },
}


class StyleCard(BaseModel):
    """A structured description of one reference, written once by the analyst."""

    background: Background
    layout: LayoutKind
    text_amount: TextAmount
    subject: Subject
    colour_count: ColourCount
    mood: list[str] = Field(default_factory=list)
    technique: str = ""


Choice = Literal["liked", "disliked", "none"]
RefStatus = Literal["pending", "analysed", "failed", "unavailable"]


class Reference(BaseModel):
    """A design collected for its style. It is never copied into a post."""

    id: str
    source: str
    source_url: str | None = None
    label: str = ""
    category: str = ""
    image_path: str | None = None  # relative to the data folder
    content_hash: str | None = None
    status: RefStatus = "pending"
    error: str | None = None
    choice: Choice = "none"
    style_card: StyleCard | None = None
    created_at: datetime = Field(default_factory=now)


class TasteProfile(BaseModel):
    """What the liked and the disliked references have in common."""

    liked_count: int = 0
    disliked_count: int = 0
    # field name -> value -> how many references have it
    liked: dict[str, dict[str, int]] = Field(default_factory=dict)
    disliked: dict[str, dict[str, int]] = Field(default_factory=dict)
    summary: str = "No likes or dislikes yet."
    liked_reference_ids: list[str] = Field(default_factory=list)

    def majority(self, field: str) -> str | None:
        """The most common value of a field among liked references, if any."""
        counts = self.liked.get(field) or {}
        if not counts:
            return None
        return max(sorted(counts), key=lambda value: counts[value])


# ------------------------------------------------------------ layouts (v2.1)

LayoutTemplate = Literal["hero", "full_bleed", "split", "corner", "caption_strip", "type_only", "custom"]
LogoPosition = Literal["top_left", "top_centre", "top_right", "bottom_left", "bottom_centre", "bottom_right"]
TextPosition = Literal["top", "bottom"]
TextAlign = Literal["left", "centre"]
PhotoSide = Literal["left", "right", "top"]
Scrim = Literal["auto", "on", "off"]

# Templates whose words can sit over the photo, so they need the contrast check.
TEXT_OVER_PHOTO: tuple[str, ...] = ("full_bleed", "caption_strip")


# ------------------------------------------------------------- the editor (v3)

BlockKind = Literal["headline", "subline", "logo", "shade", "text", "image"]
PhotoFit = Literal["cover", "contain"]


class PhotoBox(BaseModel):
    """Where the photo sits on the canvas, in percent. The default covers the whole canvas."""

    x: float = Field(default=0, ge=0, le=100)
    y: float = Field(default=0, ge=0, le=100)
    w: float = Field(default=100, gt=0, le=100)
    h: float = Field(default=100, gt=0, le=100)


class Block(BaseModel):
    """One element the designer placed on the canvas, in percent of the canvas."""

    kind: BlockKind
    x: float = Field(ge=0, le=100)  # left edge, percent of the canvas width
    y: float = Field(ge=0, le=100)  # top edge, percent of the canvas height
    w: float = Field(gt=0, le=100)  # width, percent of the canvas width
    h: float = Field(default=0, ge=0, le=100)  # height, percent; shade and image blocks only
    align: TextAlign = "left"  # text only
    size_px: int = Field(default=0, ge=0, le=400)  # text only; 0 means the default size
    colour: str = ""  # a palette colour name; text and shade only. "" means the role colour
    opacity: float = Field(default=0.7, ge=0, le=1)  # shade and image blocks
    # v3.1: text only. font names a FontFace the kit allows ("" is the kit's family);
    # weight is 100 to 900, or 0 for the role's default; italic uses the face's italic file.
    font: str = ""
    weight: int = Field(default=0, ge=0, le=900)
    italic: bool = False
    # v5: a text block carries its own words; an image block an upload's media path. On the
    # logo block, image_path overrides the kit's logo file with an upload (the Ask mode's
    # "change the logo"); the box is kept.
    text: str = Field(default="", max_length=200)  # text blocks only
    image_path: str = ""  # image blocks, or the logo block's override: uploads/<brand>/<id>.<ext>
    fit: PhotoFit = "contain"  # image blocks only
    keep_aspect: bool = True  # image blocks only: resizing keeps the image's proportions


class CustomLayout(BaseModel):
    """The editor's arrangement: blocks over a photo that covers the canvas."""

    blocks: list[Block] = Field(default_factory=list)
    photo_fit: PhotoFit = "cover"
    photo_offset_x: float = Field(default=0, ge=-50, le=50)  # percent; pans the photo under cover
    photo_offset_y: float = Field(default=0, ge=-50, le=50)
    background: str = ""  # a palette colour name for the canvas behind the photo; "" means the mode's
    # v3.1: the photo's box on the canvas. None means the whole canvas, as before.
    photo: PhotoBox | None = None


UploadSource = Literal["editor", "session"]


class Upload(BaseModel):
    """An image the designer uploaded, kept under the brand so it can be placed again."""

    id: str = Field(default_factory=new_id)
    brand_id: str
    image_path: str  # media path: uploads/<brand_id>/<id>.<ext>
    name: str = ""  # the uploaded file's name, trimmed to 80 characters, for the picker
    width: int = 0
    height: int = 0
    source: UploadSource = "editor"
    created_at: datetime = Field(default_factory=now)


class SessionReference(BaseModel):
    """A designer's picture of how one post should look, attached to a session."""

    id: str = Field(default_factory=new_id)
    session_id: str
    upload_id: str  # the Upload that holds the file
    image_path: str
    note: str = Field(default="", max_length=80)
    card: StyleCard | None = None  # the analyst's card, when it could be written
    created_at: datetime = Field(default_factory=now)


class Composition(BaseModel):
    """A template and its parameters. Rules shortlist these from where the photo's subject sits."""

    template: LayoutTemplate = "hero"
    logo_position: LogoPosition = "top_centre"
    text_position: TextPosition = "bottom"
    text_align: TextAlign = "centre"
    photo_side: PhotoSide = "top"  # split only
    scrim: Scrim = "auto"  # full_bleed and caption_strip only
    # v3: the editor's arrangement; only the "custom" template reads it.
    custom: CustomLayout | None = None


# ----------------------------------------------------------------- the post

# The template name. Kept as the old alias so every reader still works; old rows hold "hero" or "type_only".
LayoutId = LayoutTemplate


class DesignSpec(BaseModel):
    """The art director's description of one post. Code renders it exactly."""

    layout: LayoutId
    # v2.1: the template's parameters. None means the template's defaults.
    composition: Composition | None = None
    mode: Mode
    headline: str
    subline: str = ""
    # What the photo should show. Empty means no photo. Never asks for text or logos.
    photo_prompt: str = ""
    # On a revision: keep the previous version's photo.
    reuse_photo: bool = False
    caption: str = ""
    hashtags: list[str] = Field(default_factory=list)
    # Ids of the liked references this post drew on.
    reference_ids: list[str] = Field(default_factory=list)
    # What the agent assumed where the brief did not say.
    assumptions: list[str] = Field(default_factory=list)
    reason_layout: str = ""
    reason_references: str = ""
    reason_words: str = ""


class RenderReport(BaseModel):
    width: int
    height: int
    layout: LayoutId
    mode: Mode
    fits: bool
    adjustments: list[str] = Field(default_factory=list)
    # v2.1: words over a photo. The measured contrast ratio, and whether a shade was added for it.
    text_contrast: float | None = None
    scrim_added: bool = False


PostStatus = Literal["draft", "approved"]


class Post(BaseModel):
    id: str = Field(default_factory=new_id)
    root_post_id: str  # the first version's id; equals id for version 1
    version: int = 1
    run_id: str
    brand_id: str
    brief: str
    image_path: str  # relative to the data folder
    photo_path: str | None = None  # relative to the data folder
    photo_provider: str = ""
    caption: str = ""
    spec: DesignSpec
    render_report: RenderReport
    notes: list[str] = Field(default_factory=list)
    status: PostStatus = "draft"
    session_id: str | None = None  # v2: the session that made it
    sample_id: str | None = None  # v2: the chosen sample its photo came from
    auto_summary: str = ""  # v3: "Made automatically: ..." when an auto session made it
    direction_title: str = ""  # v4: the direction the post followed, if any
    created_at: datetime = Field(default_factory=now)


# --------------------------------------------------------------------- runs

# "create" is the v1 one-shot run, kept so old rows still load. v2 runs are the session stages.
# v5: "edit" is one request of the editor's Ask mode.
RunKind = Literal[
    "create", "revise", "draft", "samples", "revise_prompt", "compose", "finish", "auto", "scout",
    "edit",
]
RunStatus = Literal["running", "succeeded", "failed", "interrupted"]
StepStatus = Literal["running", "succeeded", "failed"]


class Run(BaseModel):
    id: str = Field(default_factory=new_id)
    kind: RunKind
    brand_id: str
    brief: str = ""
    comment: str = ""
    parent_post_id: str | None = None
    session_id: str | None = None
    parent_run_id: str | None = None  # v3: the auto run this run is a stage of
    status: RunStatus = "running"
    error: str | None = None
    post_id: str | None = None
    created_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None


class RunEvent(BaseModel):
    """One step of a run, as shown on the run page."""

    id: int | None = None
    run_id: str
    step: str
    status: StepStatus = "running"
    started_at: datetime = Field(default_factory=now)
    ended_at: datetime | None = None
    provider: str = ""
    attempts: int = 1
    note: str = ""
    error: str | None = None


# ------------------------------------------------------------ photo models (v6)

# Who makes a photo: a provider is one adapter, each of its models one catalogue entry.
ProviderId = Literal["cloudflare", "openai", "google", "fake"]
# How a photo's cost is known: a free allowance, the usage the provider reports, or a list price.
PriceBasis = Literal["free_allowance", "usage", "list_price"]
# The models a fresh studio offers by default, and the one it starts with: the free one.
DEFAULT_PHOTO_MODEL_ID = "@cf/black-forest-labs/flux-2-klein-4b"


def _default_photo_limits() -> dict[ProviderId, int]:
    return {"openai": 30, "google": 30}


class PhotoSettings(BaseModel):
    """The studio's choices about image models, stored as plain JSON under `photo_settings`.

    Studio-wide, not per brand. An empty `enabled_model_ids` offers every catalogue model whose
    provider has a key. Days for the limits are UTC days.
    """

    default_model_id: str = DEFAULT_PHOTO_MODEL_ID
    enabled_model_ids: list[str] = Field(default_factory=list)
    options: dict[str, dict[str, str]] = Field(default_factory=dict)  # per model id: the overrides
    daily_photo_limit: dict[ProviderId, int] = Field(default_factory=_default_photo_limits)
    daily_spend_limit_usd: float = 2.0
    auto_fallback_to_default: bool = True


# ------------------------------------------------------------ sessions (v2)

SessionStatus = Literal["needs_brief", "choosing", "drafted", "generating", "reviewing", "composing", "finished"]
PromptAuthor = Literal["agent", "designer", "agent_after_feedback"]
SampleStatus = Literal["candidate", "picked", "rejected", "deleted"]
Reaction = Literal["liked", "disliked", "none"]
CriticFlag = Literal[
    "text_in_image",
    "logo_in_image",
    "distorted_anatomy",
    "wrong_backdrop",
    "busy",
    "off_subject",
    "unrealistic",
    "wrong_materials",
]
# A sample with one of these is never the recommended pick.
HARD_FLAGS: tuple[str, ...] = ("text_in_image", "logo_in_image", "distorted_anatomy", "unrealistic")
SubjectX = Literal["left", "centre", "right"]
SubjectY = Literal["top", "middle", "bottom"]
CalmArea = Literal["top", "bottom", "left", "right"]
CommentScope = Literal["words", "photo", "both"]


class CreativeBrief(BaseModel):
    """The one idea behind a post, written by the prompt writer from the brief."""

    audience: str = ""
    idea: str = ""
    feeling: str = ""
    offer: str = ""
    tone: str = ""


class PromptDraft(BaseModel):
    """The prompt writer's answer: a concept, a detailed photo prompt and the words.

    When the brief cannot be worked with, `usable` is false and `question` says
    what is missing; the other fields are then empty.
    """

    usable: bool = True
    question: str = ""
    concept: CreativeBrief = Field(default_factory=CreativeBrief)
    photo_prompt: str = ""
    layout: LayoutId = "hero"
    mode: Mode = "dark"
    headline: str = ""
    subline: str = ""
    caption: str = ""
    hashtags: list[str] = Field(default_factory=list)
    reason_prompt: str = ""
    reason_words: str = ""
    # v2.1: what was removed, added or kept, one line each; and, on a words task, what the comment asks for.
    changes: list[str] = Field(default_factory=list)
    scope: CommentScope = "words"


class PromptVersion(BaseModel):
    """One state of the photo prompt and the words within a session."""

    id: str = Field(default_factory=new_id)
    session_id: str
    number: int
    author: PromptAuthor
    photo_prompt: str
    layout: LayoutId = "hero"
    mode: Mode = "dark"
    headline: str = ""
    subline: str = ""
    caption: str = ""
    hashtags: list[str] = Field(default_factory=list)
    reason_prompt: str = ""
    reason_words: str = ""
    # v2.1
    changes: list[str] = Field(default_factory=list)
    banned_terms_left: list[str] = Field(default_factory=list)  # terms the feedback rejected that survived
    direction_title: str = ""  # v4: the direction this version followed, if any
    created_at: datetime = Field(default_factory=now)


class SampleReview(BaseModel):
    """The critic's view of one sample. Scores run from 1 to 5."""

    on_brief: int = Field(ge=1, le=5)
    brand_fit: int = Field(ge=1, le=5)
    craft: int = Field(ge=1, le=5)
    flags: list[CriticFlag] = Field(default_factory=list)
    verdict: str = ""
    suggested_change: str = ""
    # v2.1: where the subject sits and which areas are plain enough for words.
    subject_x: SubjectX = "centre"
    subject_y: SubjectY = "middle"
    calm_areas: list[CalmArea] = Field(default_factory=list)

    @property
    def overall(self) -> float:
        return round((self.on_brief + self.brand_fit + self.craft) / 3, 1)

    @property
    def has_hard_flag(self) -> bool:
        return any(flag in HARD_FLAGS for flag in self.flags)


class RoundRanking(BaseModel):
    """The critic's answer when it sees every sample of a round at once."""

    order: list[int] = Field(default_factory=list)  # sample indexes, best first
    reasons: list[str] = Field(default_factory=list)  # one line per entry of order
    note: str = ""


class RoundReview(BaseModel):
    """A round's ranking, as stored."""

    id: str = Field(default_factory=new_id)
    session_id: str
    round: int
    order: list[int] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    note: str = ""
    created_at: datetime = Field(default_factory=now)


class LayoutCandidate(BaseModel):
    """The chosen photo rendered in one composition, for the designer to pick from."""

    id: str = Field(default_factory=new_id)
    session_id: str
    sample_id: str
    index: int
    composition: Composition
    image_path: str  # relative to the data folder
    render_report: RenderReport
    prompt_version_id: str = ""
    created_at: datetime = Field(default_factory=now)


class Sample(BaseModel):
    """One generated photo in a round, with the critic's review and the designer's reaction."""

    id: str = Field(default_factory=new_id)
    session_id: str
    prompt_version_id: str
    round: int
    index: int
    image_path: str | None = None  # relative to the data folder; None when generation failed
    provider: str = ""
    # v6: the catalogue model asked for and what its photo cost. Old samples have none, and
    # cost_usd is None when the cost is not known.
    model_id: str = ""
    cost_usd: float | None = None
    cost_basis: PriceBasis | None = None
    seed: int | None = None
    error: str | None = None  # why generation failed
    review: SampleReview | None = None
    review_error: str | None = None  # why the critic could not score it
    recommended: bool = False
    rank: int | None = None  # v2.1: 1 is best within the round
    rank_reason: str = ""
    reaction: Reaction = "none"
    comment: str = ""
    status: SampleStatus = "candidate"
    used_in_post_id: str | None = None
    created_at: datetime = Field(default_factory=now)
    deleted_at: datetime | None = None


# ---------------------------------------------------------------- auto mode (v3)

SessionMode = Literal["manual", "auto"]
FeedbackAuthor = Literal["designer", "critic"]
StoppedBy = Literal["score", "rounds", "budget", "designer", "brief", "error"]


class AutoSettings(BaseModel):
    """The limits code enforces on an auto session."""

    max_rounds: int = Field(default=3, ge=1, le=5)
    photo_budget: int = Field(default=6, ge=1, le=12)  # photos for the whole session
    stop_score: float = Field(default=4.2, ge=1, le=5)  # the top sample's overall score that ends the loop


class RoundJudgement(BaseModel):
    """The critic's answer in the judge step: compose now, or change the prompt."""

    good_enough: bool
    reason: str = ""
    changes: list[str] = Field(default_factory=list)  # what the reviser should change, one line each


class FinalReview(BaseModel):
    """The critic's answer in the final check of a composed post."""

    ship: bool
    score: int = Field(ge=1, le=5)
    biggest_flaw: str = ""
    fix_hint: str = ""
    # v3.1: the final check compares every composed layout at once.
    pick: int = Field(default=1, ge=1)  # 1-based index of the post to ship, among those shown
    reasons: list[str] = Field(default_factory=list)  # one line per post shown, in order


class AutoState(BaseModel):
    """Where an auto session got to, one decision line at a time."""

    rounds_done: int = 0
    photos_used: int = 0
    decisions: list[str] = Field(default_factory=list)
    stopped_by: StoppedBy | None = None
    final_review: FinalReview | None = None
    picked_layout: str = ""  # v3.1: describe() of the composition the final check picked


# ------------------------------------------------------------- the quality bar (v3.1)

QualityBarSource = Literal["kit", "post", "sample", "upload"]


class QualityBar(BaseModel):
    """The image every judgement is held to, when the designer chose one instead of the kit's."""

    source: QualityBarSource
    image_path: str  # relative to the data folder; the kit's own file is never copied here
    label: str = ""  # "a post", "a photo", or the upload's name
    set_at: datetime = Field(default_factory=now)


class RoundFeedback(BaseModel):
    """What the designer said about a round as a whole."""

    id: str = Field(default_factory=new_id)
    session_id: str
    round: int
    text: str
    author: FeedbackAuthor = "designer"  # v3: the critic writes the feedback in auto mode
    created_at: datetime = Field(default_factory=now)


# ------------------------------------------------------ research and directions (v4)

ResearchStatus = Literal["grounded", "ungrounded", "demo"]
DirectionFormat = Literal["object", "place", "people", "process", "detail", "statement"]
# object: a product or thing on its own; place: a space or setting; people: a person or a team;
# process: hands or tools at work; detail: a close-up; statement: words only, no photo.


class SearchQueries(BaseModel):
    """The scout's first answer: the web searches worth making."""

    queries: list[str] = Field(default_factory=list)


class SearchResult(BaseModel):
    """One result a search provider returns."""

    title: str
    url: str
    domain: str = ""
    snippet: str = ""


class ResearchSource(BaseModel):
    """A numbered source the research cites, shown as a link."""

    number: int
    title: str
    url: str
    domain: str = ""


class ScoutReport(BaseModel):
    """The scout's second answer: the research prose, citing the numbered results."""

    text: str
    source_numbers: list[int] = Field(default_factory=list)


class ResearchReport(BaseModel):
    """What one scout run found, saved on its session and shown on the session page."""

    status: ResearchStatus
    text: str = ""  # the scout's report, as returned
    queries: list[str] = Field(default_factory=list)
    sources: list[ResearchSource] = Field(default_factory=list)
    model: str = ""
    provider: str = ""  # the search provider's name
    problem: str = ""  # why the research is not grounded
    created_at: datetime = Field(default_factory=now)


class Direction(BaseModel):
    """One of the three directions a post could take, written from the research."""

    number: int = Field(ge=1, le=3)
    title: str  # at most six words
    format: DirectionFormat
    angle: str  # the idea, one sentence
    subject: str
    framing: str
    mood: str
    layout: LayoutTemplate  # from allowed_layouts; never custom
    headline_idea: str  # at most eight words
    facts: list[str] = Field(default_factory=list)  # "fact (source n)", about the subject only
    why: str = ""  # one sentence, tied to the research
    source_numbers: list[int] = Field(default_factory=list)
    needs_real_photo: bool = False
    edited: bool = False  # set when the designer changed it before use


class DirectionSet(BaseModel):
    """The direction writer's answer: three directions and a recommendation, or a question."""

    usable: bool = True
    question: str = ""
    directions: list[Direction] = Field(default_factory=list)
    recommended: int = 1
    recommended_reason: str = ""


class StudioSession(BaseModel):
    """One post's journey from brief to finished post."""

    id: str = Field(default_factory=new_id)
    brand_id: str
    brief: str
    sample_count: int = 3
    status: SessionStatus = "needs_brief"
    question: str = ""  # the prompt writer's question when the brief was unusable
    concept: CreativeBrief | None = None
    current_prompt_version_id: str | None = None
    rounds: int = 0
    picked_sample_id: str | None = None
    picked_candidate_id: str | None = None  # v2.1: the chosen layout
    source_sample_id: str | None = None  # v2.1: set when the session started from an archived photo
    post_id: str | None = None
    active_run_id: str | None = None  # the run in flight, if any
    # v3: auto mode. The mode is "auto" only while the auto run is in flight.
    mode: SessionMode = "manual"
    auto_settings: AutoSettings | None = None
    auto_state: AutoState | None = None
    # v4: research before the draft. Off by default in manual mode; the auto route turns it on.
    research_on: bool = False
    research: ResearchReport | None = None
    directions: list[Direction] = Field(default_factory=list)
    recommended_direction: int | None = None
    recommended_reason: str = ""
    chosen_direction: Direction | None = None  # a copy, with the designer's edits
    # v6: the catalogue model this session's rounds use; empty means the studio's default.
    photo_model_id: str = ""
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


# ------------------------------------------------------ the editor's Ask mode (v5)

EditOp = Literal[
    "move", "resize", "set_text", "set_style", "replace_image", "add_text", "add_image",
    "add_shade", "delete", "reorder", "set_photo", "set_background",
]


class EditorEdit(BaseModel):
    """One change to the layout, as the editor agent names it. A field an op does not use
    stays None. `block` is the block's index in the layout's list, as the context numbers
    them."""

    op: EditOp
    block: int | None = None
    x: float | None = None
    y: float | None = None
    w: float | None = None
    h: float | None = None
    text: str | None = None
    font: str | None = None
    weight: int | None = None
    italic: bool | None = None
    size_px: int | None = None
    align: TextAlign | None = None
    colour: str | None = None
    opacity: float | None = None
    upload_id: str | None = None
    fit: PhotoFit | None = None
    direction: Literal["forward", "back"] | None = None
    offset_x: float | None = None
    offset_y: float | None = None
    background: str | None = None


class EditorAnswer(BaseModel):
    """The editor agent's answer: the edits to apply and one sentence on them, or a question."""

    usable: bool = True
    question: str = ""
    edits: list[EditorEdit] = Field(default_factory=list)
    summary: str = ""
