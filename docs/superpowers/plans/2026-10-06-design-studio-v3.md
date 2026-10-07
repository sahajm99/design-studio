# Design Studio v3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Two features at the two ends of control: auto mode, a bounded loop in which the critic judges each round and the studio hands over a finished post with its trace; and the editor, a canvas where the designer places the words, the logo and a shade freely over any photo, with the kit still deciding font, colours and logo.

**Architecture:** Unchanged from v2.1. Auto mode is one more ADK `Workflow` (`studio/workflows/auto.py`) whose function steps call the existing stage functions as child runs linked by `parent_run_id`; the judge and the final checker are two more `LlmAgent`s with output schemas. The editor produces a `custom` composition that the normal renderer draws from a new `custom.html` template, so the fit report, the contrast measurement and the candidate flow all still apply.

**Tech Stack:** Python 3.12, google-adk 2.11, FastAPI + Jinja2, SQLite, Playwright Chromium, Pillow, vanilla JavaScript.

**Spec:** `docs/superpowers/specs/2026-10-06-design-studio-v3-design.md`. The v2.1, v2 and v1 specs remain the reference for what v3 does not change.

## How this plan departs from the usual shape

By Sahaj's instruction, as in v2 and v2.1: no new tests (change or delete only tests that v3 breaks; the suite must pass when you report); no commits; the controller wrote the contracts (already in `studio/contracts.py`); tasks run in waves on disjoint files; one review of the whole diff at the end, then one fix round; Sahaj tests by hand once the app runs. Sahaj's goal for this cycle: both features working locally without breaking what exists, built fast.

## Global Constraints

- Everything runs in the container; the host has no Python. Suite:
  `docker compose --project-directory "C:/Users/sahaj/OneDrive/Desktop/Experiments/projects/active/design-studio" run --rm studio pytest -q`
  While you work, run only the test files that cover what you change (for example `pytest -q tests/test_render.py`); run the whole suite once before you report.
- A copy of the app runs on port 8000 with real keys and the live database. Leave it alone. Never start a copy that shares its `.env` or database (`docker compose run` and `docker compose up` do); to look at a page, use an isolated `docker run` of the image `design-studio:dev` on another port (8010 or above) with a temporary data folder, `--env-file` omitted, the code mounted read-only, and stop it after:
  `docker run --rm -d --name studio-check-$RANDOM -p 8010:8000 -e STUDIO_DATA_DIR=/app/data -e STUDIO_DB_PATH=/app/data/studio.db -e STUDIO_BRANDS_DIR=/app/brands -v "<repo>/studio:/app/studio:ro" -v "<repo>/brands:/app/brands:ro" -v "<temp folder>:/app/data" --ipc=host design-studio:dev`
  (In Git Bash on Windows, prefix with `MSYS_NO_PATHCONV=1`.)
- No commits, no git command that changes history, the index or the working tree.
- Read-only: `studio/contracts.py`, `studio/config.py`, `studio/brand.py`, `studio/library/`, `studio/photos/`, `tests/conftest.py`, `requirements*.txt`, `pytest.ini`, `Dockerfile`, `docker-compose.yml`, `brands/`, `docs/`.
- Touch only the files your task lists. Nothing brand-specific inside `studio/`: the layout files must hold no typeface name, brand name or hex colour (a test checks). No secrets in code, logs or messages. Person-facing text is plain language in sentence case, exactly as the plan writes it.
- ADK 2.x `Workflow` only, with the v2 patterns in `studio/workflows/`: function steps, `node(fn, rerun_on_resume=True)` for a step that calls `ctx.run_node`, `StepRecorder` steps, `settle` as every run's way out, `update_session` for every session change.
- `hero` and `type_only` must render exactly as today: the existing render tests check pixels. `base.css` is not changed.
- Form field names, route paths, JSON keys and the texts below are used verbatim; later tasks and the controller's run-through depend on them.

## Review Focus

Checked by the controller's run-through and the whole-diff review: (1) an auto session with a real brief ends in a post with no click, and each round's decision reads as one line; (2) the loop never exceeds the rounds or the photo budget, and stops early when the top sample reaches the stop score with no hard flag; (3) Stop during an auto run leaves a manual session with everything made so far and no run in flight; (4) the Runs page shows the auto run's child runs and each child links back; (5) the editor's guardrails hold in the renderer: a block outside the margin is clamped, a logo narrower than 14% is widened, a colour outside the palette falls back; (6) an upload that is not a PNG or JPEG, or is over 15 MB, is refused with the one message; (7) a custom candidate can be finished and reopened with the same arrangement.

## Contracts already in `studio/contracts.py` (read them first)

