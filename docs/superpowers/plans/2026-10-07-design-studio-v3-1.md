# Design Studio v3.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** The six changes of the v3.1 design: the comparison final check, framing and layout that follow the brief, the quality bar in the studio, an Editor tab, library upload and pasted links, and the parked minors.

**Architecture:** Unchanged. One new store table (`settings`), one new reference source, one new helper (`quality_bar`) that replaces three readers, prompt changes, and web pages.

**Spec:** `docs/superpowers/specs/2026-10-07-design-studio-v3-1-design.md`. Section numbers below refer to it. Its texts are binding.

## How this plan departs from the usual shape

As in v2 to v3: no new tests (adjust a test only where a change breaks what it covers; the suite must pass when you report), no commits, the controller wrote the contracts, one review of the whole diff at the end, then one fix round. Sahaj tests by hand once the app runs.

## Global Constraints

- Everything runs in the container; the host has no Python. Suite:
  `docker compose --project-directory "C:/Users/sahaj/OneDrive/Desktop/Experiments/projects/active/design-studio" run --rm studio pytest -q`
  Run only the covering test files while working; the whole suite once before reporting.
- A copy of the app runs on port 8000 with real keys and the live database. Leave it alone. Never `docker compose up` or `docker compose run` the app. To look at a page, use an isolated `docker run` of `design-studio:dev` on port 8010 or above with a temporary data folder, no `.env`, the code mounted read-only, and stop it after (`MSYS_NO_PATHCONV=1` in Git Bash):
  `docker run --rm -d --name studio-check-$RANDOM -p 8010:8000 -e STUDIO_DATA_DIR=/app/data -e STUDIO_DB_PATH=/app/data/studio.db -e STUDIO_BRANDS_DIR=/app/brands -v "<repo>/studio:/app/studio:ro" -v "<repo>/brands:/app/brands:ro" -v "<repo>/scripts:/app/scripts:ro" -v "<temp folder>:/app/data" --ipc=host design-studio:dev`
- No commits, no git command that changes the index, history or working tree.
- Read-only: `studio/contracts.py`, `studio/config.py`, `studio/brand.py`, `tests/conftest.py`, `requirements*.txt`, `pytest.ini`, `Dockerfile`, `docker-compose.yml`, `brands/`, `docs/`, `scripts/`, `samples/`.
- Touch only the files your task lists. Nothing brand-specific inside `studio/`; the layout files hold no typeface, brand name or hex colour. No secrets anywhere. Person-facing texts verbatim from the spec.
- ADK 2.x `Workflow` patterns as in `studio/workflows/`: function steps, `node(fn, rerun_on_resume=True)` around a step that calls `ctx.run_node`, `StepRecorder`, `settle`, `update_session`.

## Contracts already in `studio/contracts.py`

`FinalReview.pick: int = 1` (1-based, `ge=1`), `FinalReview.reasons: list[str] = []`, `AutoState.picked_layout: str = ""`, `QualityBarSource = Literal["kit","post","sample","upload"]`, `QualityBar(source, image_path, label="", set_at)`.

## File ownership and order

```
Task 2  studio/store.py, studio/models.py, studio/library/sources.py, studio/render/renderer.py (7f)
Task 3  studio/workflows/agents.py, shared.py, session.py, auto.py, layouts.py, create.py
Task 4  studio/web/routes.py, templates (base, library, studio, session, post, archive, editor-picker new), static/app.js, app.css
Task 5  the demo run-through (no source files)
```

Sequential: 2, then 3, then 4, then 5.

---

### Task 2: Store, stand-ins, the link source, the contrast decode

**Files:** `studio/store.py`, `studio/models.py`, `studio/library/sources.py`, `studio/render/renderer.py`. Read first: the contracts' v3.1 additions, the existing store methods, the fake roles, `BoardHtmlSource` and `FolderSource`, the custom contrast code in the renderer, spec sections 2, 3, 4, 6 and 7f, 7g.

**Store.**
```python
quality_bar_dir: Path                              # data_dir / "quality-bar", made by init()
def get_setting(self, key: str) -> dict | None     # the stored JSON object, or None
def set_setting(self, key: str, value: BaseModel | None) -> None   # None deletes the row
# table: CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, json TEXT NOT NULL)
```
`mark_interrupted_runs` also ends every still-running step of each run it marks: status `failed`, error `The studio restarted while this step was in progress.` (7g).

