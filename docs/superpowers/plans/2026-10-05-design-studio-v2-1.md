# Design Studio v2.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Six layout templates chosen from where the photo's subject sits, with a Compose step; feedback that overrides the prompt with a change list and a diff; one comment box on the post page; a stricter critic that ranks a round; comments on liked samples; sessions started from the archive; trimming, progress and stop.

**Architecture:** Unchanged from v2. A new `compose` run renders the chosen photo in the fitting compositions; the critic reports subject position; rules shortlist; the designer picks. The finish run copies the chosen candidate instead of rendering.

**Spec:** `docs/superpowers/specs/2026-10-05-design-studio-v2-1-design.md`. The v2 and v1 specs remain the reference for what v2.1 does not change.

## How this plan departs from the usual shape

Same as v2, by Sahaj's instruction: no new tests (change or delete only tests that v2.1 breaks; the suite must pass when you report), no commits, the controller wrote the contracts (already in `studio/contracts.py`), tasks in a wave run in parallel on disjoint files, one review of the whole diff at the end.

## Global Constraints

- Everything runs in the container; the host has no Python. Suite:
  `docker compose --project-directory "C:/Users/sahaj/OneDrive/Desktop/Experiments/projects/active/design-studio" run --rm studio pytest -q`
- A copy of the app runs on port 8000 with real keys and the live database. Leave it alone. Never start a copy that shares its `.env` or database (`docker compose run` does); to look at a page, use an isolated `docker run` with a temporary data folder and no `.env`, and stop it after.
- No commits, no git command that changes history or the working tree.
- Read-only: `studio/contracts.py`, `studio/config.py`, `studio/brand.py`, `studio/library/base.py`, `studio/photos/base.py`, `tests/conftest.py`, `requirements*.txt`, `pytest.ini`, `Dockerfile`, `docker-compose.yml`, `brands/`.
- Touch only the files your task lists. Nothing brand-specific inside `studio/`. No secrets in code, logs or messages. Person-facing text is plain language in sentence case.
- ADK 2.x `Workflow` only, with the v2 patterns in `studio/workflows/`.
- `hero` and `type_only` must render exactly as today: the existing render tests check pixels.

## Review Focus

Checked by the controller's run-through and the whole-diff review: (1) a photo with calm space on one side gets words on that side; (2) words over a busy photo get the shade and the report says so; (3) "no silver" removes "chrome" or the page flags it; (4) a photo comment on the post page lands on the session with a note; (5) the ranking sets the recommended pick; (6) a session from an archived photo reaches Compose without samples; (7) Stop keeps finished samples.

## File structure and ownership

```
studio/render/                 Task 1  (renderer.py, layouts/*, compose.py, __init__.py)
studio/store.py, studio/models.py   Task 2
studio/workflows/              Task 3  (and tests it must adjust: tests/test_workflows.py, tests/test_agents.py)
studio/main.py, studio/web/    Task 4  (and tests/test_web.py)
```

Waves: Tasks 1 and 2 together; then Tasks 3 and 4 together.

---

### Task 1: Layout templates, composition parameters, the contrast check and the shortlist rules

**Files**
- Modify: `studio/render/renderer.py`, `studio/render/layouts/base.css`, `studio/render/layouts/hero.html`, `studio/render/layouts/type_only.html`, `studio/render/__init__.py`
- Create: `studio/render/layouts/full_bleed.html`, `split.html`, `corner.html`, `caption_strip.html`, `studio/render/compose.py`
- Read first: `studio/contracts.py` (the layouts section: `LayoutTemplate`, `Composition`, `TEXT_OVER_PHOTO`, `SampleReview.subject_x/subject_y/calm_areas`, `RenderReport.text_contrast/scrim_added`, `BrandKit.layouts`), the existing renderer and templates, `tests/test_render.py` (what must keep passing), and the spec's section 3

**Interfaces this task produces**