```python
SessionMode = Literal["manual", "auto"]
FeedbackAuthor = Literal["designer", "critic"]
StoppedBy = Literal["score", "rounds", "budget", "designer", "brief", "error"]

class AutoSettings(BaseModel):   max_rounds: int = 3 (1..5); photo_budget: int = 6 (1..12); stop_score: float = 4.2 (1..5)
class RoundJudgement(BaseModel): good_enough: bool; reason: str = ""; changes: list[str] = []
class FinalReview(BaseModel):    ship: bool; score: int (1..5); biggest_flaw: str = ""; fix_hint: str = ""
class AutoState(BaseModel):      rounds_done: int = 0; photos_used: int = 0; decisions: list[str] = []; stopped_by: StoppedBy | None = None; final_review: FinalReview | None = None

StudioSession: + mode: SessionMode = "manual"; auto_settings: AutoSettings | None = None; auto_state: AutoState | None = None
Run:           + parent_run_id: str | None = None;   RunKind gains "auto"
RoundFeedback: + author: FeedbackAuthor = "designer"
Post:          + auto_summary: str = ""

BlockKind = Literal["headline", "subline", "logo", "shade"]
PhotoFit = Literal["cover", "contain"]
class Block(BaseModel):        kind; x: float (0..100); y: float (0..100); w: float (0<w<=100); h: float = 0 (shade only); align: TextAlign = "left"; size_px: int = 0 (0 = default); colour: str = "" (a palette colour name); opacity: float = 0.7 (shade only)
class CustomLayout(BaseModel): blocks: list[Block] = []; photo_fit: PhotoFit = "cover"; photo_offset_x: float = 0 (-50..50); photo_offset_y: float = 0; background: str = "" (a palette colour name)
LayoutTemplate gains "custom"; Composition gains custom: CustomLayout | None = None
```

## File structure and ownership

```
studio/store.py, studio/models.py, studio/workflows/agents.py          Task 2
studio/render/ (custom.py new, layouts/custom.html new, renderer.py, compose.py, __init__.py)   Task 3
studio/workflows/ (auto.py new, session.py, layouts.py, __init__.py)   Task 4
studio/web/ (routes.py, templates studio/session/post/run/runs, static app.js/app.css)   Task 5
studio/web/ (routes.py, templates editor.html new/session/archive, static editor.js new/app.css)   Task 6
no source files; screenshots and a report                               Task 7
```

Waves: Tasks 2 and 3 together; then Task 4; then Task 5; then Task 6; then Task 7.

---

### Task 2: Store additions, the stand-ins, and the judge and final-check agents

**Files**
- Modify: `studio/store.py`, `studio/models.py`, `studio/workflows/agents.py`
- Read first: `studio/contracts.py` (the v3 section at the end and the session section), the existing store methods, the existing fake roles in `models.py`, every prompt in `agents.py`, the spec's section 2

**Interfaces this task produces**

```python
# studio/store.py
uploads_dir: Path                                   # data_dir / "uploads", created by init() like the other folders
def create_run(self, kind, brand_id, *, brief="", comment="", parent_post_id=None, session_id=None,
               parent_run_id: str | None = None) -> Run      # the one new keyword, stored on the Run
def list_runs_for_parent(self, run_id: str) -> list[Run]     # runs whose parent_run_id is run_id, OLDEST first (json_extract, like list_runs_for_session)

# studio/models.py
ROLE_JUDGE = "judge"
ROLE_FINAL_CHECK = "final_check"

# studio/workflows/agents.py
JUDGE_PROMPT: str
FINAL_CHECK_PROMPT: str
def build_judge(llm: BaseLlm) -> LlmAgent          # name "judge", output_schema RoundJudgement, output_key "round_judgement"
def build_final_checker(llm: BaseLlm) -> LlmAgent  # name "final_check", output_schema FinalReview, output_key "final_review"
```

**Stand-ins in `FakeLlm`** (deterministic, always valid for the schema; read the context with `read_context` as the other roles do)

- `ROLE_JUDGE`: the context carries `top_overall` (a number or null), `top_has_hard_flag` (bool) and `stop_score` (number). Answer a `RoundJudgement` with `good_enough` = `top_overall` is not null and `top_has_hard_flag` is false and `top_overall >= stop_score`; `reason` = `Demo judgement: the top sample scores {top_overall} against {stop_score}.` (or `Demo judgement: no sample was scored.` when `top_overall` is null); `changes` = `[]` when good enough, else `["Demo change: softer, more even light.", "Demo change: a plainer backdrop with more empty space."]`.
- `ROLE_FINAL_CHECK`: answer `FinalReview(ship=True, score=4, biggest_flaw="Demo mode: no model looked at the post.", fix_hint="Demo mode: nothing to fix.")`.

**Agents.** Both are built like `build_critic`: the instruction is a function of `ReadonlyContext` that returns the prompt, a blank line, then `wrap_context(ROLE, ctx.state["<key>"])`. The judge reads `ctx.state["judge_context"]`; the final checker reads `ctx.state["final_check_context"]`. Task 4 fills those keys with:

```python
judge_context = {"round": int, "stop_score": float, "top_index": int | None, "top_overall": float | None,
                 "top_has_hard_flag": bool,
                 "reviews": [{"index", "rank", "overall", "on_brief", "brand_fit", "craft", "flags", "verdict", "suggested_change"}],  # best first
                 "ranking_note": str, "prompt": str, "concept": dict | None, "brand": {"name", "feel"}}
final_check_context = {"concept": dict | None, "brand": {"name", "feel"},
                       "words": {"headline", "subline", "caption"}, "layout": str}
```

`JUDGE_PROMPT`, joined with newlines like the others:

