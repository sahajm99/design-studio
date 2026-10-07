# Design Studio v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Put the designer in the middle of the image work: an editable detailed photo prompt, a chosen number of samples a round, a critic's scores, reactions and comments that rewrite the prompt, an archive with delete, and a dark theme.

**Architecture:** A session replaces the one-shot run. Four short runs (draft, samples, revise_prompt, finish) are started by buttons, with the session's state in the studio's own tables. The art director is split into a prompt writer (concept, prompt, words) and a critic (judgement). Everything else from v1 stays.

**Tech Stack:** unchanged from v1 (Python 3.12, google-adk 2.11, FastAPI, Jinja2, SQLite, Playwright, Pillow, httpx).

**Spec:** `docs/superpowers/specs/2026-10-05-design-studio-v2-design.md` (the v1 spec remains the reference for what v2 does not change).

## How this plan departs from the usual shape

Sahaj's instruction for v2 (2026-10-05): build the features fast and cleanly, write no new test cases, he tests by hand. So:

- Tasks give behaviour and exact interfaces; implementers write no new tests. They change or delete an existing test only when v2 removes or changes the behaviour it covers, and they run the existing suite once before reporting.
- No commits. Checkpoints are `git add -A` plus `git write-tree`.
- The controller wrote the new contracts and settings (already in `studio/contracts.py` and `studio/config.py`).
- Tasks in a wave run in parallel on disjoint files. Wave 1: Tasks 1 and 2. Wave 2: Tasks 3 and 4.
- One review for the whole v2 diff after the controller's end-to-end run, instead of a review per task.

## Global Constraints

- Everything runs in the container; the host has no Python. The existing suite runs with
  `docker compose --project-directory "C:/Users/sahaj/OneDrive/Desktop/Experiments/projects/active/design-studio" run --rm studio pytest -q`.
- A copy of the app is running in another container on port 8000 with real keys. Leave it alone: do not stop, restart or rebuild it, and do not use port 8000.
- No commits, and no git command that changes history or the working tree.
- Read-only files: `studio/contracts.py`, `studio/config.py`, `studio/brand.py`, `studio/library/base.py`, `studio/photos/base.py`, `tests/conftest.py`, `requirements*.txt`, `pytest.ini`, `Dockerfile`, `docker-compose.yml`, `brands/`. If one needs a change, stop and report it.
- Touch only the files your task lists.
- Nothing brand-specific inside `studio/`. No key, token or account id in code, logs or error messages.
- Text a person reads is plain language in sentence case with no internal names.
- ADK 2.x `Workflow` only; the patterns proven for v1 (in `studio/workflows/create.py` and the v1 plan) stay the way to do it.
- Write no new tests. Delete or change an existing test only when v2 removes or changes what it covers. The suite must pass when you report.

## Review Focus

Checked by the controller's end-to-end run and the whole-diff review, since no tests are written:

1. A pasted caption or an empty brief gets a question back; the session waits (Task 3, Task 4).
2. A round of N makes exactly N samples; one failure leaves N−1 with a reason; all failing fails the run with the provider's reason (Task 3).
3. A photo used by a post refuses deletion and the page says how many were skipped (Task 1, Task 4).
4. Text from a person or a model never reaches a page unescaped (Task 4).
5. The suite stays green after the removal of the v1 one-shot create run (Task 3, Task 4).

## File structure and ownership

```
studio/contracts.py, config.py, photos/base.py     foundation (done; read-only)
studio/store.py                                    Task 1
studio/models.py, photos/fake.py, photos/cloudflare.py   Task 2
studio/workflows/                                  Task 3  (agents.py, steps.py, analyse.py, create.py, session.py, __init__.py)
studio/main.py, studio/web/                        Task 4
tests/                                             any task: only to delete or adjust tests broken by v2
```

---

### Task 1: Store additions for sessions, prompt versions, samples, feedback and the archive

**Files**
- Modify: `studio/store.py`
- Read first: `studio/contracts.py` (the v2 section at the bottom), `studio/store.py`

**Interfaces this task produces.** Add to `Store`, keeping every existing method unchanged.