**Stand-ins (`FakeLlm`).** `ROLE_FINAL_CHECK`: read `post_count` from the context (default 1); answer `FinalReview(ship=True, score=4, biggest_flaw="Demo mode: no model looked at the post.", fix_hint="Demo mode: nothing to fix.", pick=1, reasons=["Demo mode: no model compared the posts."] * post_count)`. `ROLE_PROMPT_WRITER`, task draft: read `allowed_layouts` (a list; default `["hero"]`); choose `full_bleed` when the brief contains any of `person`, `people`, `patient`, `smile`, `scene`, `room`, `clinic`, `team` and `full_bleed` is allowed, else `hero`; everything else as today.

**Link source** (`studio/library/sources.py`): `LinkListSource(urls: list[str])` implements `ReferenceSource`: `items()` yields a `SourceItem` per entry that starts with `http://` or `https://` (whitespace stripped, blanks and others skipped, at most 20), `label` = the last path part without its extension (or the host when there is none), `category = "pasted"`, `source = "links"`; match the fields `BoardHtmlSource` fills.

**Renderer (7f).** In the custom contrast check, open, EXIF-transpose and convert the photo once and measure every text block from that image.

**Keep passing:** `tests/test_store.py`, `tests/test_models.py`, `tests/test_sources.py`, `tests/test_render.py`, then the whole suite.

---

### Task 3: The runs: comparison final check, framing and layout, the quality bar reader, the minors

**Files:** `studio/workflows/agents.py`, `shared.py`, `session.py`, `auto.py`, `layouts.py`, `create.py`. Read first: Task 2's report, spec sections 2, 3, 4 (the reader), 7c, 7d.

**`shared.py`.**
```python
def with_backdrop(photo_prompt: str, mode: Mode, kit: BrandKit, layout_hint: LayoutTemplate = "hero") -> str
    # always: "{prompt} Set on a seamless, evenly lit {mode} backdrop close to the colour {hex}."
    # hero, type_only: + " with the subject centred and empty space around it" (inside the sentence, as today)
    # full_bleed, caption_strip: + " Keep the bottom third of the frame plain, backdrop only."
    # corner: + " Place the subject in the lower right of the frame."   split, custom: nothing more
def quality_bar(store: Store, kit: BrandKit) -> tuple[Path | None, str]
    # the setting "quality_bar" (QualityBar) when its file exists under the data folder: (path, f"set by you from {label}")
    # else the kit's examples/ideal-output.png when it exists: (path, "the kit's example"); else (None, "none")
```
Every `with_backdrop` caller passes the current version's layout. `quality_bar()` replaces `_ideal_example` in the draft images (label `The brand's ideal example`), `_quality_bar` for the critic, and the final check's bar in `auto.py`. The draft's `load_context` note and the samples run's `review_samples` note end with ` Quality bar: {label}.`

**`agents.py`.** `PROMPT_WRITER_PROMPT`: replace the hero-by-default line with the spec's layout rule (section 3, verbatim) and add the framing rule (verbatim). `FINAL_CHECK_PROMPT`: the comparison prompt of section 2: the posts arrive after the lines `Post 1`, `Post 2`, `Post 3`, then the quality bar after `The brand's quality bar.`; compare readability of the words, the logo's clarity, how the layout serves the photo, the brand's feel; `pick` the one to ship; `ship` and `score` for it; `reasons` one line per post in order; `biggest_flaw` and `fix_hint` for the pick. The final-check context gains `post_count` and `layouts` (the `describe()` of each, in order).

**`session.py`.** The draft and revise contexts gain `allowed_layouts: allowed_templates(kit)`. The samples run and the critic context pass the version's layout to `with_backdrop`.

**`auto.py`.** `final_check` sends one call with every candidate of the compose run (at most three): parts `Post 1`, image, `Post 2`, image, …, then the bar. `pick` out of range or a failed call → the first candidate and the decision `Final check skipped: {reason}.`. Decision line: `Final check: picked {describe(composition)} ({score} of 5). Biggest flaw: {biggest_flaw}` with `, would not ship as it is` after the score when `ship` is false. Set `auto_state.picked_layout`. The step note holds the decision line. 7c: in `_back_to_manual`, when the session has a post whose `run_id` is a child of this auto run and whose `auto_summary` is empty, set the summary and append the decision `Stopped after the post was saved.`. 7d: `plural()` for `{n} layouts ready.` (in `layouts.py`'s `save_layouts` note and anywhere else layouts are counted), `Making {n} samples…` (`routes.py` is Task 4's; export `plural` already exists in `auto.py`) and `{n} of {count} samples made.` in `session.py`.