```
You decide whether a round of generated photographs for a social post is good enough to compose into the finished post, for the brand named in the context below.
The context holds the round's samples as the critic scored and ranked them, best first, the prompt they were made from, the post's concept, the brand's feel, and stop_score, the overall score the top sample must reach.
Say good_enough true only when the top sample has no flag, its overall score reaches stop_score, and a designer would not be embarrassed to post it as this brand.
When it is not good enough, write changes: two to four concrete changes the photo prompt should make, one line each, drawn from the samples' weaknesses, the critic's suggested changes and the flags. Say what to show instead of what to avoid wherever you can.
Leave changes empty when it is good enough.
Write reason as one sentence.
```

`FINAL_CHECK_PROMPT`:

```
You give the final check on one finished social post for the brand named in the context below.
The post arrives first in the message, after the line "The finished post". When the brand has an ideal example, it follows after the line "The brand's quality bar.": hold the post to that bar, and never judge the example itself.
The context holds the post's concept, its words, its layout and the brand's feel.
Say ship true only when the post could be published as it is: the words read clearly over the picture, the logo is clear, nothing in the picture is wrong, and it fits the brand's feel.
Score it from 1 to 5. 5 means as good as the quality bar, and should be rare. 4 means strong, with one small weakness. 3 means fine but ordinary. 2 means a clear problem. 1 means unusable.
Write biggest_flaw as one sentence naming the single biggest weakness, even for a strong post.
Write fix_hint as one line a designer could act on.
```

**Prompt writer** (`PROMPT_WRITER_PROMPT`): add two lines, placed after the line that starts "Describe what should be in the frame":

```
When layout_hint is full_bleed or caption_strip, the words will sit over the photo at text_position: keep that third of the frame plain, backdrop only, with the subject in the other two thirds, and say so in the prompt.
When round_comment_author is critic, round_comment lists the changes the brand's own critic asked for after judging the latest round: apply every line as if the designer had written it.
```

**Keep passing:** `tests/test_store.py`, `tests/test_models.py`, `tests/test_agents.py`, then the whole suite.

---

### Task 3: The renderer's custom layout

**Files**
- Create: `studio/render/custom.py`, `studio/render/layouts/custom.html`
- Modify: `studio/render/renderer.py`, `studio/render/compose.py`, `studio/render/__init__.py`
- Read first: `studio/contracts.py` (the v3 section: `Block`, `CustomLayout`, `Composition.custom`, `LayoutTemplate`), `studio/render/renderer.py` in full (the fit script, the contrast check, `_brand_style`), `studio/render/compose.py`, `layouts/base.html`, `base.css`, `full_bleed.html`, `tests/test_render.py` (what must keep passing, including the no-brand-values check over every layout file), the spec's section 3

**Interfaces this task produces**

```python
# studio/render/custom.py  (pure functions; nothing here names a brand)
MARGIN_PCT = 4.0            # every block stays inside this margin, percent of the canvas
MIN_LOGO_PCT = 14.0         # the logo is never narrower than this, percent of the canvas width
HEADLINE_PX = 64            # a text block's size when size_px is 0
SUBLINE_PX = 26
DEFAULT_LOGO_PCT = 24.0

def default_layout() -> CustomLayout
    # logo x=8 y=6 w=24; headline x=8 y=66 w=84 align left; subline x=8 y=80 w=84 align left; no shade; cover; no background
def blocks_for_template(composition: Composition, *, scrim_added: bool = False) -> CustomLayout
    # the starting arrangement for the editor when it opens on a template candidate (table below)
def apply_guardrails(layout: CustomLayout, kit: BrandKit, mode: Mode) -> tuple[CustomLayout, list[str]]
    # a copy with every rule applied, and one line per change it had to make (texts below)
def resolve_colour(name: str, kit: BrandKit, mode: Mode, role: str) -> str
    # kit.hex(name) when name is one of kit.colours' names; otherwise kit.mode_hex(mode)[role]; role is "headline", "body" or "background"

# studio/render/compose.py
NEEDS_PHOTO gains "custom"          # a custom layout is always drawn on a photo
describe(composition) -> "Custom layout" for template "custom"
_key(composition) -> ("custom", composition.custom.model_dump_json() if composition.custom else "")
# TEMPLATES, _DEFAULTS, allowed_templates and shortlist do NOT learn about "custom": it is never offered by the rules.
# composition_for(spec): unchanged; a custom spec always carries its composition, so it is returned as it is.

# studio/render/renderer.py
START_PX["custom"] = HEADLINE_PX
CUSTOM_DOES_NOT_FIT = "Some words do not fit their box. Open the editor and make room."
build_html(...)  # layout "custom" renders layouts/custom.html; raises ValueError("The custom layout needs its blocks.") when spec.composition is None or spec.composition.custom is None
Renderer.render(...)  # no headline stepping for custom; the contrast measured but never auto-shaded; adjustments per the rules below
```

Export from `studio/render/__init__.py`: `apply_guardrails`, `blocks_for_template`, `default_layout`, `CUSTOM_DOES_NOT_FIT` (keep the existing exports).

