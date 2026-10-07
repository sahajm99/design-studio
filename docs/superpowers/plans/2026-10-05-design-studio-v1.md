# Design Studio v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A designer can import references, mark them liked or not, type a brief, get a finished on-brand post with a caption, revise it by comment, and see each run's steps, all from one `docker compose up`.

**Architecture:** One container. A FastAPI app with server-rendered pages calls two ADK 2.x workflows (analyse a reference, create or revise a post). Agents decide content and return typed JSON; plain code orders the steps, fetches the photo and renders the post from HTML/CSS in headless Chromium. Every outside service sits behind an interface with a fake, so the whole loop runs with no keys.

**Tech Stack:** Python 3.12, google-adk 2.11.0, FastAPI, Jinja2, SQLite (stdlib), Playwright Chromium 1.63, Pillow, httpx, BeautifulSoup, pytest.

**Spec:** `docs/superpowers/specs/2026-10-05-design-studio-v1-design.md`

## How this plan departs from the usual shape

Sahaj's standing instruction is small plans and fast builds, and on 2026-10-05 he said not to commit yet. So:

- Tasks give behaviour, exact interfaces and named tests. They give code only where exactness matters: the shared contracts (already in the repo) and the ADK patterns proven on 2026-10-05.
- Nobody commits. Checkpoints are `git add -A` plus `git write-tree`.
- The foundation below was written by the controller and is read-only for tasks.
- Tasks in the same wave run in parallel and own disjoint files.

## Global Constraints

- Everything runs in the container. The host has no Python. Run tests with
  `docker compose --project-directory "C:/Users/sahaj/OneDrive/Desktop/Experiments/projects/active/design-studio" run --rm studio pytest <test files> -q`.
  During your task run only your own test files; other tasks' files may be half written.
- Tests never touch the network and never need a key.
- No commits, and no git command that changes history or the working tree (no commit, reset, checkout, stash, clean).
- Read-only files: `studio/contracts.py`, `studio/config.py`, `studio/brand.py`, `studio/photos/base.py`, `studio/library/base.py`, `tests/conftest.py`, `requirements*.txt`, `pytest.ini`, `Dockerfile`, `docker-compose.yml`, `brands/`. If one needs a change, stop and report it; do not edit it.
- Touch only the files your task lists.
- Nothing brand-specific inside `studio/`: no brand name, brand colour or font name. Those live only in `brands/` and in tests.
- No key, token or account id in code, logs, error messages or test fixtures.
- Text a person reads (pages, notes, error messages) is plain language in sentence case, with no internal names.
- Python 3.12 with type hints, Pydantic v2, async for anything that waits on I/O. Small focused modules.
- ADK 2.x `Workflow` only. Do not use `SequentialAgent`, `LoopAgent` or `ParallelAgent`: they are deprecated.
- Step names are exactly `load_context`, `art_director`, `get_photo`, `render`, `save_post`.
- Test output must be clean: no warnings, no stray prints.

## Review Focus

Inputs the spec implies but does not spell out. Each has a named test in the task that owns it.

1. A headline containing HTML characters or quotes must render as text, not markup (Task 2, `test_build_html_escapes_text`).
2. A link that returns a web page or an oversized file instead of an image must be skipped with a reason, not stored (Task 4, `test_fetcher_rejects_non_image` and `test_fetcher_rejects_oversized`).
3. A model answer that names references that do not exist, or asks for a photo layout without describing a photo, must be cleaned up, not trusted (Task 5, `test_unknown_reference_ids_are_dropped` and `test_hero_without_photo_prompt_becomes_type_only`).
4. Two runs started at once must both finish and not share state (Task 5, `test_two_runs_at_once`).
5. A media address that climbs out of the data folder must return "not found" (Task 1, `test_media_path_rejects_escape`; Task 6, `test_media_rejects_path_escape`).
6. An empty brief must be refused with a clear message, and a very long one accepted without breaking the page (Task 6, `test_empty_brief_is_refused` and `test_long_brief_is_accepted`).

## File structure and ownership

```
studio/
  contracts.py        foundation   every typed object (read this first)
  config.py           foundation   Settings.from_env()
  brand.py            foundation   load_brand_kit(), logo_for_mode()
  photos/base.py      foundation   PhotoProvider, PhotoResult, PhotoUnavailable
  library/base.py     foundation   SourceItem, ReferenceSource, FetchedImage, FetchError, ImageFetcher
  store.py            Task 1
  library/taste.py    Task 1
  render/             Task 2
  models.py           Task 3
  photos/             Task 3       (__init__.py, fake.py, cloudflare.py)
  library/            Task 4       (sources.py, fetch.py, importer.py, analysis.py)
  workflows/          Task 5
  main.py, web/       Task 6
tests/
  conftest.py         foundation   fixtures: settings, hybridge_kit, second_brands_dir, second_kit, store
brands/hybridge/      foundation   brand.yaml plus the kit Hybridge supplied
```

Waves: Tasks 1, 2 and 3 together; then Tasks 4 and 5 together; then Task 6.

---

### Task 1: Store, taste profile and brand-kit tests

**Files**
- Create: `studio/store.py`, `studio/library/taste.py`, `tests/test_store.py`, `tests/test_taste.py`, `tests/test_brand.py`
- Read first: `studio/contracts.py`, `studio/brand.py`, `tests/conftest.py`

**Interfaces this task produces.** Later tasks call these exact names.

```python
# studio/store.py
class Store:
    def __init__(self, db_path: Path, data_dir: Path) -> None: ...
    references_dir: Path   # data_dir / "references"
    photos_dir: Path       # data_dir / "photos"
    posts_dir: Path        # data_dir / "posts"
    work_dir: Path         # data_dir / "work"    scratch files and the logo cache
    inbox_dir: Path        # data_dir / "inbox"   images the designer drops in to import

    def init(self) -> None: ...
    def relative(self, path: Path) -> str: ...
    def media_path(self, relative: str) -> Path: ...

    def upsert_reference(self, ref: Reference) -> None: ...
    def get_reference(self, ref_id: str) -> Reference | None: ...
    def find_reference_by_hash(self, content_hash: str) -> Reference | None: ...
    def list_references(self, *, status: RefStatus | None = None, choice: Choice | None = None) -> list[Reference]: ...
    def set_choice(self, ref_id: str, choice: Choice) -> None: ...
    def set_style_card(self, ref_id: str, card: StyleCard) -> None: ...
    def set_reference_status(self, ref_id: str, status: RefStatus, error: str | None = None) -> None: ...

    def create_run(self, kind: RunKind, brand_id: str, *, brief: str = "", comment: str = "",
                   parent_post_id: str | None = None) -> Run: ...
    def finish_run(self, run_id: str, status: RunStatus, *, error: str | None = None,
                   post_id: str | None = None) -> None: ...
    def get_run(self, run_id: str) -> Run | None: ...
    def list_runs(self, limit: int = 50) -> list[Run]: ...
    def mark_interrupted_runs(self) -> int: ...

    def start_step(self, run_id: str, step: str) -> int: ...
    def end_step(self, event_id: int, status: StepStatus, *, provider: str = "", attempts: int = 1,
                 note: str = "", error: str | None = None) -> None: ...
    def list_events(self, run_id: str) -> list[RunEvent]: ...

    def save_post(self, post: Post) -> None: ...
    def get_post(self, post_id: str) -> Post | None: ...
    def list_posts(self, limit: int = 50) -> list[Post]: ...
    def list_versions(self, root_post_id: str) -> list[Post]: ...
    def approve_post(self, post_id: str) -> None: ...

# studio/library/taste.py
def build_taste_profile(references: list[Reference]) -> TasteProfile: ...
```

