# Design Studio: the code map

What is in the repository, file by file, and why each file exists. Read this before the
code. Line counts are from 2026-10-06 (about 11,000 lines of Python and JavaScript, 2,100 of
CSS and 1,000 of templates, plus 2,700 lines of tests).

## The stack

| Layer | Choice | Why this and not something else |
|---|---|---|
| Runtime | Python 3.12 inside Microsoft's Playwright image (`mcr.microsoft.com/playwright/python:v1.63.0-noble`) | The image already carries a headless Chromium that matches the Playwright version, so one container renders posts with no host install. The host needs only Docker. |
| Agent framework | Google ADK 2.11 (`Workflow` graphs of function steps, `LlmAgent` with a pydantic `output_schema`, one `Runner` per run) | The assessment prefers ADK. Function steps make the order of work deterministic and inspectable; the agents only answer typed questions. |
| Text and vision model | Gemini 3.5 Flash-Lite through Google AI Studio's free tier | Free, multimodal (it judges photos), fast. The free tier has no image generation, which is why photos come from elsewhere. |
| Photo generation | Cloudflare Workers AI, FLUX-2 klein 4B (free daily allowance), behind a provider interface; a deterministic fake for demo mode | Free and hosted; the interface lets another provider or a local model be swapped in. The fake lets the whole studio run with no key at all. |
| Rendering | Jinja2 HTML and CSS layouts captured by Chromium; Pillow for the contrast measurement and the logo knockout | The model never draws the post. HTML places the real logo, the kit's font files and the exact palette, so a post is on brand by construction and the text is measured, not hoped for. |
| Web | FastAPI with server-rendered Jinja2 pages, vanilla JavaScript, no build step | Every page works from its HTML alone; the script only adds polling and small touches. Nothing to compile. |
| Storage | SQLite from the standard library: one row per object with a JSON column; files under `data/` | Zero infrastructure, and every run, step, sample, version and post is a row that the pages can show. |
| Packaging | Docker Compose, one service, `docker compose up`; keys in `.env`; a brand is a folder under `brands/` | One command, no deployment, and another brand is a data change. |
| Tests | pytest and pytest-asyncio, 99 tests, run in the container | The suite covers the parts that do not need a model: the kit loader, the store, the renderer's pixels, the fakes, the sources and the web wiring. |

## How the pieces fit

A brief goes in on the Studio page and starts a **session**. The **prompt writer** agent turns the
brief, the brand kit and the designer's taste (what they liked in the library) into a concept, a
photo prompt and the words. A **photo provider** makes a round of photos; the **critic** agent
scores each one against the prompt and the brand's ideal example, and its **ranker** orders the
round. The designer reacts, edits the prompt or the words, and asks for another round, or picks a
photo. Rules in code **shortlist** the layouts that fit where the photo's subject sits, the
**renderer** draws each one as a candidate, and the designer picks one, which becomes the post with
its caption. In **auto mode** a **judge** agent takes the designer's place between rounds and a
**final checker** gives the finished post its last look, inside limits the code enforces. The
**editor** lets the designer arrange a post by hand over any photo; the same renderer draws it
inside the brand's guardrails. Every run records its steps, and every object is saved, so the
Runs and Session pages show how a post came to be.

The one rule behind the layout: **agents decide content, code decides what happens next.**

## Folder map

```
studio/            the application package
  config.py        settings from environment variables
  contracts.py     every typed object the parts pass around
  brand.py         reads a brand folder into a BrandKit
  main.py          builds the FastAPI app and wires every dependency
  models.py        the language-model gateway and the stand-in model
  store.py         SQLite storage and the data folders
  library/         gather and curate: reference sources, fetching, analysis, taste
  photos/          photo providers: the interface, Cloudflare, the fake
  render/          layouts, the renderer, the layout rules, the custom layout, the faces
  workflows/       the ADK workflows: the agents and every kind of run
  web/             routes, templates, styles and scripts
scripts/           the sample-output script and its briefs
samples/           the committed sample posts with their scores (made by the script)
brands/hybridge/   the brand kit: brand.yaml, logo, fonts, examples, inspiration board
tests/             the pytest suite
docs/              this map, the design specs and plans, the architecture diagrams
Dockerfile, docker-compose.yml, requirements*.txt, .env.example
```