```python
samples_dir: Path   # data_dir / "samples", created by init()

# sessions
def save_session(self, session: StudioSession) -> None            # insert or replace; sets updated_at to now()
def get_session(self, session_id: str) -> StudioSession | None
def list_sessions(self, limit: int = 50) -> list[StudioSession]  # newest first by created_at

# prompt versions
def save_prompt_version(self, version: PromptVersion) -> None
def get_prompt_version(self, version_id: str) -> PromptVersion | None
def list_prompt_versions(self, session_id: str) -> list[PromptVersion]   # by number ascending

# samples
def save_sample(self, sample: Sample) -> None                        # insert or replace
def get_sample(self, sample_id: str) -> Sample | None
def list_samples(self, session_id: str) -> list[Sample]             # not deleted; by round then index ascending
def list_archive(self, *, reaction: Reaction | None = None, status: SampleStatus | None = None,
                 limit: int = 500) -> list[Sample]                  # every session; not deleted; newest first
def delete_sample(self, sample_id: str) -> bool                     # see rules below
def count_photos_since(self, since: datetime) -> int                # samples with an image_path created at or after since, deleted ones included

# round feedback
def save_round_feedback(self, feedback: RoundFeedback) -> None
def list_round_feedback(self, session_id: str) -> list[RoundFeedback]   # oldest first

# runs and posts
def list_runs_for_session(self, session_id: str) -> list[Run]       # newest first
def recent_headlines(self, brand_id: str, limit: int = 10) -> list[str]   # headlines of the newest posts for the brand, newest first
```

**Behaviour**
- Same storage pattern as v1: a row per object with a few plain columns plus a `json` column. New tables: `sessions(id, status, created_at, json)`, `prompt_versions(id, session_id, number, json)`, `samples(id, session_id, round, idx, status, reaction, created_at, json)`, `round_feedback(id, session_id, round, created_at, json)`. Created in `init()` with `IF NOT EXISTS`, so an existing database gains them on start-up.
- `list_runs_for_session` and `recent_headlines` read the existing `runs` and `posts` tables with SQLite's `json_extract(json, '$.session_id')` and `json_extract(json, '$.spec.headline')`, so no column is added to the old tables.
- `delete_sample`: `KeyError` when the id is unknown. Returns `False` and changes nothing when `used_in_post_id` is set. Otherwise removes the image file if there is one (`media_path(image_path)`, ignore a missing file), sets `status` to `deleted` and `deleted_at` to now, keeps `image_path` as it was, saves, and returns `True`.
- `list_samples` and `list_archive` never return deleted samples. `list_archive` filters combine with AND.
- Keep the plain columns in step with the JSON on every write, as v1 does.

**Report:** the suite's result (`pytest -q`), which must still pass.

---

### Task 2: The prompt writer and critic stand-ins, and seeded photo samples

**Files**
- Modify: `studio/models.py`, `studio/photos/fake.py`, `studio/photos/cloudflare.py`
- Read first: `studio/contracts.py` (v2 section), `studio/models.py`, `studio/photos/base.py`, `studio/photos/fake.py`, `studio/photos/cloudflare.py`

**Interfaces this task produces**

```python
# studio/models.py: two new roles beside ROLE_ANALYST and ROLE_ART_DIRECTOR
ROLE_PROMPT_WRITER = "prompt_writer"
ROLE_CRITIC = "critic"
# FakeLlm answers them as described below. get_llm, describe_llm, wrap_context, read_context are unchanged.

# studio/photos: the provider interface gained a seed (already in studio/photos/base.py)
async def generate(self, prompt: str, width: int, height: int, out_path: Path, *, seed: int | None = None) -> PhotoResult
```

**The prompt writer's context**, as Task 3 will send it. Keys, all optional:
`task` (`draft`, `revise` or `words`), `brief`, `brand` (`name`, `audience`, `feel`, `rules`), `taste` (a `TasteProfile` dict), `liked_cards` (list of `{id, label, card}`), `recent_headlines` (list of str), `backdrops` (`{"dark": "#hex", "light": "#hex"}`), `current` (a `PromptVersion` dict or null), `designer_edited` (bool), `round_comment` (str), `reactions` (list of `{reaction, verdict, flags, comment}`), `earlier_rounds` (list of str), `comment` (str, the words task).