**Store behaviour**
- Standard-library `sqlite3`, a new connection per call (`sqlite3.connect(db_path, timeout=30)`). Do not turn on WAL: the file may sit on a mount that handles it badly. Calls are short and synchronous, and async code calls them directly.
- Each object is one row: a few plain columns for filtering and ordering, plus a `json` column holding `model_dump_json()`, read back with `model_validate_json`. Tables: `refs(id, status, choice, content_hash, created_at, json)`, `runs(id, status, created_at, json)`, `run_events(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id, json)`, `posts(id, root_post_id, version, created_at, json)`. Keep the plain columns in step with the JSON on every write.
- `init()` creates the database's parent folder, the tables (`IF NOT EXISTS`) and the five data folders. Calling it twice is harmless.
- `relative(path)` returns the path relative to `data_dir` with forward slashes, and raises `ValueError` if the path is outside it. `media_path(relative)` resolves a relative path under `data_dir` and raises `ValueError` if the result escapes `data_dir` (`../x`, absolute paths, `a/../../b`). It does not check that the file exists.
- `upsert_reference` and `save_post` insert or replace by id.
- `set_choice`, `set_style_card`, `set_reference_status`, `approve_post`, `finish_run` and `end_step` raise `KeyError` naming the id when the row does not exist. `set_style_card` also sets the status to `analysed` and clears `error`.
- `list_references` returns oldest first (created time, then id). The two filters combine with AND.
- `create_run` builds a `Run` with status `running`, stores it and returns it. `finish_run` sets status, error, post id and `finished_at`. `list_runs` returns newest first.
- `mark_interrupted_runs` turns every `running` run into `interrupted` with the error "The studio restarted while this run was in progress." and returns how many it changed.
- `start_step` stores a `RunEvent` with status `running` and returns its integer id. `end_step` sets status, `ended_at`, provider, attempts, note and error. `list_events` returns a run's events in id order with `id` filled in.
- `list_posts` returns newest first. `list_versions` returns one root's posts by version ascending. `approve_post` sets status `approved`.
- Datetimes stay timezone-aware UTC through a round trip.

**Taste behaviour**
- Count only references that have a `style_card` and whose choice is `liked` or `disliked`.
- `liked[field][value]` is the count for each field in `STYLE_FIELDS`; leave out zero counts. Same for `disliked`. `liked_count` and `disliked_count` are the numbers of counted references. `liked_reference_ids` lists counted liked ids in input order.
- The summary uses only the phrases in `STYLE_LABELS`. For a group of n counted references, write `Liked {n}: ` then, for each field in `STYLE_FIELDS` order, the single most common value when its count times two is at least n, as `{count} {label}`; join with `, ` and end with a full stop. A tie goes to the value that comes first in `STYLE_LABELS[field]`. If no field qualifies write `Liked {n}: no clear pattern yet.` The disliked group reads the same with `Disliked {n}: `. Join the two groups with one space. With nothing counted the summary is `No likes or dislikes yet.`
- Example: three liked references, all dark, all `centred_subject`, text amounts few, few, some, subjects product, product, person, all `one_or_two` colours, gives exactly
  `Liked 3: 3 dark background, 3 single centred subject, 2 few words, 2 product as subject, 3 one or two colours.`

**Tests to write first, then make pass**

`tests/test_store.py`
- `test_init_is_idempotent_and_creates_folders`
- `test_reference_round_trip_with_style_card`
- `test_list_references_filters_and_order`
- `test_choice_survives_a_new_store_instance`: a second `Store` on the same paths sees the choice.
- `test_setters_raise_keyerror_for_unknown_ids`: covers all six setters named above.
- `test_find_reference_by_hash`
- `test_run_lifecycle`: create, two steps started and ended, finish; event order, `finished_at` set, `attempts` and `note` stored.
- `test_mark_interrupted_runs`
- `test_post_versions_and_approve`
- `test_media_path_rejects_escape`: `"../secret"`, `"/etc/passwd"` and `"a/../../b"` raise `ValueError`; `"posts/x.png"` resolves under the data folder.
- `test_relative_rejects_outside_paths`
- `test_datetimes_round_trip_timezone_aware`

`tests/test_taste.py`
- `test_nothing_counted`
- `test_example_summary`: the example above, exact string.
- `test_ignores_unrated_and_unanalysed`
- `test_disliked_group_is_joined_with_a_space`
- `test_no_clear_pattern`
- `test_majority`
- `test_tie_goes_to_first_label`

`tests/test_brand.py` tests the loader that already exists. Do not edit `studio/brand.py`; if a test exposes a bug, report it.
- `test_hybridge_kit_loads`: name `Hybridge`, dark background `#18181B`, light headline `#0F4B7B`, post size 1080 by 1350, inspiration board set.
- `test_missing_manifest_message`
- `test_missing_logo_file_message`: copy the second brand, delete its logo; the error names "logo lockup" and the file.
- `test_unknown_mode_colour_message`
- `test_invalid_hex_message`
- `test_second_brand_loads`
- `test_knockout_logo_for_dark`: for Hybridge on dark, the file is in the cache folder, has the lockup's size, every pixel that is not fully transparent is white, and the alpha channel equals the lockup's.
- `test_logo_is_untouched_for_light_and_for_as_is_kits`

**Report:** the test command you ran and its output for your three test files.

---

### Task 2: Renderer and layouts

**Files**
- Create: `studio/render/renderer.py`, `studio/render/layouts/base.css`, `studio/render/layouts/hero.html`, `studio/render/layouts/type_only.html`, `tests/test_render.py`
- Replace the empty `studio/render/__init__.py` so it exports `Renderer` and `build_html`
- Read first: `studio/contracts.py` (`DesignSpec`, `BrandKit`, `RenderReport`), `studio/brand.py` (`logo_for_mode`), `tests/conftest.py`, and look at `brands/hybridge/examples/ideal-output.png`

**Interfaces this task produces**

```python
# studio/render/renderer.py
def build_html(spec: DesignSpec, kit: BrandKit, *, logo_url: str, photo_url: str | None,
               font_url: str, italic_font_url: str | None, headline_px: int) -> str: ...

class Renderer:
    def __init__(self, work_dir: Path) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def render(self, spec: DesignSpec, kit: BrandKit, photo_path: Path | None,
                     out_path: Path) -> RenderReport: ...
```