**Guardrails** (`apply_guardrails`, in this order, each producing a line only when it changed something):
1. A text or logo block with `x < 4` or `y < 4` or `x + w > 96` is moved or shrunk until it is inside: first move (`x = max(x, 4)`, `y = max(y, 4)`), then shrink `w` so `x + w <= 96`. A shade block is also clamped so `y + h <= 96`. Line: `The {kind} was moved inside the margin.` (kind: headline, subline, logo, shade).
2. A logo block with `w < 14`: `w = 14`, then rule 1 again if it now crosses the margin. Line: `The logo was widened to 14% of the canvas.`
3. A `colour` or `background` naming no palette colour is set to `""` (which means the role colour). Line: `The colour "{name}" is not in the palette, so the brand's colour was used.`
4. `size_px` outside 12..400 is clamped to that range, `opacity` to 0..1. No line.

**The template** (`layouts/custom.html`, extends `base.html`): `body { padding: 0 }`. The photo covers the whole canvas: `.photo { position: absolute; inset: 0 }` with `<img>` at `width: 100%; height: 100%; object-fit: {{ photo_fit }}; object-position: {{ 50 + photo_offset_x }}% {{ 50 + photo_offset_y }}%`. Every block is an absolutely positioned element with its geometry as inline percentages (`left`, `top`, `width`, and `height` for a shade). Drawing order: the photo, then every shade (`<div class="shade">` with `background: {{ hex }}; opacity: {{ opacity }}`), then the text blocks (`<h1 class="headline">` and `<p class="subline">`, with `font-size: {{ px }}px; color: {{ hex }}; text-align: {{ align }}; margin: 0; max-width: none`), then the logo (`<img class="logo">`, width only, so it keeps its proportions). A `background` colour becomes an inline `background` on `<body>` (so `contain` fit and the margin show it); otherwise the mode's background (`--bg`) stays. The template file holds no hex value, no typeface name and no brand name: every value arrives from `build_html` through the template context. Give `build_html` the blocks as a list of plain dicts (`kind, left, top, width, height, align, px, hex, opacity`) so the template does no maths.

**The fit script for custom.** `build_html` passes `CUSTOM_FIT_SCRIPT` instead of `FIT_SCRIPT` when the layout is custom. It defines the same `window.studioFit()` shape (`fits`, `overflow`, `words`, `shown`, `photo`) but judges each text block on its own: `overflow` gets `"{kind} box"` when the block's words (the range rectangle) spill past the block's left or right edge by more than 1px, and `"{kind} margin"` when the block's bottom or right edge passes 96% of the canvas. `words` is the union of the text blocks' word rectangles, `shown` lists the kinds shown, `photo` is the `img` rectangle. The renderer turns `"headline box"` into `The headline overflows its box.` and `"subline margin"` into `The subline runs past the margin.` (same pattern for each kind) in `adjustments`, and `fits` is false when any line was added.

**The renderer** for custom: `start_px = min_px = HEADLINE_PX`, so `_fit_text` never steps (each block carries its own size inline; `RESIZE_SCRIPT` is harmless). Apply the guardrails once at the start of `render()` and `build_html()` (the same function, so what is reported is what is drawn), and add their lines to `adjustments`. Then the contrast: when `photo_fit == "cover"` and the capture has `words` and `photo`, measure with `_words_contrast` using a colours dict of `{"headline": <the headline block's resolved hex>, "body": <the subline block's resolved hex or the headline's>, "background": <the canvas background hex>}` and the photo's object position: extend `_luminance_under` with `position: tuple[float, float] = (0.5, 0.5)` (the fractions of the slack the image is offset by: `left = photo.left + (photo.width - image.width * scale) * position[0]`, same for top); the existing callers keep the centred default. When the ratio is under `MIN_CONTRAST`, add `The words may be hard to read over the photo (contrast {ratio}). Add a shade.` with the ratio to one decimal, rounded down. Never add a shade yourself: `scrim_added` stays false; `text_contrast` holds the ratio. With `contain` fit, skip the measurement (`text_contrast` None, no line).

**`blocks_for_template`** (percent of the canvas; the designer adjusts from here): the logo's x is 8 (left), 38 (centre) or 68 (right) by the horizontal part of `logo_position`, and y is 6 (top) or 86 (bottom); w = 24. Text width 84 at x 8 unless stated.

| Template | Blocks |
|---|---|
| `hero` | logo top centre; headline y=70 centre; subline y=82 centre |
| `full_bleed` | logo at `logo_position`; words at `text_position`: bottom → headline y=66, subline y=80; top → headline y=20 when the logo is at the top else y=8, subline 14 lower; align `text_align`; when `scrim_added`: a shade x=0 w=100 covering the words' half (bottom: y=55 h=45; top: y=0 h=45), colour "" and opacity 0.7 |
| `split` | a shade with opacity 1.0 and colour "" as the panel: `photo_side` top → y=58 h=42 full width, logo x=8 y=62, headline x=8 y=72 w=84, subline y=86; left → the panel at x=55 w=45 full height, logo x=59 y=6 w=24, headline x=59 y=40 w=37, subline y=54; right → the panel at x=0 w=45, logo x=4 y=6, headline x=4 y=40 w=37, subline y=54; align left |
| `corner` | two panel shades with opacity 1.0: the top band y=0 h=44 full width, and the left column x=0 w=36 full height; headline x=8 y=9 w=62 align left; subline y=24; logo bottom left x=6 y=88 |
| `caption_strip` | the band shade y=76 h=24 full width opacity 1.0; logo at the band's side (`logo_position` left → x=8, right → x=68) y=82; headline on the other side (x=36 or x=8) y=80 w=56 size 40 align left; no subline |
| `type_only` or anything else | `default_layout()` |