**Keep passing:** `tests/test_workflows.py`, `tests/test_agents.py`, then the whole suite.

---

### Task 4: The web side: quality bar, the Editor tab, library upload and links, the minors

**Files:** `studio/web/routes.py`, `studio/web/templates/base.html`, `library.html`, `studio.html`, `session.html`, `post.html`, `archive.html`, new `editor_picker.html`, `studio/web/static/app.js`, `app.css`. Read first: Tasks 2 and 3's reports, spec sections 4, 5, 6, 7a, 7b, 7e, 7h, and the existing routes for the session upload (`upload_photo`, `_upload_extension`), the archive edit (`edit_from_archive`, `_session_from_archive`), the library import.

**Quality bar** (spec section 4): the Library page panel with the texts `Quality bar`, `The brand's ideal example from the kit.` / `Set by you from {label} on {date}.`, `Upload an image`, `Back to the kit's example`, `This image is the bar the critic holds every photo to.`; routes `POST /quality-bar/upload` (field `image`), `POST /quality-bar/from-post/{post_id}`, `POST /quality-bar/from-sample/{sample_id}`, `POST /quality-bar/reset`; the chosen image copied into `store.quality_bar_dir / f"{new_id()}.png"` (PNG via Pillow) and saved with `store.set_setting("quality_bar", QualityBar(...))`; `Set as the quality bar` buttons on the post page (beside Approve), on every sample card with a photo and on every archive card; a `Quality bar` thumbnail beside the brief on the session page (from `quality_bar(store, kit)`; the kit's file served through a new `GET /brand/quality-bar` route, the designer's through `/media/`).

**Editor tab** (section 5): `Editor` in the top bar after `Archive`; `GET /editor` renders `editor_picker.html` with `Start from an upload` (file field `photo`, button), `Start from a post` (posts with a photo, as a contact sheet), `Start from a photo` (archive photos with an image, newest first); every card is a form to `POST /editor/start` with `post_id` or `sample_id` or the file; the route creates the session, copies the photo with `pick_source_photo` (an upload becomes a round-0 upload sample as on the session page), and redirects to the editor (with `from=` the session's chosen layout when starting from a post and that layout belongs to the post's photo). Share the helper with `/archive/{id}/edit`.

**Library** (section 6): `Upload images` (multipart `images`, several files; each PNG or JPEG under 15 MB saved into `store.inbox_dir` under a server-chosen name, then the inbox import and the analysis job start; refused files listed in the summary as `{n} refused: {name} is not an image the studio can use.`); `Paste image links` (textarea `links`, hint `One link per line, up to 20.`) → `LinkListSource` + the HTTP fetcher + `import_references`; the note `Only add images you are allowed to use. A reference shapes the prompt; it is never copied into a post.`

**Minors.** 7a (the file inputs' `color-scheme` keyed on the theme), 7b (`pollSession` skips a mid-run reload while a sample comment has focus), 7e (the finished panel's line `Final check picked {picked_layout}.` above the final-check line, and `would not ship as it is` when `ship` is false), 7h (`editor_preview` stores the guarded layout as the candidate's composition: apply `apply_guardrails` before composing), `Making {n} samples…` pluralised.

**Keep passing:** `tests/test_web.py`, then the whole suite.

---

### Task 5: The demo run-through

No source files. In an isolated demo container (the recipe above, with `scripts/` mounted): (1) a manual session whose brief mentions a person gets layout `full_bleed` in its draft and a full-bleed candidate first in the shortlist; (2) an auto session's decisions include `Final check: picked …` and `auto_state.picked_layout` is set; (3) the Library page shows the kit's quality bar; `POST /quality-bar/from-sample/{id}` changes it and the next draft's load-context note names it; reset restores the kit's; (4) `GET /editor` lists a post and a photo; `POST /editor/start` with a `sample_id` redirects to the editor; with an upload too; (5) `POST /library/upload` with a PNG adds a reference; `POST /library/import-links` with one good link (a `file://` is not allowed; use `http://localhost:8000/brand/logo` inside the container) and one bad link gives one reference and one unavailable; (6) after `docker kill` and a restart on the same data folder, an interrupted run's steps read failed, not running. Screenshots of the Library panel, the Editor tab and a finished auto session into the ledger folder. Report every check with its result; change no source file.