**Behaviour**
- Use Playwright's async API. `start()` launches one headless Chromium and keeps it; calling it twice is harmless; `render` calls it when needed; `stop()` closes it. Each render opens its own page with the viewport set to `kit.post_size` and a device scale factor of 1, and closes it afterwards, so renders can run at the same time.
- Templates are Jinja2 with autoescape on, loaded from `studio/render/layouts/`. `base.css` is inlined into the page.
- Every brand value enters as a CSS custom property set in one `<style>` block: `--bg`, `--headline`, `--body` from `kit.mode_hex(spec.mode)`, plus `--font-family`, `--headline-weight`, `--body-weight`, `--headline-tracking` and `--headline-size`. The templates and CSS contain no brand name, no brand colour and no font name.
- Fonts load through `@font-face` from the kit's font files by `file://` URL, with `font-weight: 100 900` so a variable font works. Wait for `document.fonts.ready` before measuring.
- Write the page to a scratch file under `work_dir / "render"` and open it by `file://` URL so local fonts, logo and photo load. Delete the scratch file afterwards.
- Logo: use `logo_for_mode(kit, spec.mode, work_dir / "logos")`. Place it with an `<img>` sized by width only. Never stretch, crop, filter or add effects. Keep everything else at least half the logo's height away from it.
- Layout `hero` follows the composition of the ideal example, not its content. The logo is centred near the top. The photo fills the area between logo and text with `object-fit: cover` and fades into the background colour at its bottom edge and softly at its top edge, using a gradient built from `--bg`, so it melts into the canvas. The headline is centred below, the subline under it. Side margins are about 8% of the width. All text sits on the solid background colour, never on the photo.
- Layout `type_only` has no photo. The logo is centred near the top, the headline is large and centred around the optical middle, the subline beneath, with a lot of empty space.
- Headline: the kit's headline weight and tracking, the text exactly as given (never change its case), line height about 1.08, balanced wrapping. Subline: the body weight, about 40% of the headline size, line height about 1.35, slightly transparent.
- Fitting: start the headline at 84px for `hero` and 112px for `type_only`. The page exposes `window.studioFit()` returning `{fits: boolean, overflow: string[]}`. It is true when no text box overflows and the text block stays inside the safe area, clear of the bottom margin, the photo and the logo. While it does not fit, step the headline down 6px at a time to a minimum of 48px; the subline scales with it. When the size was reduced add one adjustment line such as `Headline reduced from 84px to 66px to fit.` If it still does not fit at 48px, set `fits` to false, add `Text does not fit at the smallest size. Shorten the copy.` and still take the screenshot.
- `hero` with no photo raises `ValueError("The hero layout needs a photo")`. A photo path that does not exist raises `FileNotFoundError`. An empty subline leaves the subline element out.
- The screenshot is a PNG of exactly `kit.post_size`, written to `out_path`, creating parent folders.
- Visual quality is the point of this task. Once the tests pass, render four samples with the Hybridge kit (hero dark, hero light, type-only dark, type-only light), look at the PNGs with your image viewer, compare them with the ideal example, and refine spacing and sizes until they look premium and restrained. Save them in `data/samples-render/` and list them in your report. For the sample photo draw a soft radial gradient with Pillow; download nothing.

**Tests to write first, then make pass** (`tests/test_render.py`, one Chromium for the module)
- `test_build_html_escapes_text`: a headline of `<b>Fish & chips</b> "quoted"` appears in the HTML as `&lt;b&gt;Fish &amp; chips&lt;/b&gt;` and never as `<b>Fish`.
- `test_layout_files_hold_no_brand_values`: no file in `layouts/` contains "Inter" or "Hybridge" in any case, or a `#` followed by six hex digits.
- `test_hero_dark_size_and_background`: Hybridge, hero, dark, generated photo; the PNG is 1080 by 1350 and the pixel at (5, 1345) matches the kit's dark background within 2 per channel.
- `test_type_only_light_without_photo`
- `test_hero_without_photo_raises`
- `test_missing_photo_file_raises`
- `test_long_headline_steps_down`: a 200-character headline gives a non-empty `adjustments`, and either fits or carries the "Shorten the copy." line with `fits` false.
- `test_short_headline_needs_no_adjustment`
- `test_empty_subline_is_left_out`: checked on `build_html`.
- `test_logo_keeps_its_proportions`: with `second_kit` (a solid magenta 400 by 100 logo, used as it is on dark) the bounding box of near-magenta pixels has a width to height ratio within 3% of 4.0 and sits at least 40px from every edge.
- `test_second_brand_colours`: dark corner pixel near `#102030`, light corner pixel near `#FFF8E7`.
- `test_hybridge_logo_is_white_on_dark`: the top quarter of a Hybridge dark render has near-white pixels and none near navy `#00305E`.
- `test_photo_is_cropped_not_stretched`: a 640 by 480 photo holding one centred bright-green circle comes out with a bounding box whose ratio is within 5% of 1.0.
- `test_three_renders_at_once`

**Report:** the test command and output, the four sample paths, and one line on what you changed after looking at them.

---

### Task 3: Model gateway and photo providers

**Files**
- Create: `studio/models.py`, `studio/photos/fake.py`, `studio/photos/cloudflare.py`, `tests/test_models.py`, `tests/test_photos.py`
- Replace the empty `studio/photos/__init__.py` so it holds `get_photo_provider`
- Read first: `studio/contracts.py` (`StyleCard`, `DesignSpec`, `STYLE_FIELDS`), `studio/config.py`, `studio/photos/base.py`

**Interfaces this task produces**

```python
# studio/models.py
ROLE_ANALYST = "analyst"
ROLE_ART_DIRECTOR = "art_director"
def wrap_context(role: str, context: dict) -> str: ...
def read_context(text: str) -> tuple[str | None, dict]: ...
class FakeLlm(BaseLlm): ...            # model: str = "fake"
def get_llm(settings: Settings) -> BaseLlm: ...
def describe_llm(llm: BaseLlm) -> str: ...

# studio/photos/__init__.py
def get_photo_provider(settings: Settings) -> PhotoProvider | None: ...

# studio/photos/fake.py
class FakePhotoProvider: ...           # name = "fake"

# studio/photos/cloudflare.py
class CloudflarePhotoProvider:         # name = "cloudflare"
    def __init__(self, account_id: str, api_token: str, model: str, *,
                 client: httpx.AsyncClient | None = None, timeout: float = 90.0) -> None: ...
```

**The context block.** `wrap_context(role, context)` returns exactly this text:

```
ROLE: {role}
<context>
{json.dumps(context, ensure_ascii=False)}
</context>
```

`read_context(text)` finds the first `ROLE:` line and the JSON between `<context>` and `</context>` anywhere in the text and returns `(role, context)`, or `(None, {})` when either is missing or the JSON is invalid. Agent instructions embed this block, so the fake model and the real model receive the same input.

**FakeLlm**, proven shape on google-adk 2.11.0:

```python
from collections.abc import AsyncGenerator
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

class FakeLlm(BaseLlm):
    model: str = "fake"

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        instruction = str(llm_request.config.system_instruction or "")
        # image bytes, when present: part.inline_data.data for part in content.parts
        text = '{"...": "..."}'
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))
```