```python
# studio/render/compose.py
def allowed_templates(kit: BrandKit) -> list[LayoutTemplate]
    # kit.layouts filtered to known names, or all six in this order: hero, full_bleed, split, corner, caption_strip, type_only
def shortlist(review: SampleReview | None, hint: LayoutTemplate, allowed: list[LayoutTemplate],
              *, has_photo: bool = True) -> list[Composition]
    # ordered, no duplicates; the first three are the main picks; the rest follow under "More layouts"
def describe(composition: Composition) -> str
    # "Hero", "Full bleed, words bottom left, logo top right", "Split, photo left", "Corner",
    # "Caption strip, logo left", "Words only"

# studio/render/renderer.py (signatures unchanged; behaviour widened)
def build_html(spec, kit, *, logo_url, photo_url, font_url, italic_font_url, headline_px, scrim: bool = False) -> str
class Renderer:
    async def render(self, spec, kit, photo_path, out_path) -> RenderReport
```

**Templates.** All share one base template (fonts, tokens, the fit script, the logo rules); each template adds its own markup and CSS; parameters arrive as CSS classes and custom properties. Canvas is `kit.post_size`. Margins are 8% of the width unless stated. The logo is placed with an `<img>` sized by width only; width 30% of the canvas for `top_centre` in `hero` (as today), 24% elsewhere; clear space of half its height all round.