**Keep passing:** `tests/test_render.py` in full (hero and type_only pixel-identical; every layout file free of brand values), then the whole suite.

---

### Task 4: The auto run and the stage changes

**Files**
- Create: `studio/workflows/auto.py`
- Modify: `studio/workflows/session.py`, `studio/workflows/layouts.py`, `studio/workflows/__init__.py`
- Read first: `studio/contracts.py` (the v3 section), `studio/workflows/agents.py` and `studio/models.py` (Task 2: `build_judge`, `build_final_checker`, the context keys), `studio/store.py` (Task 2: `create_run(parent_run_id=)`, `list_runs_for_parent`), `studio/render/__init__.py` (Task 3: `CUSTOM_DOES_NOT_FIT`), every file under `studio/workflows/`, the spec's section 2

**Interfaces this task produces** (exported from `studio/workflows/__init__.py`; the v2.1 ones stay)

```python
run_auto(deps, run, session) -> Post | None          # the whole auto session; None when it stopped at the brief, failed or was stopped
run_samples(deps, run, session, count: int | None = None) -> list[Sample]   # count overrides session.sample_count for this round (still clamped to settings.max_samples)
pick_source_photo(store, session, source: Sample, version_id: str | None) -> Sample
    # the public form of session.py's _pick_source_photo: copy an archived photo into the session as its round-0 pick (same file), once; prompt_version_id = version_id or ""; sets picked_sample_id and status drafted. Task 6 calls it.
```

**Stage changes.**
- `run_samples` takes `count`; `_make_samples` uses `count if count is not None else session.sample_count`, clamped as today.
- `_feedback` (the reviser's context) adds `round_comment_author`: the `author` of the latest round's newest feedback (`"designer"` when there is none), and `layout_hint`: the current version's layout, and `text_position`: `"bottom"`. The draft context adds `layout_hint: None` and `text_position: "bottom"`.
- `layouts.py` `save_post`: when the candidate's template is `custom`, the note for a post that does not fit is `CUSTOM_DOES_NOT_FIT` (from `studio.render`) instead of `TEXT_DOES_NOT_FIT`.

**The auto run** (`studio/workflows/auto.py`). One `Workflow(name="auto")` with the edge `("START", plan, draft, node(round_loop, name="round_loop", rerun_on_resume=True), compose, node(final_check, name="final_check", rerun_on_resume=True), finish, report)`. Each step is a `recorder.step(...)` like every other run. Steps pass through when an earlier step ended the run (`auto_done` in state): they record the note `Skipped.` and return. Every child run is created with `parent_run_id=run.id` and `session_id=session.id`, and awaited in place: `child = store.create_run("samples", kit.id, brief=session.brief, session_id=session.id, parent_run_id=run.id)` then `await run_samples(deps, child, latest, count=...)`. After every child run, read it back: a child whose status is `failed` fails the auto run with the child's error (`raise ValueError(child.error)`); the session's `active_run_id` stays the auto run's id throughout (a child's `settle` only clears its own id).

| Step | Does |
|---|---|
| `plan` | `settings = session.auto_settings or AutoSettings()`; `update_session(store, session, mode="auto", auto_settings=settings, auto_state=AutoState())`; note `Up to {max_rounds} rounds and {photo_budget} photos, stop at {stop_score}.` |
| `draft` | child `draft` run. When the session comes back `needs_brief`: decision `Stopped: the brief needs work.`, `stopped_by="brief"`, `auto_done`, note = the question. Otherwise note `Version {n} drafted.` |
| `round_loop` | the loop below |
| `compose` | child `compose` run with the best sample (`run_compose(deps, child, latest, best)`); no candidates → `raise ValueError("No layout could be rendered.")`; state holds the candidate ids in order |
| `final_check` | the final check below |
| `finish` | child `finish` run with the chosen candidate; then `post.auto_summary = "Made automatically: {rounds_done} rounds, {photos_used} photos, final check {score} of 5."` (or `..., final check skipped.`), saved; state `post_id` |
| `report` | note = the same summary; `auto_state.stopped_by` stays; `update_session(..., mode="manual")` |