It reads the role and context from the system instruction with `read_context` and answers with one JSON text part.

- Role `analyst` returns a `StyleCard`. It is deterministic: take SHA-256 over the bytes of every `inline_data` part in `llm_request.contents`, or over the joined text parts when there is no image. For field number i in `STYLE_FIELDS` order pick `options[digest[i] % len(options)]`, with the options in the order the `Literal` types list them in `contracts.py`. `mood` is three different words from `["calm", "precise", "quiet", "bold", "warm", "sleek", "sculptural", "spare"]` chosen with digest bytes 5, 6 and 7, moving to the next word on a repeat. `technique` is `Demo analysis: no model looked at this image.`
- Role `art_director` returns a `DesignSpec`. Context keys, all optional: `brief`, `brand` (`name`, `audience`, `feel`, `rules`), `taste` (a `TasteProfile` as a dict), `liked_cards` (a list of `{id, label, card}`), `previous_spec` (a `DesignSpec` dict or null) and `comment`.
  - New post, when there is no `previous_spec`:
    - `mode`: the more common of `dark` and `light` in `taste["liked"]["background"]`; a tie or no likes gives `dark`.
    - `layout`: `hero`.
    - `headline`: the brief stripped, first letter upper-cased, cut at a word boundary to at most 60 characters, trailing punctuation removed, then a full stop. An empty brief gives `A new post.`
    - `subline`: `A demo post written without a model.`
    - `photo_prompt`: `Minimal premium studio photograph for: {brief}. No text, no logos.`
    - `caption`: the headline, a space, then `This caption was written in demo mode.`
    - `hashtags`: one tag, `#` plus the brand name with everything but letters and digits removed, or none when there is no brand name.
    - `reference_ids`: the ids of the first two liked cards.
    - `assumptions`: `["Demo mode: a fake model wrote this post."]`
    - `reason_layout`: `Chose {mode} because {count} of {liked_count} liked references use a {mode} background.` when there are likes, otherwise `No likes yet, so the default dark layout was used.`
    - `reason_references`: `Drew on the first two liked references.` or `No liked references yet.`
    - `reason_words`: `The headline repeats the brief, because demo mode cannot write copy.`
  - Revision, when `previous_spec` is present: start from it and set `reuse_photo` true. Then, matching the comment without regard to case: "light" sets mode light; "dark" sets mode dark; "shorter" makes the headline the first four words of the previous one, trailing punctuation removed, plus a full stop; "no photo" or "type only" sets layout `type_only` and an empty photo prompt; "new photo" sets `reuse_photo` false. Append the assumption `Revised after the comment: {comment}`.
- An unknown role or missing context returns `{"error": "FakeLlm received no context"}`. That fails validation downstream, which is intended.

**get_llm and describe_llm**
- In demo mode `get_llm` returns `FakeLlm()`. Otherwise it returns ADK's Gemini model for `settings.gemini_model` with retries on 429, 500, 502, 503 and 504: three attempts, starting at 2 seconds, capped at 30. Find the exact class and option names by inspecting the installed packages in the container, for example `python -c "import inspect, google.genai.types as t; print(inspect.signature(t.HttpRetryOptions))"`, and use what is there. The SDK reads the key from the `GOOGLE_API_KEY` environment variable. Never pass it around or log it.
- `describe_llm` returns `fake` for the fake and `gemini:{model}` otherwise.

**Photo providers**
- `FakePhotoProvider.generate` draws, with Pillow, a dark vertical gradient with one soft blurred lighter ellipse near the centre. Colours come from SHA-256 of the prompt, so one prompt always gives the same image. The image is exactly the requested size, saved as PNG at `out_path` with parent folders created. It returns `PhotoResult(path=str(out_path), provider="fake", prompt=prompt)`.
- `CloudflarePhotoProvider.generate` posts to `https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}` with the header `Authorization: Bearer {api_token}`. Before coding, read the model's page, https://developers.cloudflare.com/workers-ai/models/flux-2-klein-4b/ (also try adding `index.md`), and follow its request and response format. On 2026-10-05 the page described a multipart form request with fields such as `prompt`, `width` and `height`, and a JSON response whose `result.image` is a Base64 string. If the page says otherwise, follow the page and say so in your report. Round width and height to what the page allows. Add `No text, no logos, no watermark.` to the prompt when it does not already say so. Decode the image, open it with Pillow and save it as PNG at `out_path`. The provider string is `cloudflare:` plus the last part of the model id.
- Cloudflare failures raise `PhotoUnavailable` with these messages: missing account id or token, `Cloudflare credentials are not set.`; 401 or 403, `Cloudflare rejected the credentials.`; 429, `Cloudflare's free daily allowance is used up.`; any other error status, `Cloudflare returned an error ({status}).` followed by the first error message in the body when there is one; a timeout or network error, `Cloudflare did not respond in time.`; a success without an image, `Cloudflare returned no image.` No message may contain the token or the account id.
- `get_photo_provider` follows `settings.photo_mode`: `cloudflare` gives the Cloudflare provider built from the settings, `fake` gives the fake, `none` gives `None`.

**Tests to write first, then make pass**

`tests/test_models.py`
- `test_context_round_trip`: includes non-ASCII text and braces in values.
- `test_read_context_without_block`
- `test_fake_analyst_is_deterministic_and_valid`
- `test_fake_art_director_follows_taste`: dark majority, light majority, no likes.
- `test_fake_art_director_headline_rules`: a long brief and an empty brief.
- `test_fake_art_director_revision_rules`: one case per keyword.
- `test_fake_unknown_role_returns_error_json`
- `test_get_llm_in_demo_mode_is_fake`
- `test_get_llm_with_a_key_is_gemini`: construction only, no call; `describe_llm` gives `gemini:gemini-3.5-flash-lite`. Set a dummy `GOOGLE_API_KEY` with `monkeypatch` if construction needs one.
- `test_fake_runs_inside_an_adk_workflow`: a `Workflow` holding one `LlmAgent` with `FakeLlm`, `output_schema=DesignSpec` and `output_key="design_spec"` leaves a valid spec in session state. Pattern:

```python
from google.adk import Runner, Workflow
from google.adk.agents import LlmAgent
from google.adk.sessions import InMemorySessionService
from google.genai import types

agent = LlmAgent(name="art_director", model=FakeLlm(),
                 instruction=lambda ctx: wrap_context(ROLE_ART_DIRECTOR, {"brief": "Free consultation week"}),
                 output_schema=DesignSpec, output_key="design_spec")
workflow = Workflow(name="t", edges=[("START", agent)])
sessions = InMemorySessionService()
runner = Runner(node=workflow, app_name="studio", session_service=sessions)
await sessions.create_session(app_name="studio", user_id="u", session_id="s", state={})
message = types.Content(role="user", parts=[types.Part(text="Free consultation week")])
async for _ in runner.run_async(user_id="u", session_id="s", new_message=message):
    pass
state = (await sessions.get_session(app_name="studio", user_id="u", session_id="s")).state
```