## `studio/` top level

| File | Lines | What it holds | Why it exists |
|---|---|---|---|
| `config.py` | 68 | `Settings`: the data, database and brands paths, the brand id, the Google key and Gemini model name, the Cloudflare account, token and model, the photo provider choice (`auto`, `cloudflare`, `fake`, `none`), the analysis pace and the most photos a round may ask for. Two derived answers: `demo_mode` (no Google key, so the stand-ins run) and `photo_mode`. `Settings.from_env` is the only place the environment is read. | Everything else takes a `Settings` object, so tests build one by hand, no module reads `os.environ`, and the demo-versus-real decision is made once. |
| `contracts.py` | 622 | The pydantic models: `BrandKit` and its parts; references and taste (`StyleCard`, `Reference`, `TasteProfile`); layouts (`LayoutTemplate`, `Composition`, the editor's `Block`, `PhotoBox` and `CustomLayout`); the post (`DesignSpec`, `RenderReport`, `Post`); runs (`Run`, `RunEvent`); sessions (`StudioSession`, `PromptVersion`, `Sample`, `SampleReview`, `RoundRanking`, `LayoutCandidate`, `RoundFeedback`); auto mode (`AutoSettings`, `RoundJudgement`, `FinalReview`, `AutoState`). | The contract between agents, code, store and pages. Agents answer with these schemas, the store saves them as JSON, the pages read them. A change here is a change to every reader, which is the point. |
| `brand.py` | 92 | `load_brand_kit`: reads `brand.yaml`, validates it, checks that the logo, font, example and board files exist and that each mode's roles name a real palette colour. `logo_for_mode`: the white knockout of the lockup for dark backgrounds, cached under `data/work`. | A brand is a folder, and this is the only code that knows the folder's shape. A missing file fails at start-up with a plain message instead of in the middle of a run. |
| `main.py` | 126 | `create_app`: the FastAPI app whose lifespan builds the store, the brand kit, the model, the photo provider, the renderer (one shared Chromium), the paced analysis job and the background run registry, marks runs cut off by a restart as interrupted and frees their sessions, then mounts the static files and the router. | The composition root: every dependency is built once, here, and handed down as `Deps`. |
| `models.py` | 554 | The model gateway. `wrap_context` and `read_context` put a role and a JSON context into every agent instruction and read it back. `FakeLlm` answers each role deterministically (a style card from the image's hash, a prompt draft from the brief, a critic review, a ranking, a judgement, a final review). `banned_terms` extracts what a comment asks to remove. `get_llm` returns the fake in demo mode or ADK's Gemini with retry options. | Demo mode and the tests run exactly the same agent code as a live run; the real model is one object swap. The stand-in is deterministic so a demo run is repeatable. |
| `store.py` | 780 | `Store`: the SQLite tables (`refs`, `runs`, `run_events`, `posts`, `sessions`, `prompt_versions`, `samples`, `round_feedback`, `round_reviews`, `layout_candidates`), the data folders (references, photos, posts, work, inbox, samples, compositions, uploads), `media_path` which refuses any path that escapes the data folder, the delete rules for photos that share a file, the run tree (`list_runs_for_parent`), and the daily photo count. | Persistence with no infrastructure, and traceability: a run, its steps, each sample with its review, each prompt version and each candidate is a row the pages can show, and everything survives a restart. |

## `studio/library/`: gather and curate

| File | Lines | What it holds | Why it exists |
|---|---|---|---|
| `base.py` | 38 | `SourceItem`, `FetchedImage`, `FetchError`, and the `ReferenceSource` and `ImageFetcher` protocols. | The seam between discovering references, fetching them and storing them, so each part can be faked in tests. |
| `sources.py` | 134 | `BoardHtmlSource` reads the kit's `inspiration-board.html` and lists its image links with their labels and categories; `FolderSource` lists images the designer dropped into `data/inbox`. | References come only from permitted places: the brand's own board and the designer's own folder. No scraping. |
| `fetch.py` | 119 | `HttpImageFetcher`: downloads one image with httpx, checks it is an image with Pillow, reads local files the same way. | One place for the network, with the checks that keep a broken file out of the library. |
| `importer.py` | 130 | `import_references`: fetches each item once, deduplicates by content hash, records `pending`, `failed` or `unavailable`, and returns an import summary for the page. | The Gather step, idempotent: importing the board twice adds nothing. |
| `analysis.py` | 109 | `AnalysisJob`: a paced background job that asks the analyst agent to describe each pending reference as a style card, a few per minute, with progress the page polls. | The free tier's rate limit is respected and the designer can keep working while the library is analysed. |
| `taste.py` | 84 | `build_taste_profile`: counts the style-card fields among the liked and disliked references and writes a one-sentence summary. | The Curate step's output: taste is counts and a sentence the prompt writer reads, not an embedding. Simple, explainable, zero cost. |

## `studio/photos/`: where photos come from

| File | Lines | What it holds | Why it exists |
|---|---|---|---|
| `base.py` | 33 | The `PhotoProvider` protocol (`generate(prompt, width, height, out_path, seed)` returning a `PhotoResult`) and `PhotoUnavailable`. | Plug and play: any image service or local model that implements `generate` can be the studio's photographer. |
| `cloudflare.py` | 166 | The Workers AI FLUX-2 klein provider: multipart request, sizes rounded to multiples of 32, a 180-second timeout with one retry, and plain messages for bad credentials, the daily allowance and other errors. The docstring records what the model's page does and does not document. | The free hosted image model. The messages are what the designer sees on a failed sample. |
| `fake.py` | 102 | A deterministic provider that paints a soft shape from the prompt's hash. | Demo mode and the tests make "photos" at once and with no key. |
| `__init__.py` | 22 | `get_photo_provider`: the provider the settings ask for, or none. | The one place the provider choice is made. |

## `studio/render/`: drawing the post

| File | Lines | What it holds | Why it exists |
|---|---|---|---|
| `renderer.py` | 1084 | `Renderer`: one shared Chromium; `build_html` fills a layout template with the words, the brand's values (as CSS custom properties) and the composition (as classes); the fit script measures the text and the renderer steps the headline down until it fits; for words over a photo, Pillow measures the photo behind them and a shade is added when the contrast is under 4.5; the custom layout path applies the guardrails, draws the blocks and the photo box as placed, measures the contrast per text block and never steps or shades on its own. Returns a `RenderReport`. | Spec-then-render: the post is drawn from the real logo file, the kit's font files and the exact palette, so it is on brand by construction, and the report says what had to change and how readable the words are. |
| `compose.py` | 223 | The six templates and their defaults, `allowed_templates` from the kit, `shortlist` (the rules that turn the critic's subject position and calm areas into an ordered list of compositions), and `describe` (a composition in words). | Rules, not a model, choose layouts, so the choice is explainable and free. |
| `custom.py` | 337 | The editor's pure functions: `default_layout`, `blocks_for_template` (a template candidate's slots as blocks, so the editor starts from what the designer picked), `apply_guardrails` (words and logo inside the margin, the logo always present and never under 14%, shades and the photo box on the canvas, palette colours and kit faces only), `resolve_colour`. | The brand rules for hand-made layouts live in code the renderer runs, not only in the page's script. |
| `faces.py` | 82 | `STUDIO_FACES` (four open-licence variable fonts under `fonts/`), `available_faces` (the kit's family, the kit's extra faces, then the studio's when the kit allows them), `face_named`. | One source of truth for which typefaces the editor offers, the renderer embeds and the guardrail accepts. |
| `layouts/base.html`, `base.css` | 19, 122 | The page every template extends: the brand style block, the fit script, the shared text and logo rules, the shade gradient. | Shared rules in one place; no template names a brand, a colour or a typeface (a test checks). |
| `layouts/hero.html`, `full_bleed.html`, `split.html`, `corner.html`, `caption_strip.html`, `type_only.html` | 59 to 103 each | The six templates, each a small block of CSS and markup. | Each template owns its geometry; the composition's parameters arrive as classes. |
| `layouts/custom.html` | 58 | The editor's layout: the photo in its box, then shades, text blocks and the logo at the positions, sizes, faces and colours the context supplies. | What the designer sees on the canvas is what Chromium draws. |
| `fonts/` | 7 files | Playfair Display, Lora, Montserrat and Space Grotesk as variable fonts, with their OFL licences. | The studio's own choice of faces, offered only to a kit that opts in. |
| `__init__.py` | 32 | The package's exports. | One import line for the callers. |

## `studio/workflows/`: the agents and the runs

| File | Lines | What it holds | Why it exists |
|---|---|---|---|
| `agents.py` | 303 | The prompts and the `LlmAgent` builders: the analyst (one reference in, a style card out), the prompt writer (brief, taste, brand, feedback in; concept, photo prompt, words out), the critic (one photo in; scores, flags, subject position, calm areas out), the ranker (a round in; an order out), the judge (a round's reviews in; good enough or the changes to make), the final checker (the finished post in; ship, score, biggest flaw). Each instruction is a function that embeds the context block. | Every agent is a typed question with a typed answer. The split follows the designer's own steps: describe, write, judge, compare, decide, check. |
| `steps.py` | 86 | `StepRecorder`: an async context manager that writes a run event when a step starts and closes it with the outcome, the provider, the attempts, a note and a readable error. | The run page's rows come from here, so every step of every run is visible without reading code. |
| `shared.py` | 348 | What every run shares: `Deps`; `settle` (records how a run ended and never raises except to pass on a Stop); `run_workflow` (one ADK `Runner` and session per run); `update_session` (save on top of the latest saved state); `ask_prompt_writer` (one retry with the validation error fed back); `render_post`; `trim_prompt`; the hashtag, mention and image helpers. | The patterns the runs follow are written once, so a new run is a short file. |
| `analyse.py` | 66 | The one-step workflow that turns a reference image into a style card, with one retry on a bad answer. | Used by the library's analysis job. |
| `session.py` | 865 | The session runs: `run_draft` (the prompt writer, from a brief or from an archived photo), `run_samples` (generate a round, review each photo, rank the round, save; Stop keeps the photos already made), `run_revise_prompt` (rewrite the prompt from the designer's or the critic's feedback, with a guard that retries when a banned term survives), `pick_source_photo`. | The three stages between which the designer acts. Each is short and saves what it made, so a stop or a failure loses nothing finished. |
| `layouts.py` | 370 | `run_compose` (shortlist, render each candidate, save) and `run_finish` (copy the chosen candidate as the post, with the words of the current version, and finish the session). | The post is exactly the thumbnail the designer picked, and a candidate made with older words is refused. |
| `create.py` | 304 | `run_revise`: the comment on a post, classified by the prompt writer as about the words, the photo or both; words make the next version, a photo request goes to the session as feedback, with a code safety net for comments that read as photo requests. | One comment box on the post page, routed where it belongs. |
| `auto.py` | 673 | `run_auto`: plan, draft, the round loop (samples, judge, revise, within the rounds and photo limits and the stop score), compose, the final check (the next layout once when the first would not ship), finish, report. Each stage is a child run linked by `parent_run_id`; every decision is one line saved at once; the session goes back to manual however the run ends. | Auto mode as a bounded loop: code owns the limits, the critic owns the judgements, and the trace is the same runs the manual mode makes. |
| `__init__.py` | 22 | The run functions the web layer calls. | One import line for the routes. |

## `studio/web/`: the designer's pages

| File | Lines | What it holds | Why it exists |
|---|---|---|---|
| `routes.py` | 1549 | Every page and endpoint: the Studio page and session creation (manual or auto), the session page and its actions (draft again, generate, feedback, use a photo, more layouts, adjust, finish, stop), the editor page with preview and upload, the post page and its comment and approval, the archive with delete and reuse, the run pages, the library import and choices, the JSON endpoints the pages poll, the brand font and logo routes, and the media route with its path guard. Helpers build each page's context (the round groups, the diff of prompt versions, the layout part, the auto status, the child-run note). | The whole UI is here; handlers read their dependencies from `app.state` and start runs through `RunJobs`. |
| `jobs.py` | 37 | `RunJobs`: keeps each background run task by run id and cancels one on Stop. | A task nothing holds can be garbage-collected mid-run; this holds it, and Stop needs to find it. |
| `templates/base.html` | 54 | The page frame: header, navigation, the demo and no-photo banners, the theme script, the stylesheet and script tags. | Every page extends it. |
| `templates/studio.html` | 93 | The brief form with Manual or Auto and the three limits, recent sessions, recent posts. | The front door. |
| `templates/session.html` | 401 | The session: brief and concept, the editable prompt and words with the version diff, the upload form, the rounds with each sample's scores, flags, rank, reactions and comment, the layout candidates with Adjust, the auto decisions, the finished post with the final check. | The workroom; most of the designer's time is spent here. |
| `templates/editor.html` | 166 | The canvas, the panel (words, block controls, photo, mode, logo width) and the preview column; the page data as one JSON block. | The editor's HTML; its behaviour is in `editor.js`. |
| `templates/post.html` | 108 | The finished post: image, caption, hashtags, warnings, the reasons, the comment box, approval, versions, links to the session, the run and the editor. | The deliverable and its explanation. |
| `templates/run.html`, `runs.html` | 72, 24 | One run with its steps (label, status, timing, provider, note, error), its parent and child runs; the list of runs. | Visibility and debugging without reading code. |
| `templates/library.html` | 103 | Import buttons, the analysis progress, the taste summary, the reference grid with like and dislike. | Gather and Curate. |
| `templates/archive.html` | 84 | Every photo ever made, filtered, with delete, start a session and open in the editor. | Nothing is thrown away; photos are reused. |
| `static/app.css` | 2121 | The studio's own neutral chrome in light and dark, as tokens; the layouts of every page; the editor's canvas, handles and panel. | The tool never borrows the brand's colours, so the brand's output is the only saturated colour on the page. |
| `static/app.js` | 558 | Theme toggle, library choices and progress polling, run-page polling, session polling (status line, decisions, reload when a round lands or the run ends), reactions and comments, the adjust forms, the archive confirmations, the caption copy button. | The live touches; every page works without it. |
| `static/editor.js` | 1041 | The canvas editor: blocks and the photo as draggable, resizable boxes with handles, the margin and canvas rules mirrored from the renderer, the panel (font, weight, italic, size, alignment, colour, opacity, fit, pan, background, mode, logo width), keyboard nudging, the stale-preview rule, the preview request and the finish form. | The free arrangement the templates cannot give, inside the same guardrails. |

## `scripts/`, `samples/`, `brands/`, `tests/`, `docs/` and the root

| Path | What it holds | Why it exists |
|---|---|---|
| `scripts/make_samples.py` (467) | Runs the briefs in `scripts/sample-briefs.txt` through auto mode on the running studio, one after the other, reads each post's scores from the store and writes `samples/README.md` (the table and each post's caption, decisions and trace link), `samples/results.json` and the PNGs. | The committed sample output and the "how do you know it is good" answer in one command. |
| `samples/` | Six posts, their table and the JSON, made on 2026-10-06. | What the assessment asks to commit. |
| `brands/hybridge/` | `brand.yaml` (name, audience, feel, rules, palette, dark and light roles, typography, logos, post size, board), `logo/`, `fonts/` (Inter, OFL), `examples/` (four current posts and the ideal output), `inspiration-board.html`, the kit's `README.md`. | The brand as data. Another brand is another folder and `STUDIO_BRAND=<folder>` in `.env`. |
| `tests/` (12 files, 99 tests) | `conftest.py` (demo settings in a temporary folder, the Hybridge kit, a second made-up brand); `test_brand.py` (the loader and its errors); `test_store.py`; `test_models.py` (the context block and the deterministic fakes); `test_agents.py`; `test_photos.py` (the fake and the Cloudflare provider against a fake HTTP client); `test_sources.py`, `test_importer.py`, `test_analysis_job.py`, `test_taste.py` (gather and curate); `test_render.py` (pixels, sizes, fitting, the logo's proportions, the second brand's colours, no brand values in layouts); `test_workflows.py` (a session end to end on the fakes, the revision, the analyse workflow); `test_web.py` (pages, import, choices, media guard, 404s, restart handling). | The parts that must not break run without a key in the container. The model's judgement is not unit-tested; the sample table and the run traces are its evidence. |
| `docs/CODE-MAP.md` | This file. | A stranger's way in. |
| `docs/superpowers/specs/`, `docs/superpowers/plans/` | The design documents and implementation plans of v1, v2, v2.1 and v3, in order. | The record of what was decided, when, and what was rejected. |
| `docs/architecture/` | The Archify diagrams (HTML and JSON), to be made from the finished code. | The assessment asks for them. |
| `Dockerfile`, `docker-compose.yml` | The image (the Playwright base, the requirements, the code) and the one service with its mounts (the code, the brands, the data folder, the scripts and samples) and the named database volume. | One command to run. |
| `requirements.txt`, `requirements-dev.txt` | google-adk 2.11, playwright 1.63, fastapi, uvicorn, pydantic 2, jinja2, pillow, beautifulsoup4, httpx, pyyaml, python-multipart; pytest and pytest-asyncio. | Pinned enough to reproduce. |
| `.env.example`, `.gitignore`, `.dockerignore` | The keys to fill in and what never enters git or the image (`.env`, `data/`). | No secrets in code or history. |

## Where each assessment question is answered

| They will look at | Start here |
|---|---|
| Agent design | `workflows/agents.py` (what each agent owns), `contracts.py` (what they hand each other) |
| Orchestration | `workflows/session.py`, `layouts.py`, `auto.py` (the runs), `shared.py` (`run_workflow`, `settle`) |
| Failure and fallbacks | `photos/cloudflare.py` (messages, retry), `workflows/steps.py` and `shared.py` (recorded failures, Stop), `auto.py` (the limits and the best-effort rules), `main.py` (restart handling) |
| Calls to models and services | `models.py` (the gateway, the fake, the retry options), `photos/`, `library/fetch.py` |
| Visibility and debugging | `store.py` (every step is a row), `web/templates/run.html`, `session.html` (decisions, versions, diffs) |
| Explaining decisions | the reasons on the post page, the decision lines of auto mode, `docs/superpowers/specs/` |
| Output | `render/` (on brand by construction), `samples/README.md` (the scores) |
| The whole problem | `docs/superpowers/specs/` (decisions and rejected options), DESIGN.md (to come) |
| Packaging | `docker-compose.yml`, `Dockerfile`, `brands/`, `.env.example` |
| Engineering | this map, `tests/`, the docstrings at the top of every module |