**FakeLlm rules for `prompt_writer`.** It answers with a `PromptDraft` JSON.
- Task `draft`:
  - Fewer than 6 words in the brief: `usable` false, `question` = `Tell me who the post is for, what is on offer, and how it should feel.`, everything else empty or default.
  - Otherwise `mode` is the more common of dark and light in `taste["liked"]["background"]`, dark on a tie or with no likes; `layout` is `hero`; the subject is the first six words of the brief in lower case.
  - `photo_prompt` is exactly this template filled in, one paragraph:
    `A single {subject}, photographed as a premium studio still life. Setting: a seamless, evenly lit {mode} backdrop close to the colour {hex}, with nothing else in frame. Light: soft and directional, with a gentle falloff into the backdrop. Lens: 85mm, straight on, the subject centred with generous empty space around it. Colour: restrained, two tones at most, the subject's own material doing the work. Mood: calm, precise and quiet. No text, no logos, no watermarks.` where `{hex}` is `backdrops[mode]`.
  - `headline` is the first six words of the brief, first letter upper-cased, trailing punctuation removed, plus a full stop. If it equals one of `recent_headlines`, append ` Again.` to it.
  - `subline` is `A demo subline written without a model.`; `caption` is the headline plus ` This caption was written in demo mode.`; `hashtags` is `#` plus the brand name with everything but letters and digits removed, or none.
  - `concept`: `audience` = the brand's audience, `idea` = `Show {subject} with nothing else in frame.`, `feeling` = `calm`, `offer` = the brief's first sentence, `tone` = `quiet`.
  - `reason_prompt` = `Demo mode: the prompt follows a fixed template.`; `reason_words` = `Demo mode: the headline repeats the brief.`
- Task `revise`: start from `current`; append to `photo_prompt` a sentence ` Changed after feedback: {round_comment}.` when there is a round comment, and for each reaction that is `disliked` with a comment, ` Avoiding: {comment}.`; `reason_prompt` = `Kept the subject and setting; changed what the feedback asked.`; words unchanged; `usable` true.
- Task `words`: start from `current`; apply the v1 art director's comment rules to the words only ("light" sets mode light, "dark" sets mode dark, "shorter" cuts the headline to its first four words plus a full stop); the prompt is unchanged; `reason_words` = `Revised after the comment: {comment}`.

**FakeLlm rules for `critic`.** It answers with a `SampleReview` JSON, deterministic from SHA-256 of the image bytes in the request (or of the text when there is no image): `on_brief` = 2 + digest[0] % 4, `brand_fit` = 2 + digest[1] % 4, `craft` = 2 + digest[2] % 4; `flags` = `["busy"]` when digest[3] % 5 == 0, else empty; `verdict` = `Demo review: no model looked at this image.`; `suggested_change` = `Demo mode: nothing to suggest.`