`tests/test_photos.py`
- `test_fake_photo_size_and_determinism`
- `test_cloudflare_success_writes_an_image`: an `httpx.MockTransport` returns the documented success body with a Base64 PNG made by Pillow; the file is an image, the provider string is right, and the request carried the bearer header and the prompt.
- `test_cloudflare_without_credentials`
- `test_cloudflare_rejected_credentials`
- `test_cloudflare_allowance_used_up`
- `test_cloudflare_error_with_message`
- `test_cloudflare_timeout`
- `test_cloudflare_no_image`
- `test_cloudflare_messages_never_hold_the_token`
- `test_factory_follows_photo_mode`

**Report:** the test command and output, and what the Cloudflare page said about the request and response format.

---

### Task 4: Library: sources, import and the analysis job

**Files**
- Create: `studio/library/sources.py`, `studio/library/fetch.py`, `studio/library/importer.py`, `studio/library/analysis.py`, `tests/test_sources.py`, `tests/test_importer.py`, `tests/test_analysis_job.py`
- Read first: `studio/library/base.py`, `studio/contracts.py` (`Reference`, `StyleCard`), `studio/store.py`, `tests/conftest.py`

**Interfaces this task consumes** (from Task 1, already in the repo): `Store.references_dir`, `Store.relative`, `Store.upsert_reference`, `Store.get_reference`, `Store.find_reference_by_hash`, `Store.list_references`, `Store.set_style_card`, `Store.set_reference_status`.

**Interfaces this task produces**

```python
# studio/library/sources.py
class BoardHtmlSource:                 # name = "board"
    def __init__(self, html_path: Path) -> None: ...
    def items(self) -> list[SourceItem]: ...
class FolderSource:                    # name = "folder"
    def __init__(self, folder: Path) -> None: ...
    def items(self) -> list[SourceItem]: ...

# studio/library/fetch.py
class HttpImageFetcher:
    def __init__(self, *, client: httpx.AsyncClient | None = None,
                 max_bytes: int = 15_000_000, timeout: float = 20.0) -> None: ...
    async def fetch(self, item: SourceItem) -> FetchedImage: ...
    async def aclose(self) -> None: ...

# studio/library/importer.py
class SkippedItem(BaseModel):
    label: str
    source_url: str | None
    reason: str
class ImportSummary(BaseModel):
    added: int = 0
    already_in_library: int = 0
    duplicates: int = 0
    unavailable: list[SkippedItem] = []
def reference_id(item: SourceItem) -> str: ...
async def import_references(source: ReferenceSource, store: Store, fetcher: ImageFetcher, *,
                            concurrency: int = 6) -> ImportSummary: ...

# studio/library/analysis.py
Analyse = Callable[[Reference, bytes, str], Awaitable[StyleCard]]
class AnalysisProgress(BaseModel):
    total: int
    analysed: int
    pending: int
    failed: int
    running: bool
class AnalysisJob:
    def __init__(self, store: Store, analyse: Analyse, *, per_minute: int = 10,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None: ...
    def start(self) -> bool: ...
    async def run_once(self) -> AnalysisProgress: ...
    def progress(self) -> AnalysisProgress: ...
```

**Behaviour**
- `BoardHtmlSource` parses with BeautifulSoup's `html.parser`. It returns one item per `<img>` whose `src` starts with `http://` or `https://`, in document order, keeping the first of any repeated address.
  - `source_url` is the `src` with HTML entities decoded.
  - `label` is the text of the image's card with the text of any `<a>` and `<button>` inside it removed and whitespace collapsed. The card is the nearest ancestor that is an `<article>`, `<figure>` or `<li>`, or failing that the nearest ancestor `<div>` holding exactly one such image. An empty label falls back to the image's `alt`.
  - `category` is the text of the nearest `<h3>` before the image, prefixed by the nearest `<h2>` before it and ` / ` when there is one. Use the heading element's own text only, not sibling counters.
  - Images without an http address, such as a lightbox placeholder, are skipped.
  - The real file is `brands/hybridge/inspiration-board.html`. It is one long file: search it to learn its structure, do not read it whole. On it the source returns exactly 60 items, each with a label and a category.
- `FolderSource` returns every file directly in the folder ending in `.png`, `.jpg`, `.jpeg`, `.webp` or `.gif` in any case, sorted by name, with `local_path` absolute, the label set to the file stem and an empty category. A missing folder gives an empty list.
- `HttpImageFetcher.fetch`:
  - With `local_path` it reads the file; a missing file raises `FetchError("File not found.")`.
  - Otherwise it GETs `source_url`, following redirects, with a browser-like User-Agent and the timeout. A timeout or network error raises `FetchError("The image host did not respond.")`. A status of 400 or more raises `FetchError("The image host returned {status}.")`.
  - A body over `max_bytes`, by `Content-Length` or by actual size, raises `FetchError("The image is larger than {n} MB.")` with n taken from `max_bytes`.
  - It checks the bytes are an image with Pillow's `Image.open(...).verify()`; failure raises `FetchError("The link did not return an image.")`.
  - `mime_type` comes from the format Pillow detects: JPEG, PNG, WEBP or GIF. Anything else raises `FetchError("The image format is not supported.")`.
  - It creates its own client when none is given, and `aclose()` closes only a client it created.
- `reference_id(item)` is the first 16 hex characters of SHA-256 of `source_url`, or of `local_path` when there is no address.
- `import_references` fetches with at most `concurrency` requests in flight, then applies the results in item order so the outcome does not depend on timing:
  - A reference with that id already stored as `pending`, `analysed` or `failed` counts in `already_in_library` and is not fetched.
  - A failed fetch stores `Reference(status="unavailable", error=reason)` with the item's source, address, label and category, and adds a `SkippedItem`. An existing `unavailable` reference is tried again.
  - A fetched image whose SHA-256 matches another stored reference counts in `duplicates` and is not stored.
  - Otherwise the bytes are written to `store.references_dir / "{id}.{ext}"` (`jpg`, `png`, `webp` or `gif`) and a `pending` reference is stored with `image_path=store.relative(path)` and the content hash. It counts in `added`.
- `AnalysisJob.run_once` takes `store.list_references(status="pending")` and, for each, reads the image bytes, takes the mime type from the file suffix, awaits `analyse(ref, data, mime)` and calls `store.set_style_card`. Any exception marks that reference `failed` with the first 300 characters of the message and moves on. A missing image file marks it `failed` with `The cached image is missing.` Between calls it awaits `sleep(60 / per_minute)`, not after the last one.
- `start()` schedules `run_once()` as an asyncio task and returns true, or returns false when one is already running. `progress()` counts from the store: `total` is every reference except `unavailable`, and `running` says whether a task is in flight.

**Tests to write first, then make pass.** No network: use a fake fetcher class and `httpx.MockTransport`.

`tests/test_sources.py`
- `test_board_items_from_inline_html`: two categories, one `<article>` card and one `<figure>` card, a lightbox image without `src`, a repeated address, and `&amp;` in an address.
- `test_real_board_has_sixty_labelled_items`: exactly 60, every label and category non-empty, the first label contains `Apple MacBook Pro`, and some category contains `Radical simplicity`.
- `test_folder_source_lists_only_images_sorted`
- `test_folder_source_missing_folder`