| Template | Composition |
|---|---|
| `hero` | Exactly as today. |
| `type_only` | Exactly as today. |
| `full_bleed` | The photo covers the whole canvas (`object-fit: cover`). The logo sits at `logo_position` with a 6% margin. The headline and subline sit at `text_position` (top or bottom, 8% from the edge) aligned `text_align`, width 84%. When the words sit over the photo the contrast check below applies. |
| `split` | `photo_side` left or right: the photo covers 55% of the width, full height; the other 45% is a solid panel in the background colour with the logo at the top of the panel (position within the panel from `logo_position`'s horizontal part) and the words vertically centred, aligned `text_align`. `photo_side` top: the photo covers the top 58%, the panel the bottom 42%, logo top-left of the panel, words left-aligned beside or under it. |
| `corner` | Solid background. The headline sits top-left (x 8%, y 9%, width 62%), left-aligned, the subline under it. The photo sits in a box from x 36% to 100% and y 44% to 100% with `object-fit: cover`, its top and left edges feathered into the background with a gradient mask of about 12% of the box. The logo sits bottom-left at `logo_position` row bottom (default `bottom_left`), 6% margin. |
| `caption_strip` | The photo covers the top 76% (`object-fit: cover`). A solid band in the background colour fills the bottom 24%: the logo on the left (or right, by `logo_position`) and the headline on the other side, at most two lines, vertically centred; no subline. |

Headline start sizes: hero 64, type_only 96, full_bleed 60, split 56, corner 72, caption_strip 40; step 4; minimum 36 for caption_strip and 44 for the rest. The fit script's safe area is the template's text box; the subline scales with the headline as today.

**The contrast check** (templates in `TEXT_OVER_PHOTO`, with `scrim` `auto`): render once, read the text block's rectangle from the fit script, compute in Python the region of the photo as drawn under that rectangle (cover-fit maths: scale to cover the photo box, centre crop), take its mean relative luminance with Pillow at a reduced size, and compute the WCAG contrast ratio against the text colour. Under 4.5, render again with `scrim=True`: a gradient in the background colour from 0 to 70% opacity behind the text block, extending one text-height above and below it, drawn under the words. `scrim: on` always adds it; `off` never does. Set `RenderReport.text_contrast` (the ratio before the shade, rounded to one decimal) and `scrim_added`; when a shade was added, add the adjustment line `A shade was added behind the words for contrast.` A photo that cannot be read for the check: add the shade and the line `The photo could not be measured, so a shade was added.`

**Shortlist rules**, applied in order; stop adding once every allowed template has been placed; remove compositions whose template is not allowed or (without a photo) needs one:

1. The hint first, with its default parameters, when it is allowed.
2. `bottom` in `calm_areas`: `full_bleed` with `text_position=bottom`, `text_align` opposite the subject (`subject_x` right → left, left → right, centre → centre), `logo_position` top on the same side as the text.
3. `top` in `calm_areas`: `full_bleed` with `text_position=top`, aligned the same way, `logo_position` bottom on the side of the text.
4. `left` or `right` in `calm_areas` with the subject on the other side: `split` with `photo_side` on the subject's side; and `corner` when `subject_x` is right and `subject_y` is bottom.
5. No calm area, no review, or a hard flag: `hero`, then `split` with `photo_side=top`.
6. Then `caption_strip` with the logo on the side away from the subject, then any remaining allowed template with defaults.

`describe()` is used as each candidate's caption, so make it read well.

**Visual pass.** After it works, render the six templates with the Hybridge kit and a generated photo (dark and light where it matters), look at them with your image viewer, and refine spacing until they look premium and restrained like the ideal example. Save them under `data/samples-layouts/` and list them in your report.

**Keep passing:** `tests/test_render.py`. The `hero` and `type_only` outputs must not change.

---

### Task 2: Store additions and the stand-in updates

**Files**
- Modify: `studio/store.py`, `studio/models.py`
- Read first: `studio/contracts.py` (sessions and layouts sections), the existing store methods and the existing fake roles

**Store additions**

```python
candidates_dir: Path   # data_dir / "compositions", created by init()
def save_round_review(self, review: RoundReview) -> None
def get_round_review(self, session_id: str, round: int) -> RoundReview | None
def save_layout_candidate(self, candidate: LayoutCandidate) -> None
def get_layout_candidate(self, candidate_id: str) -> LayoutCandidate | None
def list_layout_candidates(self, session_id: str, sample_id: str | None = None) -> list[LayoutCandidate]   # by index
def delete_layout_candidates(self, session_id: str, sample_id: str) -> int   # removes their files and rows; returns the count
def update_step_note(self, event_id: int, note: str) -> None   # KeyError when unknown
```
Tables `round_reviews(id, session_id, round, json)` and `layout_candidates(id, session_id, sample_id, idx, json)`, same row-plus-JSON pattern, created in `init()`.

**Stand-in updates in `FakeLlm`** (every rule deterministic, every answer valid for its schema)
- `critic`: also fill `subject_x` from digest[4] % 3 over (left, centre, right), `subject_y` from digest[5] % 3 over (top, middle, bottom), and `calm_areas` from digest[6] % 5: 0 → `["bottom"]`, 1 → `["top"]`, 2 → `["left"]`, 3 → `["right"]`, 4 → `[]`.
- New role `ROLE_CRITIC_RANK = "critic_rank"`: the context carries `sample_indexes` (a list of ints); answer a `RoundRanking` with `order` = those indexes sorted ascending, `reasons` = `Demo ranking: sample {i}.` for each, `note` = `Demo ranking: no model compared these samples.`
- `prompt_writer`, task `draft`: `changes` = `["Demo mode: a new prompt from the template."]`, `scope` = `words`.
- `prompt_writer`, task `revise`: as today, plus the banned-term rule. Banned terms are the words that follow `no`, `not`, `without`, `instead of`, `remove` or `never` in `round_comment` and in each disliked reaction's `comment`, up to the next punctuation mark or the end, at most three words each, lower-cased. Remove every occurrence of each banned term from the prompt (case-insensitive, whole words), tidy double spaces, and add a change line `Removed "{term}".` per term. For each liked reaction with a comment add `Kept the liked sample's look; applied: {comment}` to `changes` and append ` Also: {comment}.` to the prompt. Keep the existing `Changed after feedback:` sentence.
- `prompt_writer`, task `words`: as today, plus `scope`: `photo` when the comment contains any of `photo`, `image`, `picture`, `backdrop`, `background`; `both` when it also contains any of `headline`, `caption`, `subline`, `words`, `hashtag`; otherwise `words`.
- `prompt_writer`, new task `words_for_photo`: words as the draft task writes them from the brief; `photo_prompt` = `The chosen photo, kept as it is.`; `changes` = `["Demo mode: words written for an existing photo."]`; `usable` follows the draft rule.

**Keep passing:** the whole suite (no existing behaviour changes except the additions above).

---

### Task 3: The runs: compose, the critic's position and ranking, overrides, scope, reuse, trim, progress and stop

**Files**
- Modify: `studio/workflows/agents.py`, `studio/workflows/session.py`, `studio/workflows/shared.py`, `studio/workflows/create.py`, `studio/workflows/__init__.py`; adjust `tests/test_workflows.py` and `tests/test_agents.py` only where v2.1 changes what they cover
- Read first: `studio/contracts.py`, `studio/render/compose.py` and `renderer.py` (Task 1), `studio/store.py` and `studio/models.py` (Task 2), every file under `studio/workflows/`, the spec's sections 3 to 9

**Interfaces this task produces** (exported from `studio/workflows/__init__.py`; the v2 ones stay)

```python
run_compose(deps, run, session, sample, compositions: list[Composition] | None = None) -> list[LayoutCandidate]
    # None: shortlist from the sample's review, the current version's layout as the hint, and allowed_templates(kit);
    # render the first three. A list: render exactly those (used for "More layouts", "Adjust" and "Preview").
run_finish(deps, run, session, candidate: LayoutCandidate) -> Post | None     # replaces the v2 signature
run_draft(deps, run, session) -> StudioSession                               # now also handles source_sample_id
run_revise(deps, run) -> Post | None                                         # now reads the comment's scope
```

**Critic** (`agents.py`): the per-sample message gains the kit's ideal example image (when `examples/ideal-output.png` exists in the kit folder) preceded by `The brand's quality bar.`; the instruction adds: judge realism (anatomy, materials, light and shadow that obey physics) and raise `unrealistic` or `wrong_materials` when sure; report `subject_x`, `subject_y` and `calm_areas` (areas plain enough to hold words); keep the anchors; name the single biggest weakness. New `build_critic_ranker(llm)`: name `critic_rank`, `output_schema=RoundRanking`, `output_key="round_ranking"`; the message holds every scored sample's image, each preceded by `Sample {index}`, and the context (`wrap_context(ROLE_CRITIC_RANK, {...})`) carries `sample_indexes`, `prompt`, `concept`, `brand` feel; the instruction: rank best first, one reason each, one note on the round, judge realism and brand fit over polish.

**Samples run**: after `review_samples`, a new step `rank_samples` when two or more samples were scored: run the ranker; store a `RoundReview`; set `rank` and `rank_reason` on each sample; `recommended` = the top-ranked sample without a hard flag. One sample: rank 1, no call. Ranking failure: keep the v2 rule and note `Ranking skipped: {reason}`. `generate_samples` updates its step note as each sample lands (`store.update_step_note`, `1 of 3 made…`), and on `asyncio.CancelledError` cancels the rest, saves the finished samples as a round with the note `Stopped after {k} of {N}.`, bumps `session.rounds`, sets status `reviewing` when k ≥ 1, then re-raises.

**Reviser** (`revise_prompt`): the instruction adds the override rule (anything the feedback names wins; remove contradicted words; never keep a material the feedback rejects because a liked sample had it; a comment on a liked sample means keep its subject, setting and look and change only what the comment says; a comment on a disliked sample names what to avoid; list every removal, addition and kept element in `changes`). Code guard: banned terms are the phrases after `no`, `not`, `without`, `instead of`, `remove`, `never` in the round comment and the disliked samples' comments (same rule as the stand-in). When the new prompt still contains one: retry once with `retry_note` = `Your prompt still contains "{term}", which the feedback asked to remove.`; if it still does, save the version with `banned_terms_left`. Then trim: over 120 words, cut at the last sentence end before the 120th word and add `Trimmed to 120 words.` to `changes`. Save `changes` on the version. The draft task is trimmed the same way.

**Words task** (`run_revise`): the prompt writer's answer carries `scope`. `words`: as v2. `photo` or `both`: after the words step (and after the words revision when `both`), save a `RoundFeedback` with the comment on the session's latest round (round 0 when there is none), create a `revise_prompt` run for the session (`store.create_run(..., session_id=)`), set the session's `active_run_id` to it, and await `run_revise_prompt`. The words run's step note reads `The comment asks for a new photo, so it went to the session.` and the run ends succeeded (with no post for `photo`). A post without a session treats every scope as `words`.

**Draft from an archived photo** (`run_draft` when `session.source_sample_id` is set): the context task is `words_for_photo`; the source sample's image goes with the brief as `The chosen photo.`; after `save_draft`, copy the sample into the session: a new `Sample` with round 0, index 1, the same `image_path`, `provider`, `review`, status `picked`; set `session.picked_sample_id` to it; status `drafted`.

**Compose run** (`run_compose`): steps `load_pick` → `shortlist` → `render_layouts` → `save_layouts`. `load_pick` loads the current version and builds the base `DesignSpec` as the v2 finish did. `shortlist` as described, note `{n} layouts fit; rendering {m}.`; when a list is given, note `Rendering {m} chosen layouts.` `render_layouts`: for each composition, a spec copy with `layout` and `composition` set, rendered to `store.candidates_dir / session.id / f"{sample.id}-{index}.png"` (index continues from existing candidates for that sample), two at a time; a failed render is skipped with its reason in the note. `save_layouts`: `LayoutCandidate` rows, `session.picked_sample_id`, `session.status = "composing"`. Returns the candidates.

**Finish run** (`run_finish` with a candidate): `load_pick` → `save_post`. Copies the candidate's file to `store.posts_dir / f"{post_id}.png"`, builds the `Post` from the candidate's sample and the current version with `spec.layout` and `spec.composition` from the candidate and the candidate's render report; marks the sample `picked`, the round's other candidates `rejected`, `session.picked_candidate_id`, `post_id`, status `finished`.

**Keep passing:** adjust the tests that covered the v2 finish signature and the recommended-pick rule; the rest of the suite stays green.

---

### Task 4: The web side: Layout part, stop, the single comment box, diff and change list, archive reuse

**Files**
- Modify: `studio/main.py`, `studio/web/routes.py`, `studio/web/jobs.py`, `studio/web/templates/session.html`, `post.html`, `run.html`, `runs.html`, `archive.html`, `studio/web/static/app.css`, `app.js`; adjust `tests/test_web.py` only where v2.1 changes what it covers
- Read first: `studio/contracts.py`, `studio/store.py` (Task 2), `studio/workflows/__init__.py` (Task 3's signatures), `studio/render/compose.py` (`describe`, `allowed_templates`), the existing routes and templates

**Routes added or changed**

| Method and path | Does |
|---|---|
| `POST /sessions/{id}/use/{sample_id}` | Now starts a `compose` run (background) and redirects to the session |
| `POST /sessions/{id}/compose/more` | `compose` run with the shortlist's remaining compositions (those not yet rendered for the picked sample) |
| `POST /sessions/{id}/compose/preview` | Fields `template`, `logo_position`, `text_position`, `text_align`, `photo_side`, `scrim`: a `compose` run with that one composition |
| `POST /sessions/{id}/finish/{candidate_id}` | Runs `run_finish` to completion, redirects to the post |
| `POST /sessions/{id}/stop` | `jobs.cancel(session.active_run_id)`; redirects to the session |
| `POST /posts/{id}/revise` | Unchanged route; the run page now shows the scope note and, when the run has no post, `Open the session` |
| `POST /archive/{sample_id}/start` | Creates a session with `source_sample_id`, status `needs_brief`, empty brief; redirects to it |
| `GET /api/sessions/{id}` | Also returns the active run's latest step note |

`RunJobs.cancel(run_id)` cancels the task for that run if it is still running and returns whether it did.

**Session page**
- Part 1, for a session with `source_sample_id` and no brief yet: the photo shown as the pick, a brief box, `Draft`.
- Part 2: under the current version, a collapsible `What changed from version N−1` with the version's `changes` list and a word-level diff of the prompt built with `difflib.SequenceMatcher` on words (added words in `<ins>`, removed in `<del>`, styled). When `banned_terms_left` is non-empty: `The prompt still mentions "chrome".` next to the version line. The reason lines stay.
- Part 3 (rounds): each sample shows `Rank {n} of {m}` and its `rank_reason` when ranked; the round shows the critic's note; under a liked sample's comment field: `Keeps this look; your comment says what to change.`; a `Stop` button while a run is active, with the latest step note beside the status line (`Making samples… 1 of 3 made`).
- New part, Layout, shown while `composing` or when candidates exist: the candidates as thumbnails with `describe()` as the caption, the contrast note when a shade was added, `Use this layout`; `More layouts`; `Adjust` opens a small form (selects for the six parameters, defaults from the selected candidate) with `Preview`. A session with a picked source photo shows `Compose with this photo` instead of `Use this photo`.
- The finished panel keeps `Open the post` and `Change the photo`; add `Change the layout` (scrolls to the Layout part).

**Post page**: one comment box labelled `Ask for a change`, with the help text `Words or the photo. A photo request goes to the session's prompt.`; `Revise` posts to the same route as before.

**Archive**: each card gets `Start a session with this photo`.

**Runs**: plain names for the new steps: `rank_samples` "Rank the samples", `shortlist` "Choose fitting layouts", `render_layouts` "Render the layouts", `save_layouts` "Save the layouts".

**Both themes** checked on the new parts.

**Keep passing:** adjust `tests/test_web.py` where the finish route changed; the rest stays green.