**Photos**
- `FakePhotoProvider.generate` takes `seed`. The hash that places and tints the ellipse is now SHA-256 of `f"{prompt}|{seed}"` when a seed is given, so different seeds give different images for one prompt. Everything else, including the backdrop-colour rule, stays.
- `CloudflarePhotoProvider.generate` takes `seed`. Read the model page (https://developers.cloudflare.com/workers-ai/models/flux-2-klein-4b/) once more: if it lists a `seed` input, send it in the form when given; if it does not, ignore the seed and say so in your report. Everything else stays.

**Report:** the suite's result, and whether Cloudflare's page lists `seed`.

---

### Task 3: The session runs on ADK: draft, samples, revise_prompt, finish, and the words revision

**Files**
- Modify: `studio/workflows/agents.py`, `studio/workflows/create.py`, `studio/workflows/__init__.py`
- Create: `studio/workflows/session.py`
- Delete or change tests that cover removed behaviour: `tests/test_workflows.py`, `tests/test_agents.py`, `tests/test_web.py` (only the parts that call `run_create` or the v1 art director; Task 4 owns the web tests' routes, so coordinate by deleting only what breaks)
- Read first: `studio/contracts.py` (v2 section), `studio/store.py` (Task 1's additions), `studio/models.py` (Task 2's roles and context keys), `studio/photos/base.py`, every file under `studio/workflows/`

**Interfaces this task consumes:** the Task 1 store methods, the Task 2 context keys and roles, `Renderer.render`, `PhotoProvider.generate(..., seed=)`, `build_taste_profile`, `describe_llm`, `wrap_context`.

**Interfaces this task produces**

```python
# studio/workflows/__init__.py exports
Deps                      # unchanged
analyse_reference         # unchanged
run_draft(deps, run, session) -> StudioSession
run_samples(deps, run, session) -> list[Sample]
run_revise_prompt(deps, run, session) -> PromptVersion | None
run_finish(deps, run, session, sample) -> Post | None
run_revise(deps, run) -> Post | None      # the v1 words revision, now through the prompt writer's "words" task
# run_create and the art director are removed.
```

All five record their steps with the v1 `StepRecorder`, never raise except on cancellation (as v1), call `store.finish_run` on success or failure, and save the session with `active_run_id` cleared at the end. Each run gets its own ADK session, keyed by the run id, as in v1.

**Agents** (`agents.py`)
- `build_prompt_writer(llm) -> LlmAgent`: name `prompt_writer`, `output_schema=PromptDraft`, `output_key="prompt_draft"`. Its instruction function reads `ctx.state["prompt_writer_context"]`, returns the prompt below followed by `wrap_context(ROLE_PROMPT_WRITER, context)`, and adds the retry line when state holds `retry_note`, as v1 did. The user message carries the brief as text, and for the draft task also up to 4 liked reference images and the kit's ideal example image (`examples/ideal-output.png` when the kit folder has it) as image parts, each preceded by a text part naming it ("Liked reference: R1-B6 Rolex", "The brand's ideal example").
- The prompt writer's instruction, in plain specific sentences: you turn a brief into one post for the brand named in the context. First judge the brief: if it has fewer than six words, is a finished caption or marketing sentence rather than a request, or does not say what the post is about, set `usable` false and ask one question that would make it usable. Otherwise write the concept (who it is for, the one idea, the feeling, the offer, the tone). Write `photo_prompt` as one paragraph of 60 to 120 words in this order: the subject and what it is doing; the setting and a backdrop that matches the chosen mode (use the hex in `backdrops`); the light; the lens and framing; colour and material; mood. Describe what should be in the frame, never what should not, except the fixed ending "No text, no logos, no watermarks." Draw on what the liked reference images share and on the ideal example's finish. Do not repeat the subject or the headline of any of `recent_headlines`. Choose `hero` unless the brief asks for words only. Write a headline of at most eight words in sentence case, a subline of at most eighteen words, a caption of one to three sentences for the audience, and up to five hashtags. Never promise medical outcomes. When `task` is `revise`: keep the subject and setting of `current` unless the feedback asks otherwise, apply the round comment and the disliked samples' comments, keep what the liked samples share, and say in `reason_prompt` what you kept and what you changed. When `task` is `words`: change only the words that the comment asks about and return `photo_prompt` exactly as in `current`.
- `build_critic(llm) -> LlmAgent`: name `critic`, `output_schema=SampleReview`, `output_key="sample_review"`. Instruction: you judge one generated photograph for a post. Score from 1 to 5 how well it matches the prompt and concept (`on_brief`), how well it fits the brand's feel (`brand_fit`), and its craft (`craft`: sharpness, lighting, no artefacts, no broken anatomy). Raise a flag only when sure: `text_in_image`, `logo_in_image`, `distorted_anatomy`, `wrong_backdrop`, `busy`, `off_subject`. One sentence of verdict and one suggested change. The context block carries `prompt`, `concept` and `brand` feel; the user message is the image plus "Judge this sample."
- `build_analyst` stays as it is. Remove `build_art_director`.

**Runs** (`session.py`; keep `create.py` for the words revision and the shared helpers it already has, or move shared helpers as you see fit without changing their behaviour)

`run_draft` — steps `load_context` → `write_prompt` → `save_draft`
- `load_context`: taste profile, liked cards (at most 12), up to 4 liked reference image files, the ideal example image if present, `recent_headlines`, `backdrops` from `kit.mode_hex`, the brand block. Puts the context dict in state as `prompt_writer_context` with `task: "draft"`. Returns the brief text; the images are added to the user message when the workflow is started.
- `write_prompt`: the prompt writer with the v1 code-driven retry (two attempts; a validation error or a blank answer sets `retry_note`). Provider `describe_llm(deps.llm)`, note = the concept's idea, or the question.
- `save_draft`: `usable` false → `session.status = "needs_brief"`, `session.question`, no version. Otherwise a `PromptVersion` (number = one more than the count so far, author `agent`) with the prompt and words, `session.concept`, `session.current_prompt_version_id`, `session.question = ""`, `session.status = "drafted"`. Hashtags cleaned as in v1. Return the session.

`run_samples` — steps `load_prompt` → `generate_samples` → `review_samples` → `save_round`
- `load_prompt`: the current prompt version; no provider configured fails the run with `No photo provider is configured.`
- `generate_samples`: N = `session.sample_count`, clamped to 1..`settings.max_samples`. The prompt sent is the version's `photo_prompt` plus the v1 backdrop sentence built from the version's mode. N calls at once with `asyncio.gather`, each with a random seed, writing to `store.samples_dir / session.id / f"{round}-{index}.png"`. Each result becomes a `Sample` (round = `session.rounds + 1`, index 1..N) with `image_path` and `provider`, or with `error` set to the `PhotoUnavailable` message (other exceptions: `The photo service failed.`). Note: `{ok} of {N} samples made.` All failing: fail the run with the first error message, keep no samples, leave the session as it was.
- `review_samples`: for each sample with an image, run the critic (at most 3 at a time), set `review` or `review_error` (`The critic could not score this sample.`). Then `recommended` = true on the one sample with the highest `overall` and no hard flag, when any. Provider `describe_llm`; note `{scored} of {ok} scored.`
- `save_round`: save the samples, `session.rounds += 1`, `session.status = "reviewing"`, save the session. Return the samples.

`run_revise_prompt` — steps `load_feedback` → `rewrite_prompt` → `save_draft`
- `load_feedback`: the current version; the latest round's `RoundFeedback` text if any; the latest round's samples that have a reaction, as `{reaction, verdict, flags, comment}` (verdict and flags from the review when present); one line per earlier round: `Round {n}: {count} samples, {disliked} disliked. {feedback text}`; `designer_edited` = current.author == `designer`. Context `task: "revise"`.
- `rewrite_prompt`: the prompt writer with the retry. `save_draft` as above with author `agent_after_feedback`, `status = "drafted"`. Return the version.

`run_finish` — steps `load_pick` → `render` → `save_post`
- `load_pick`: the sample's prompt version → a `DesignSpec` (layout `hero` when the sample has an image, else `type_only`; mode, words and hashtags from the version; `photo_prompt` from the version; `reference_ids` empty; `reason_layout` = the version's `reason_prompt`, `reason_words` = the version's `reason_words`).
- `render`: as v1, with the sample's image file.
- `save_post`: a `Post` with `session_id`, `sample_id`, `photo_path` = the sample's image path, `photo_provider` = the sample's provider. A session that already has a post makes the next version under that post's root; otherwise version 1. Then the sample's `status = "picked"`, `used_in_post_id`; every other sample of the same round that is still `candidate` becomes `rejected`; `session.picked_sample_id`, `session.post_id`, `session.status = "finished"`. Return the post.

`run_revise` (words): as v1, but the agent is the prompt writer with `task: "words"`, `current` built from the post's spec, and `comment` = the run's comment. The photo is always reused; a mode change no longer makes a new photo. Keep the v1 notes and versioning.

Any step that raises ends the run as `failed` with a readable message (v1 rules) and sets the session's `active_run_id` to `None` without changing its status.

**Report:** the suite's result after deleting the tests that covered the removed create run and art director, and anything about ADK that behaved differently from the v1 patterns.

---

### Task 4: Web: sessions, the archive, the theme, and the wiring

**Files**
- Modify: `studio/main.py`, `studio/web/routes.py`, `studio/web/jobs.py`, `studio/web/templates/base.html`, `studio.html`, `post.html`, `runs.html`, `run.html`, `studio/web/static/app.css`, `app.js`
- Create: `studio/web/templates/session.html`, `studio/web/templates/archive.html`
- Change `tests/test_web.py` only where v2 removes or changes what a test covers (the v1 one-click create is gone; its tests go)
- Read first: `studio/contracts.py` (v2 section), `studio/store.py`, `studio/workflows/__init__.py`, `studio/workflows/session.py`, `studio/web/routes.py`, `studio/web/jobs.py`, the templates and static files

**Interfaces this task consumes:** Task 1's store methods; Task 3's `run_draft`, `run_samples`, `run_revise_prompt`, `run_finish`, `run_revise`; `Settings.max_samples`.

**Routes**

| Method and path | Does |
|---|---|
| `GET /studio` | The brief box, a "Samples a round" number input (1 to `settings.max_samples`, default 3), the line `Photos made today: n` (`count_photos_since` midnight UTC), the recent sessions with their status, and recent posts. The old one-click Generate is gone |
| `POST /sessions` | Fields `brief`, `sample_count`. Empty brief: 400 with `Write a brief first.` Creates a `StudioSession`, starts a `draft` run in the background (`jobs.start`), sets `active_run_id`, redirects 303 to `/sessions/{id}` |
| `GET /sessions/{id}` | The session page |
| `POST /sessions/{id}/brief` | Field `brief`: replaces the brief, starts another draft run, redirects to the session |
| `POST /sessions/{id}/generate` | Fields `photo_prompt`, `headline`, `subline`, `caption`, `hashtags` (space or comma separated), `mode`, `layout`, `sample_count`. If any of the prompt or words differ from the current version, save a new `PromptVersion` by `designer` and make it current. Save `sample_count` (clamped). Start a `samples` run, redirect to the session |
| `POST /sessions/{id}/reset` | Makes the latest agent-written version current again (the newest version whose author is not `designer`), redirects |
| `POST /sessions/{id}/feedback` | Field `text` for the latest round. Saves a `RoundFeedback` when non-empty, starts a `revise_prompt` run, redirects |
| `POST /sessions/{id}/again` | Starts a `samples` run from the current version, redirects |
| `POST /api/samples/{id}/reaction` | JSON `{"reaction": "liked"|"disliked"|"none", "comment": "..."}`; a dislike sets `status` to `rejected`, a like or none on a rejected sample sets it back to `candidate`; returns the sample |
| `POST /sessions/{id}/use/{sample_id}` | Runs `run_finish` to completion (it takes about a second), then redirects to `/posts/{post_id}`; on failure, redirects to the run page |
| `GET /api/sessions/{id}` | JSON: the session, its active run's status if any, and the number of rounds. The page polls this while a run is active and reloads when it ends |
| `GET /archive` | Query `filter` in `all`, `liked`, `disliked`, `picked`, `unrated`. The archive page |
| `POST /archive/delete` | Field `ids` (repeated). Deletes each through `store.delete_sample`; shows the archive with `Deleted {n} photos. Skipped {k} used by a post.` |
| `POST /archive/delete-disliked` | Deletes every disliked sample the same way |
| `GET /posts/{id}` | As v1, plus a session link, `Change the photo` (to the session), and the sample's scores under the image when it has a review |
| `GET /runs/{id}`, `GET /runs` | As v1, plus a session link on runs that have `session_id`; the new step names in plain words (see Task 3's list in the spec) |

Pages are rendered with the kit's name in the header as before. A session's active run is shown as a status line ("Writing the prompt…", "Making 3 samples…", "Rewriting the prompt…") with the poll.

**The session page** (`session.html`), three parts:
1. Brief and concept: the brief in an editable box with `Draft again`; the concept's five fields when present; the question in a callout when the session `needs_brief`.
2. Prompt and words (shown once a version exists): the photo prompt in a large text box; headline, subline, caption, hashtags fields; mode and layout choices; the sample count; `Generate`; a `Reset to the agent's version` link; the version's `reason_prompt` and `reason_words` in small text; `Version {n} by {author}` where the author reads "the agent", "you" or "the agent, after your feedback".
3. Rounds, newest first: a card per sample: the photo (or the error in its place), the three scores and the overall as `on brief 4 · brand 5 · craft 3 · overall 4.0`, the flags as small labels, the verdict, a `Recommended` mark, like and dislike buttons (filled when active), a comment field that saves on blur, and `Use this photo`. A rejected sample is greyed. Under each round: the round's feedback text if any, a text box, `Revise the prompt with this feedback`, and `Generate again`. When the session is finished, the top of the page shows the post's image with `Open the post` and the rounds stay below.

**The archive page** (`archive.html`): the filter links, a grid of samples newest first with the photo, the session link, the round, the prompt's first 90 characters with a `more` toggle, the overall score, the reaction, `Used in a post` when so, and a checkbox. A toolbar with `Delete selected` and `Delete all disliked`; each asks for confirmation in the browser that states the count. The result line after a delete.

**The theme.** `base.html` gets a toggle button in the header (sun or moon, with an accessible label). `app.css` defines the studio's colours as custom properties on `:root` with a light set, and a dark set under `:root[data-theme="dark"]` and under `@media (prefers-color-scheme: dark)` guarded by `:root:not([data-theme="light"])`. `app.js` sets `data-theme` from `localStorage` on load (before paint, in a small inline script in `base.html` to avoid a flash) and flips it on the toggle. Posts are untouched. Check every page in both themes: contrast, borders, the banners, the sample cards, images on dark.

**Wiring** (`main.py`, `jobs.py`): `RunJobs` gains `start(coro)` for any run coroutine, keeping the v1 behaviour; the lifespan is unchanged except that `run_create` no longer exists. `mark_interrupted_runs` at start-up also clears `active_run_id` on sessions whose run was interrupted (use `list_sessions` and `get_run`).

**Report:** the suite's result after removing the tests for the one-click create, and screenshots of the session page (both themes) and the archive if you take them.