`tests/test_importer.py`
- `test_import_counts_and_files`: added, duplicates and unavailable in one run, with files on disk under the right extensions.
- `test_reimport_is_idempotent`
- `test_unavailable_is_stored_and_retried`
- `test_reference_id_is_stable`
- `test_fetcher_success`
- `test_fetcher_not_found`
- `test_fetcher_timeout`
- `test_fetcher_rejects_non_image`: an HTML body with status 200.
- `test_fetcher_rejects_oversized`: `max_bytes=100`.
- `test_fetcher_reads_local_file`
- `test_fetcher_rejects_unsupported_format`: BMP bytes.

`tests/test_analysis_job.py`
- `test_run_once_analyses_all_pending`: a stub `analyse` and a recording `sleep` called n minus 1 times with `60 / per_minute`.
- `test_one_failure_does_not_stop_the_rest`
- `test_missing_image_file_is_marked_failed`
- `test_progress_counts`
- `test_start_twice_returns_false`

**Report:** the test command and output.

---

### Task 5: Workflows on ADK: analyse, create and revise

**Files**
- Create: `studio/workflows/agents.py`, `studio/workflows/steps.py`, `studio/workflows/analyse.py`, `studio/workflows/create.py`, `tests/test_agents.py`, `tests/test_workflows.py`
- Replace the empty `studio/workflows/__init__.py` so it exports `Deps`, `analyse_reference`, `run_create`, `run_revise`
- Read first: `studio/contracts.py`, `studio/store.py`, `studio/models.py`, `studio/photos/base.py`, `studio/photos/fake.py`, `studio/render/renderer.py`, `studio/library/taste.py`, `tests/conftest.py`

**Interfaces this task consumes** (already in the repo)
- `Store`: `list_references`, `get_post`, `list_versions`, `save_post`, `start_step`, `end_step`, `finish_run`, `photos_dir`, `posts_dir`, `relative`, `media_path`
- `Renderer.render(spec, kit, photo_path, out_path) -> RenderReport`
- `PhotoProvider.generate(prompt, width, height, out_path) -> PhotoResult` and `PhotoUnavailable`
- `wrap_context`, `ROLE_ANALYST`, `ROLE_ART_DIRECTOR`, `describe_llm`, `FakeLlm` from `studio.models`
- `build_taste_profile(references) -> TasteProfile`

**Interfaces this task produces**

```python
# studio/workflows/__init__.py
@dataclass
class Deps:
    settings: Settings
    store: Store
    kit: BrandKit
    llm: BaseLlm
    photo_provider: PhotoProvider | None
    renderer: Renderer

async def analyse_reference(llm: BaseLlm, image: bytes, mime_type: str, *, label: str = "") -> StyleCard: ...
async def run_create(deps: Deps, run: Run) -> Post | None: ...
async def run_revise(deps: Deps, run: Run) -> Post | None: ...
```

**ADK 2.x patterns, each proven on google-adk 2.11.0 on 2026-10-05**

```python
from google.adk import Context, Event, Runner, Workflow
from google.adk.agents import LlmAgent
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import RetryConfig, node
from google.genai import types

# 1. A workflow is a list of edges. Plain functions and agents are both steps.
workflow = Workflow(name="create_post", edges=[("START", load_context, art_director, get_photo)])

# 2. A function step. `ctx.state` is shared session state. Any other parameter is bound from
#    state by name. Its return value becomes the next step's input.
def load_context(ctx: Context):
    ctx.state["art_director_context"] = {"brief": "..."}
    return "the brief text"          # an agent placed next receives this as its user message

def get_photo(ctx: Context, design_spec: dict):      # design_spec is read from state
    return Event(output={"photo": "p.png"}, state={"photo_path": "p.png"})

# 3. An agent step that returns typed JSON. The instruction is a function of the context,
#    because an agent inside a workflow runs as a single turn and sees no earlier conversation.
art_director = LlmAgent(
    name="art_director", model=llm,
    instruction=lambda ctx: "prompt text built from ctx.state",
    output_schema=DesignSpec,        # the reply must be JSON for this schema
    output_key="design_spec",        # the parsed dict is written to state["design_spec"]
)

# 4. Running it. State must hold plain JSON data only, never objects.
sessions = InMemorySessionService()
runner = Runner(node=workflow, app_name="studio", session_service=sessions)
await sessions.create_session(app_name="studio", user_id="studio", session_id=run_id, state={})
message = types.Content(role="user", parts=[types.Part(text="the brief")])
async for event in runner.run_async(user_id="studio", session_id=run_id, new_message=message):
    pass   # event.node_info.path names the step, event.output holds its output
state = (await sessions.get_session(app_name="studio", user_id="studio", session_id=run_id)).state

# 5. When the agent is the first step, the user message reaches it whole, image parts included:
image_message = types.Content(role="user", parts=[
    types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
    types.Part(text="Describe this reference."),
])

# 6. Failure. A step that raises ends the run: `runner.run_async` re-raises the original
#    exception and later steps do not run. A reply that does not fit `output_schema` raises
#    pydantic's ValidationError the same way.

# 7. Built-in retry of a step, with no feedback to the model:
analyst_step = node(analyst, retry_config=RetryConfig(max_attempts=2, initial_delay=0.5, jitter=0.0))

# 8. Retry driven by code, feeding the error back. The calling step needs rerun_on_resume=True.
#    A failed child raises google.adk.workflow._errors.DynamicNodeFailError; the original
#    exception is attached to it (check the attribute name in the installed source).
@node(rerun_on_resume=True)
async def art_director_step(ctx: Context, node_input: str):
    for attempt in (1, 2):
        try:
            spec = await ctx.run_node(art_director, node_input=node_input)   # returns the parsed dict
            return Event(output=spec, state={"design_spec": spec, "attempts": attempt})
        except Exception as error:
            ctx.state["retry_note"] = "..."      # the instruction function reads this next time
    raise ValueError("...")
```

Step functions reach `deps`, `run` and the step recorder through a closure, so build the workflow once per run with a factory such as `build_post_workflow(deps, run, recorder)`. Do not put those objects in state.