**The loop** (`round_loop`), with `settings` and the latest session re-read from the store at the top of every pass:
1. `round_number = state.rounds_done + 1`; `count = max(1, min(latest.sample_count, settings.photo_budget - state.photos_used))`; step note `Round {round_number} of {max_rounds}: making {count} photos…` (via `store.update_step_note`).
2. Child `samples` run with `count`. `state.rounds_done += 1`; `state.photos_used += ` the number of samples with a photo.
3. `top` = the round's recommended sample; else the sample with rank 1; else the highest `review.overall`; else the first with a photo. `top_overall = top.review.overall` when reviewed, else None; `hard = top.review.has_hard_flag` when reviewed, else False.
4. The judge, when `top_overall` is not None: `ctx.state["judge_context"] = {...}` (keys in Task 2's list; `reviews` best first by rank then overall), then `ctx.run_node(judge, node_input=text_message("Judge this round."), use_sub_branch=True)` → `RoundJudgement`. One failure (any exception or a None answer): try once more; a second failure → `judgement = None`. No reviewed sample → `judgement = None` without a call.
5. Decide, in this order, writing ONE line to `state.decisions` (and `update_session(..., auto_state=state)` at once, so the page shows it live). `x` is `top_overall` with one decimal, `y` is `stop_score`:
   - `judgement is None` → `Round {n}: the critic could not judge this round, so it was composed as it is.`; `stopped_by="error"`; leave the loop.
   - `judgement.good_enough and not hard and top_overall >= y` → `Round {n}: top {x} of 5, good enough, composing.`; `stopped_by="score"`; leave.
   - `state.rounds_done >= max_rounds` → `Round {n}: top {x} of 5, rounds limit reached, composing.`; `stopped_by="rounds"`; leave.
   - `state.photos_used >= photo_budget` → `Round {n}: top {x} of 5, photo allowance used up, composing.`; `stopped_by="budget"`; leave.
   - otherwise continue: `changes = judgement.changes` or, when empty, `[top.review.suggested_change]`; the line is `Round {n}: top {x} under {y}, revising: {changes joined by "; "}.` when `top_overall < y`, or `Round {n}: the critic says good enough, but the top sample has the {flag} flag; revising: {changes}.` when it was the hard flag that stopped it, or `Round {n}: not good enough ({judgement.reason}), revising: {changes}.` when the judge said no at a passing score.
6. Continue means: `store.save_round_feedback(RoundFeedback(session_id=session.id, round=latest.rounds, text="\n".join(changes), author="critic"))`, then a child `revise_prompt` run (`comment=` the same text) awaited; then the next pass.
7. After the loop, `best`: the latest round's recommended sample; else, across every round, the highest `overall` with no hard flag; else the highest `overall`; else any sample with a photo (round 1 or later, not deleted). None → `raise ValueError("No photo could be made, so there is nothing to compose.")`. State `best_sample_id`.

**The final check** (`final_check`): for the first candidate: `ctx.state["final_check_context"] = {...}` (Task 2's keys; `layout` is `describe(candidate.composition)`), message parts `[types.Part(text="The finished post"), image_part(candidate png), *quality bar parts as the critic uses]`, `ctx.run_node(checker, node_input=..., use_sub_branch=True)` → `FinalReview`. `ship` → chosen. Not `ship` and a second candidate exists → decision `Final check: {score} of 5, not good enough to ship ({biggest_flaw}); trying the next layout.`, check the second the same way; chosen = the one with the higher score (the first on a tie). Any exception or None answer → chosen = the first candidate, decision `Final check skipped: {readable reason}.`, `final_review` None. Then decision `Final check: {score} of 5. Biggest flaw: {biggest_flaw}` for the chosen one (when reviewed), `state.final_review = review`, saved. State `chosen_candidate_id`.

**`run_auto`**: `settle` around `run_workflow` as every run; the result is `saved_post(store, state)` when state has `post_id`, else None. In a `finally` after `settle` (so it runs on failure and on cancellation too): re-read the session; `mode = "manual"`; when `auto_state.stopped_by` is still None, set it from the run's recorded status: `interrupted` → `"designer"` with the decision `Stopped by you.`; `failed` → `"error"` with `Stopped: {run.error}`; save. Cancellation keeps the v2.1 rules: the child in flight records itself interrupted and re-raises, the auto run records itself interrupted, the session is left with no run in flight.

**Keep passing:** `tests/test_workflows.py` (adjust only where a signature changed; `run_samples` keeps working without `count`), then the whole suite.

---

### Task 5: The web side of auto mode

**Files**
- Modify: `studio/web/routes.py`, `studio/web/templates/studio.html`, `session.html`, `post.html`, `run.html`, `runs.html`, `studio/web/static/app.js`, `app.css`
- Read first: `studio/contracts.py` (the v3 section), `studio/workflows/__init__.py` (Task 4: `run_auto`), `studio/store.py` (`list_runs_for_parent`), the existing routes, templates, script and styles, the spec's section 2 ("Pages")

**Routes added or changed**

| Method and path | Does |
|---|---|
| `POST /sessions` | New form fields: `mode` (`manual` or `auto`, default `manual`), `max_rounds` (int, default 3, clamped 1..5), `photo_budget` (int, default 6, clamped 1..12), `stop_score` (float, default 4.2, clamped 1..5). When `mode` is `auto`: `session.mode = "auto"`, `session.auto_settings = AutoSettings(...)`, `session.auto_state = AutoState()`, the run kind is `auto`, and `jobs.start(run.id, run_auto(deps, run, session))`. Manual is unchanged. |
| `GET /api/sessions/{id}` | Also returns `auto_state` (the dict or null) and `auto_status` (the status text below or null) |
| `GET /api/runs/{id}` | Also returns `children`: `[{"id", "kind", "kind_label", "status"}]` from `list_runs_for_parent`, and `parent_run_id` |

**Texts** (verbatim). Status line while an auto run is in flight, from `auto_state` and `auto_settings`: `Auto: round {min(rounds_done + 1, max_rounds)} of {max_rounds}, {photos_used} of {photo_budget} photos`. Run kind label for `auto`: `Automatic session`. Step labels (`STEP_LABELS` in routes and `app.js` both): `plan` "Plan the run", `draft` "Draft the prompt", `round_loop` "Make and judge the rounds", `compose` "Compose the layouts", `final_check` "Check the finished post", `finish` "Finish the post", `report` "Report". Studio hint under the limits: `Auto mode makes up to {photo_budget} photos on its own.` (updated live by the script as the number changes).

**Studio page:** next to the brief, a `pill-group` of two radios named `mode` (`Manual`, checked by default, and `Auto`). Under it, a block (`data-auto-limits`, hidden unless Auto is selected; the script toggles it) with three small number inputs with labels `Rounds`, `Photos`, `Stop score` (values 3, 6, 4.2; `step="0.1"` for the score) and the hint line above.

**Session page:** while the session's mode is `auto` and a run is in flight: the status line shows the auto status text (in place of the generic one), the Stop button stays, and under the status a list `data-auto-decisions` of `auto_state.decisions`. When the mode is back to manual and `auto_state` exists: a collapsible `How it was made` (`<details>`) above the finished panel listing the decisions. In the finished panel, when `auto_state.final_review` exists: `Final check: {score} of 5. Biggest flaw: {biggest_flaw}` and, on the next line, `{fix_hint}`. The script's `pollSession` updates the status text and rebuilds the decisions list from `auto_state.decisions` on every tick, and still reloads when the run ends.

**Post page:** `post.auto_summary`, when set, as a line above `Why it looks like this`.

**Run page:** when the run has `parent_run_id`: `Part of an automatic session: ` with a link to the parent run (`/runs/{parent_run_id}`). When `list_runs_for_parent` returns children: a `Child runs` list (kind label, status pill, link), server-rendered, and rebuilt by the script from `children` on each poll of `/api/runs/{id}` (so a running auto run's list grows). Runs list: the auto run reads `Automatic session`; child runs are listed as today.

**Both themes** checked on the new parts.

**Keep passing:** `tests/test_web.py`, then the whole suite.

---

### Task 6: The editor and uploads

**Files**
- Create: `studio/web/templates/editor.html`, `studio/web/static/editor.js`
- Modify: `studio/web/routes.py`, `studio/web/templates/session.html`, `archive.html`, `studio/web/static/app.css`
- Read first: `studio/contracts.py` (the v3 section), `studio/render/__init__.py` and `studio/render/custom.py` (Task 3: `default_layout`, `blocks_for_template`, `apply_guardrails`), `studio/workflows/__init__.py` (Task 4: `pick_source_photo`, `run_compose`), `studio/brand.py` (`logo_for_mode`), the existing routes (`compose_preview`, `finish_session`, `_session_busy`, `start_session_from_archive`), `session.html`, `app.js` (`pollSession`, `initAdjustForms`), the spec's section 3

**Routes**

| Method and path | Does |
|---|---|
| `GET /sessions/{id}/editor?sample=&from=` | The editor page. 404 when the sample is missing, deleted, has no image or belongs to another session. `from` (optional) names a layout candidate of this session: a `custom` candidate gives its `composition.custom`; a template candidate gives `blocks_for_template(candidate.composition, scrim_added=candidate.render_report.scrim_added)`; otherwise `default_layout()`. |
| `POST /sessions/{id}/editor/preview` | JSON body `{"sample_id", "headline", "subline", "mode", "layout": CustomLayout}`. 409 `{"error": "The session is busy."}` when busy; 404 for a bad sample. When the headline, subline or mode differ from the current version (or there is no version): save a designer `PromptVersion` (copy of the current one with those three fields; with no current version: `photo_prompt="The chosen photo, kept as it is."`, empty caption and hashtags, layout `hero`) and make it current. Create a `compose` run, set it active, and AWAIT `run_compose(deps, run, session, sample, compositions=[Composition(template="custom", custom=layout)])` (about a second). Reply `{"candidate_id", "image_url": "/media/{image_path}", "fits", "adjustments", "run_id"}`, or 500 `{"error": run.error, "run_id"}` when it failed. |
| `POST /sessions/{id}/upload` | multipart field `photo`. Refuse (400, the session page with the error `That file is not an image the studio can use.`) when the file is over 15 MB or Pillow does not open it as PNG or JPEG. Save under `store.uploads_dir / session.id / "{new_id()}.{png|jpg}"`; a `Sample(round=0, index=<next index among round 0>, image_path, provider="upload", status="candidate")`; redirect to `/sessions/{id}#rounds`. |
| `POST /archive/{sample_id}/edit` | Like `/archive/{sample_id}/start` but then `pick_source_photo(store, session, sample, None)` copies the photo into the new session at once, and redirects to `/sessions/{id}/editor?sample={copy.id}`. |
| `GET /brand/font` | The kit's regular font file (`FileResponse`), so the editor's canvas shows the real typeface |
| `GET /brand/logo?mode=dark|light` | `logo_for_mode(kit, mode, store.work_dir / "logos")` as a `FileResponse` |
| `POST /sessions/{id}/finish/{candidate_id}` | Unchanged; the editor's `Use this layout` posts here with the last preview's candidate id |

**The page** (`editor.html` + `editor.js`, no framework, pointer events; the page is the only one that loads `editor.js`, after `app.js`):
- Left: the canvas, 540 by 675, showing the photo (`/media/{sample.image_path}`) with the chosen fit, position and background; each block as an absolutely positioned, draggable, resizable element in the kit's font (`@font-face` from `/brand/font`) at half size (`size_px / 2`), the logo from `/brand/logo?mode=`. A selected block shows a resize handle at its bottom-right corner: text and logo resize by width (the logo keeps its proportions), a shade by width and height. Dragging and resizing keep every block inside the 4% margin, and the logo never narrower than 14%: the page clamps the same way the renderer does, so what the designer sees is what the renderer draws.
- Right: the panel for the selected block: the text (two inputs `headline` and `subline`, which are the version's words), size (number, px), alignment (`left`/`centre`), colour (the kit's palette as swatches with names; the first choice is `Brand colour` = `""`), opacity (a range, shade only), `Remove the shade` (shade only). Below: `Add a shade`; the photo: fit (`cover`/`contain`), pan arrows (±2% per press, within ±50), background colour (palette swatches, `Brand colour` = `""`); the post mode (`Dark`/`Light`, which switches the role colours and the logo file); the logo width.
- Bottom: `Preview` (posts the JSON, shows the returned PNG beside the canvas with its `adjustments` as lines, enables `Use this layout`), `Use this layout` (a form posting to `/sessions/{id}/finish/{candidate_id}`, disabled until a preview exists), `Back to the session`.
- Positions are kept as percentages in a JavaScript object that mirrors `CustomLayout`; the preview posts exactly that object. Reopening the editor with `from=` a custom candidate restores the arrangement from its composition.

**Session page:** every sample card with an image gets `Open in the editor` (`/sessions/{id}/editor?sample=`); every layout candidate card gets `Edit this layout` (`...?sample={candidate.sample_id}&from={candidate.id}`); round 0 is headed `Your photos` instead of `Round 0`; above the rounds, a small form `Upload a photo` (`enctype="multipart/form-data"`, `<input type="file" name="photo" accept="image/png,image/jpeg">`), disabled while a run is in flight. The `Rounds` section shows even when the only samples are round 0 (it already does: round groups come from `list_samples`). A custom candidate's caption reads `Custom layout` (from `describe`).

**Archive page:** each card with an image gets `Open in the editor` (a form posting to `/archive/{sample_id}/edit`).

**Styles:** the editor's layout (two columns on wide screens, stacked on a phone), the block outlines (a thin line in the studio's accent, only on the selected block), the handle, the swatches (a row of circles with the colour inside and the name as the title), the preview column. Both themes.

**Keep passing:** `tests/test_web.py`, then the whole suite.

---

### Task 7: The demo run-through

**Files:** none in the source tree. Writes `.superpowers/sdd/2026-10-06-design-studio-v3/task-7-screenshots/*.png` and its report.

Start an isolated container of the image in demo mode (see Global Constraints: no `.env`, a temporary data folder, port 8010, the code mounted read-only), then, with `curl` from Git Bash against `http://localhost:8010`:
1. Auto: `POST /sessions` with `brief=Announce our free consultation week to new patients and families`, `sample_count=2`, `mode=auto`, `max_rounds=2`, `photo_budget=4`, `stop_score=4.9` (so the stop score is rarely reached and the rounds limit ends the loop). Poll `GET /api/sessions/{id}` every 2 s until `run.status` is not `running` (allow 3 minutes). Check: the session's `mode` is back to `manual`, `auto_state.rounds_done <= 2`, `auto_state.photos_used <= 4`, `auto_state.decisions` has one line per round plus the final check line, the session has a `post_id`, and `GET /posts/{post_id}` shows `Made automatically:`. `GET /runs/{run_id}` lists the child runs; a child run page says `Part of an automatic session`.
2. Auto, stopped: start another auto session the same way, wait 3 s, `POST /sessions/{id}/stop`; poll until the run ends; check the run is `interrupted`, the session's `mode` is `manual`, `active_run_id` is null, and the decisions end with `Stopped by you.`
3. Auto, unusable brief: `brief=Hello there` with `mode=auto`; check the run ends `succeeded` with no post, the session is `needs_brief` and manual.
4. Editor: on a manual session with one sample, `GET /sessions/{id}/editor?sample=` returns 200; `POST .../editor/preview` with the default layout JSON returns a `candidate_id`; `POST /sessions/{id}/finish/{candidate_id}` makes a post whose `spec.layout` is `custom`; the editor reopened with `from=` that candidate holds the same blocks. Also post a layout with the logo at `w=5` and a headline at `x=-10`: the preview's `adjustments` hold the clamp lines.
5. Upload: `POST /sessions/{id}/upload` with a small PNG you generate (Pillow inside the container, or any PNG under `brands/hybridge/examples/`): a round-0 sample appears on the session page under `Your photos`; a text file and a 16 MB file are refused with `That file is not an image the studio can use.`
6. Screenshots, with Playwright inside the container (`docker exec <container> python -c ...` against `http://localhost:8000` from inside the container): the Studio page with Auto selected, the finished auto session page, the auto run page, the editor page, and the post page of the custom layout. Save them to the screenshots folder.

Report every check with its result, and every failure with the request, the response and your reading of the cause. Do not change source files; the fix round does that. Stop and remove the container at the end.