**Agents** (`agents.py`)
- `build_analyst(llm, label="") -> LlmAgent`: name `analyst`, `output_schema=StyleCard`, `output_key="style_card"`. Its instruction is the analyst prompt followed by `wrap_context(ROLE_ANALYST, {"label": label})`. The prompt says: describe one advertising design for its visual style only, not the product; judge the image as a whole; fill every field with one of the allowed values; `mood` is three single words; `technique` is one sentence on what makes the design work.
- `build_art_director(llm) -> LlmAgent`: name `art_director`, `output_schema=DesignSpec`, `output_key="design_spec"`. Its instruction function reads `ctx.state["art_director_context"]` and returns the prompt, then `wrap_context(ROLE_ART_DIRECTOR, context)`, then, when state holds `retry_note`, the line `Your previous answer was rejected: {retry_note} Return valid JSON for the schema.` The brief arrives as the user message.
- The art director prompt, in plain specific sentences: you are the art director for the brand named in the context; decide one social post from the brief; follow the brand's feel and rules; follow the designer's taste by preferring what the liked references share and avoiding what the disliked ones share; choose the layout `hero` (one photo above a headline and subline) or `type_only` (no photo); choose `dark` or `light`; write a headline of at most 8 words in sentence case and a subline of at most 18 words; write `photo_prompt` describing one photograph with a single clear subject, studio quality and plenty of empty space, with no text, logos or watermarks, and leave it empty for `type_only`; write a caption of one to three sentences for the audience and up to five hashtags; list in `reference_ids` up to three ids of the liked references you drew on; put in `assumptions` what you assumed where the brief was silent; give one sentence each for `reason_layout`, `reason_references` and `reason_words`; never promise medical outcomes or guarantees; and when `previous_spec` and `comment` are present you are revising, so change only what the comment asks, keep the rest, and set `reuse_photo` true unless the comment asks for a different photo.
- The context dict has the keys `brief`, `brand` (`name`, `audience`, `feel`, `rules`), `taste` (the `TasteProfile` as a dict), `liked_cards` (at most 12 of `{id, label, card}`, liked and analysed, in library order), `previous_spec` (a dict or `None`) and `comment`.

**Step recorder** (`steps.py`)

```python
class StepInfo:            # mutable; the step body fills it in
    provider: str = ""
    attempts: int = 1
    note: str = ""

class StepRecorder:
    def __init__(self, store: Store, run_id: str) -> None: ...
    def step(self, name: str) -> AbstractAsyncContextManager[StepInfo]: ...
```

Entering calls `store.start_step`. A normal exit calls `end_step("succeeded", ...)` with the info. An exception calls `end_step("failed", ..., error=<readable message>)` and re-raises.

**Analyse** (`analyse.py`). `analyse_reference` runs a workflow whose only step is the analyst with the built-in retry (pattern 7), sends the image message (pattern 5), reads `style_card` from state and returns it as a `StyleCard`. It raises on failure; the analysis job records that.

**Create and revise** (`create.py`). One workflow serves both kinds, with the steps `load_context`, `art_director`, `get_photo`, `render`, `save_post` in that order. Each step body runs inside `recorder.step(<name>)`.
- `load_context`: build the taste profile from `store.list_references()` and the liked cards. For a revision load `store.get_post(run.parent_post_id)`; when it is missing fail with `The post to revise was not found.`, and otherwise use that post's brief. Put the context dict in state as `art_director_context`. Return the brief text. Note: `{liked} liked and {disliked} disliked references.`
- `art_director` (pattern 8): up to two attempts. After a failure set `retry_note` to the first validation message, at most 200 characters. After two failures raise `SpecInvalid("The model's answer did not match the design spec after 2 attempts.")`, a `ValueError` subclass defined in this module. Then clean the spec:
  - strip the headline, subline and caption; an empty headline counts as a failed attempt;
  - drop `reference_ids` that are not ids of the liked cards;
  - hashtags: strip, remove inner spaces, add a leading `#` when missing, drop empties, keep at most 8;
  - `hero` with an empty `photo_prompt` becomes `type_only`, with the post note `The spec asked for a photo layout without describing a photo, so the type-only layout was used.`;
  - `type_only` always has an empty `photo_prompt`.
  Record `provider=describe_llm(deps.llm)`, the attempts, and `note=spec.reason_layout`.
- `get_photo`, which always succeeds as a step:
  - `type_only`: note `This layout has no photo.`
  - a revision whose spec has `reuse_photo` true, when the previous post has a photo file that exists: reuse it, note `Reused the previous photo.`
  - no provider configured: switch the spec to `type_only`, post note `No photo provider is configured, so the type-only layout was used.`
  - otherwise `await provider.generate(spec.photo_prompt, kit.post_size.width, kit.post_size.height, store.photos_dir / f"{run.id}.png")`, with the provider's string as the step's provider;
  - `PhotoUnavailable` with a reason: switch to `type_only`, post note `No photo was available ({reason}) so the type-only layout was used.`; any other exception is handled the same with the reason `the photo service failed`.
- `render`: `await renderer.render(spec, kit, photo_path, store.posts_dir / f"{post_id}.png")`. The step note is the adjustments joined with `; `, or `Fits without changes.` When `fits` is false add the post note `The text does not fit at the smallest size. Ask for shorter copy.`
- `save_post`: build the `Post`. A new post is version 1 with `root_post_id` equal to its own id. A revision is one more than the highest version under the previous post's root, with that root. Store paths with `store.relative(...)`. Save it.
- `run_create` and `run_revise` build the recorder and the workflow, run them, then call `store.finish_run(run.id, "succeeded", post_id=post.id)` and return the post. On any exception they call `store.finish_run(run.id, "failed", error=<message>)` and return `None`; they never raise. The message is `str(error)` for `ValueError` and `PhotoUnavailable`, and otherwise the exception type and text cut to 300 characters.
- Every run has its own session, with the run id as the session id.

**Tests to write first, then make pass.** Use `FakeLlm`, `FakePhotoProvider` and the real `Renderer` started once for the module. For bad-JSON cases write a small scripted model in the test file that returns a list of canned replies.

`tests/test_agents.py`
- `test_art_director_instruction_holds_the_context_block`: the instruction text contains `wrap_context(...)` of the context, including the taste summary and a liked card id.
- `test_art_director_instruction_adds_the_retry_note`
- `test_analyst_instruction_holds_the_label`

`tests/test_workflows.py`
- `test_create_makes_a_post`: the post is saved, its image file is 1080 by 1350, the run succeeded, and the events are the five step names in order, all succeeded.
- `test_no_photo_provider_gives_a_type_only_post`: layout `type_only`, a note starting `No photo provider`, run succeeded.
- `test_photo_unavailable_gives_a_type_only_post`: a provider raising `PhotoUnavailable("Cloudflare's free daily allowance is used up.")`; the post note includes that reason.
- `test_bad_json_once_is_retried`: `attempts` is 2 on the `art_director` step and the run succeeded.
- `test_bad_json_twice_fails_cleanly`: returns `None`; the run is `failed` with the message above; the `art_director` step is `failed`; no later step was recorded.
- `test_revise_makes_version_two`: comment `make it light`; version 2, mode `light`, the same root and the same `photo_path` as version 1.
- `test_revise_with_new_photo_comment_makes_a_new_photo`
- `test_revise_missing_parent_fails_cleanly`
- `test_likes_change_the_post`: liked dark cards give mode `dark`; with the likes switched to light cards the next post is `light`.
- `test_unknown_reference_ids_are_dropped`
- `test_hero_without_photo_prompt_becomes_type_only`
- `test_hashtags_are_cleaned`
- `test_two_runs_at_once`: `asyncio.gather` of two creates gives two different posts, both succeeded.
- `test_analyse_reference_returns_a_style_card`: the same bytes give the same card.

**Report:** the test command and output, and the attribute that holds the original exception on `DynamicNodeFailError`.

---

### Task 6: Web app: pages, routes and wiring

**Files**
- Create: `studio/main.py`, `studio/web/routes.py`, `studio/web/jobs.py`, `studio/web/templates/base.html`, `library.html`, `studio.html`, `post.html`, `run.html`, `runs.html` in that templates folder, `studio/web/static/app.css`, `studio/web/static/app.js`, `tests/test_web.py`
- Read first: `studio/contracts.py`, `studio/config.py`, `studio/store.py`, `studio/workflows/__init__.py`, `studio/library/importer.py`, `studio/library/analysis.py`, `studio/library/sources.py`, `studio/library/fetch.py`, `studio/library/taste.py`, `studio/models.py`, `studio/photos/__init__.py`, `studio/render/renderer.py`, `studio/brand.py`

**Interfaces this task consumes** (already in the repo)
- `Settings.from_env()`, `load_brand_kit(brands_dir, brand_id)`
- `Store(db_path, data_dir)` and its methods
- `get_llm(settings)`, `describe_llm(llm)`, `get_photo_provider(settings)`
- `Renderer(work_dir)` with `start()` and `stop()`
- `Deps`, `analyse_reference`, `run_create`, `run_revise`
- `BoardHtmlSource`, `FolderSource`, `HttpImageFetcher`, `import_references`, `AnalysisJob`, `build_taste_profile`

**Interfaces this task produces**

```python
# studio/main.py
def create_app(settings: Settings | None = None, *, fetcher: ImageFetcher | None = None) -> FastAPI: ...
app = create_app()
```

**Wiring**
- `create_app` does nothing heavy at import time. Its lifespan builds, in order: the store (`init()`, then `mark_interrupted_runs()`), the brand kit, the model, the photo provider, the renderer (`start()`), the `Deps`, and an `AnalysisJob` whose `analyse` calls `analyse_reference(deps.llm, data, mime, label=ref.label)` at `settings.analyse_per_minute`. On shutdown it stops the renderer and closes the fetcher. In demo mode the analysis job runs with no pause between references.
- The analysis job is started after every import, and once at start-up when pending references exist.
- `jobs.py` starts `run_create` and `run_revise` as asyncio tasks and keeps a reference to each until it finishes, so none is garbage-collected.
- A brand kit that fails to load stops start-up with the loader's message.

**Routes**

| Method and path | Does |
|---|---|
| `GET /` | Redirects to `/studio` |
| `GET /studio` | Brief form and recent posts |
| `POST /runs` | Form field `brief`. Empty after stripping: status 400 and the Studio page with `Write a brief first.` Longer than 2,000 characters: cut to 2,000. Otherwise create a run, start it, redirect (303) to `/runs/{id}` |
| `GET /runs` | Recent runs |
| `GET /runs/{id}` | The run page: its steps, refreshing while it is running, with a link to the post when done |
| `GET /api/runs/{id}` | JSON: the run and its events |
| `GET /posts/{id}` | The post page |
| `POST /posts/{id}/revise` | Form field `comment`. Empty: status 400 with `Write a comment first.` Otherwise create a revise run and redirect to its run page |
| `POST /posts/{id}/approve` | Marks the version approved and redirects back to it |
| `GET /library` | Reference grid, taste panel, import buttons |
| `POST /library/import` | Form field `source`, `board` or `inbox`. Runs the import, starts the analysis job, and shows the Library page with the summary |
| `POST /api/references/{id}/choice` | JSON body `{"choice": "liked" | "disliked" | "none"}`. Returns the new taste profile |
| `GET /api/library/status` | JSON: analysis progress and the taste profile |
| `GET /media/{path}` | Serves a file from the data folder through `store.media_path`. An escaping path or a missing file is 404 |

Unknown run, post or reference ids are 404.

**Pages**
- All four pages share a header with the studio's name, the brand's name and links to Studio, Library and Runs. In demo mode a banner reads `Demo mode: no model key is set, so posts are written by a stand-in.` When no photo source is available, a second line reads `No photo source is set, so posts use the type-only layout.`
- Library: buttons `Import the brand's inspiration board` (shown when the kit has one) and `Import images from the inbox folder` (reads `store.inbox_dir`). After an import, a summary line with the counts and a collapsible list of skipped items with reasons. A taste panel shows the summary sentence and the liked and disliked counts. An analysis line shows `Analysed {a} of {t}` and updates while the job runs. The grid shows each reference's image, label and category with a tick and a cross; the current choice is visibly selected, pressing it again clears it, and the taste panel updates without a page reload. Filters: all, liked, disliked, not rated. It must be comfortable on a phone: one column, large tap targets.
- Studio: a text area for the brief with a `Generate` button, and a grid of recent posts linking to their pages.
- Run: the brief, the status, and one row per step with its name in plain words, status, duration, provider, attempts when more than one, note and error. While the run is `running` the page polls `/api/runs/{id}` about once a second and updates the rows; when it succeeds it shows a link to the post and moves there after a short pause. A failed run shows the error and a `Run again` button that posts the same brief.
- Post: the image; the caption with a copy button; the hashtags; any notes as warnings; `Why it looks like this` with the three reasons, the assumptions, and the references it drew on as thumbnails; a comment box with `Revise`; `Approve`; `Download PNG`; the other versions of the same post; and a link to the run.
- Step names in plain words: `load_context` is "Gather context", `art_director` is "Design the post", `get_photo` is "Get the photo", `render` is "Render", `save_post` is "Save".
- Look: a calm, neutral studio with generous spacing. It must not borrow the brand's colours or logo, because the studio serves any brand. No external fonts, scripts or stylesheets: everything is served from `static/`. If a frontend design skill is available to you, use it.

**Tests to write first, then make pass** (`tests/test_web.py`). Use `create_app(settings, fetcher=<fake fetcher>)` with Starlette's `TestClient` as a context manager so the lifespan runs. Poll `/api/runs/{id}` to wait for a run.
- `test_pages_load`: `/studio`, `/library`, `/runs` return 200; `/` redirects.
- `test_demo_banner_is_shown`
- `test_empty_brief_is_refused`: status 400 and the message.
- `test_long_brief_is_accepted`: 5,000 characters; the stored brief is 2,000.
- `test_generate_makes_a_post`: post a brief, follow the redirect, wait for success, then the post page is 200 and its image is served from `/media/` as `image/png`.
- `test_run_page_lists_steps_in_plain_words`
- `test_revise_makes_a_second_version`: both versions appear on the post page.
- `test_empty_comment_is_refused`
- `test_approve_marks_the_version`
- `test_import_board_adds_references`: the fake fetcher returns small generated images; the summary shows the count; the grid shows them.
- `test_choice_updates_the_taste`: after analysis, posting a choice returns a profile whose summary starts with `Liked 1:`.
- `test_choice_survives_restart`: a second app on the same settings shows the choice.
- `test_media_rejects_path_escape`: `/media/../../etc/passwd` and `/media/%2e%2e/%2e%2e/x` are 404.
- `test_unknown_ids_are_404`
- `test_interrupted_runs_are_marked_on_startup`

**Report:** the test command and output, and a screenshot path for each of the four pages if you can take them.
