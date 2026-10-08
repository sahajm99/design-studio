# Design notes

Design Studio turns a short brief and a brand kit into a finished social post: a 1080 by 1350 PNG
with the brand's logo, fonts and colours, plus a caption and hashtags. This document explains how
it is built, what was decided and why, what was rejected, where it falls short, and what another
week would buy. The README covers how to run it; [CODE-MAP.md](docs/CODE-MAP.md) describes every
file; [MODELS-AND-COSTS.md](docs/MODELS-AND-COSTS.md) holds the model landscape and the numbers;
[REVIEW-POINTS.md](docs/REVIEW-POINTS.md) is the open backlog with its history.

One rule shaped everything: **agents decide content, code decides what happens next.** Every agent
answers one question with one typed record. Every loop limit, stop rule, layout rule and guardrail
lives in code, where it can be read, tested and explained.

## 1. The shape of the studio

The assessment sketched gather, curate, create. The studio maps onto it like this.

| Sketch | In the studio |
| --- | --- |
| Gather | The Library: the brand's inspiration board, uploads, pasted links and an inbox folder. The scout, which searches the web for how the brand's field presents the brief's subject today. |
| Curate | Liked and disliked marks on references, summarised into a taste profile. A quality bar, the one image the critic holds every photo to. Up to six reference images on a session, with notes, for how this one post should look. Three directions from the research, one chosen. |
| Create | A session: prompt, photos, critique, feedback, layouts, final check, post. An editor for the layout, with the designer's own text and images and an Ask mode that applies a typed request as edits. An archive of posts. |

Two ways to run a session:

- **Manual mode.** The designer is the router. They read the directions, edit the prompt, react to
  samples, write feedback, pick the photo and the layout. Every step is a button.
- **Auto mode.** A bounded loop does the same work on its own: plan, research, draft, rounds of
  photos judged by the critic, compose, final check, finish. Three limits are set per session:
  rounds (default 3), photos (default 6) and the stop score (default 4.2 of 5). When the loop
  stops for any reason, the session drops back to manual mode with the best result so far, so
  the designer can continue by hand.

## 2. The agents

Nine agents, all on Gemini 3.5 Flash-Lite through the Google ADK, each with a pydantic output
schema so a malformed answer is rejected before it reaches the code. In demo mode a stand-in
answers for each role, so the whole studio runs with no keys.

| Agent | Owns | Reads | Returns |
| --- | --- | --- | --- |
| Analyst | A style card for one reference image | The image | Mood, technique, palette and layout traits from fixed vocabularies |
| Scout | What to search, then the research | The brief, the brand's field and policy; then the numbered results | Two or three queries; a 250 to 400 word report citing results as [n] |
| Direction writer | Three directions and a recommendation | The brief, the research, the taste summary, recent posts | Title, format, subject, framing, mood, layout, headline idea, facts with sources |
| Prompt writer | The photo prompt and the words | The brief, the brand, the taste, the chosen direction, feedback | Concept, photo prompt, layout, mode, headline, subline, caption, hashtags, reasons |
| Critic | One photo's verdict | The photo, its prompt, the layout, the quality bar | Three scores, flags, subject position, calm areas, a suggested change |
| Ranker | The order of a round | Every scored photo of the round | Best-first order with a reason per sample |
| Judge | Whether a round is good enough | The scored, ranked round and the stop score | Good enough or not, a reason, two to four changes for the prompt |
| Final checker | Which layout ships | Up to three composed layouts, the words, the quality bar | The pick, ship or not, a score, the biggest flaw, a fix hint |
| Editor | A typed change to the layout | The request, the layout's numbered blocks, the kit's palette and fonts, a picture of the canvas, an uploaded file | A short list of edit operations from a fixed set and a one-line summary, or one question |

Hand-offs never go agent to agent. Each answer is written into SQLite as the session's product
state, and the next step reads from there; the editor agent's answer is the one exception, it
goes back to the page as a list of edits and only the run's step note is kept. The ADK session
state is scratch for one run only.

Why this split: each agent can be tested, replaced and explained on its own, and its instruction
stays short. The cost is more calls per post, about a dozen in an auto run.

## 3. Orchestration

Every run is an ADK `Workflow` graph of named function steps: draft, samples, revise prompt,
compose, finish, revise, scout, auto and edit (the editor's Ask, one step). Steps that call an
agent run as rerunnable nodes, as the ADK requires. Each run gets its own `Runner` and in-memory
session service.

The auto run is the only planner, and it is a loop with a fixed decision order after each round:
no judgement, good enough, rounds limit, photo allowance. Code holds the judge to its own rule: a
top sample with a hard flag, or under the stop score, is never good enough. When the loop goes
on, the judge's changes become the critic's feedback on the round and the prompt is revised from
them, exactly as it would be from a designer's comment.

A session moves through needs brief, choosing, drafted, reviewing, composing and finished. In
manual mode the designer moves it; in auto mode the loop does, and the session page shows the
decision lines as they are made. Child runs carry their parent's id, so an auto session reads as
one tree on the Runs page.

## 4. Spec then render

A post is a specification, not a model's drawing: a photo, a headline, a subline, a logo, a
layout template and a light or dark mode. Jinja and CSS templates render it in headless
Chromium. Six templates ship (hero, full bleed, split, corner, caption strip, words only) and a
seventh, custom, is whatever the editor produced.

Why: brand rules can be enforced in code, the logo is never stretched, the words are always set in
the kit's face, a WCAG contrast check runs on every text block, and a layout can be changed
without touching the photo. The cost: layouts are limited to what the templates express, and the
posts share a family look. Section 11 says what that costs and section 12 what fixes it.

The photo prompt is the one place the model is free. The prompt writer is told the backdrop
colour for the mode, which third of the frame must stay plain for the words, and that the
photograph never contains text, lettering or a logo, because the studio sets those afterwards.

### The studio's own look

The tool's chrome follows Hybridge's design system, the look of its own internal tools: ink
on white surfaces over a porcelain canvas, the kit's typeface, headings in the brand's blue,
glass for the navigation bar only, colour only where it means an outcome, light by default,
dark by choice, one primary action per view. The rules are re-expressed in the studio's own
stylesheet rather than copied from another project, and the chrome reads its colours and
typeface from the active kit, so the Hybridge kit yields the Hybridge look and another kit
tints the tool its own way.
The spec is `docs/superpowers/specs/2026-10-07-design-studio-ui-design.md`; a "How it works"
page introducing the eight agents is the next page to be built on it.

## 5. Calls to models and services

| Service | How | Why |
| --- | --- | --- |
| Gemini 3.5 Flash-Lite | ADK `LlmAgent` with structured output; the client retries 429 and 5xx | The free tier with vision; structured output removes parsing |
| Cloudflare Workers AI, flux-2-klein-4b | One HTTP call per photo, 180 second budget, one retry on timeout | The only zero-cost photo model found with a usable daily allowance |
| Tavily search | One HTTP call per query, 900 credits a month cap in the store | Free plan with no card; Google's grounding is not available at zero cost on the account used |

Each service sits behind a small interface with a fake beside it, chosen from the environment:
demo mode when no keys are set, otherwise the real provider. Critics run three at a time, renders
too. Keys come only from the environment. Errors are logged by type, never by text, so a request
address never leaks into a log.

## 6. Failure and fallbacks

| What fails | What the studio does | What the person sees |
| --- | --- | --- |
| No keys | Runs on stand-ins end to end | A demo badge; every page works |
| A photo call times out | Retries once after 180 seconds | The step's note names the timeout |
| A round makes no photo | Composes the best photo from an earlier round, or fails the run when there is none | "Stopped: … Composing with the best photo so far." |
| The daily photo allowance is used up | As above; no second provider exists | The provider's message, in plain words |
| A critic fails on one sample | That sample keeps the reason instead of a score | "The critic could not score this sample." |
| The judge fails | The round is composed as it is | "The critic could not judge this round, so it was composed as it is." |
| The final check fails | The first layout ships | "Final check skipped: …" |
| Research fails or finds nothing | The draft follows the brief alone | "Research failed (…); drafting from the brief alone." |
| A model answer does not fit its schema | Asked once more with the problem fed back, then the step fails | The step's error on the run page |
| The app restarts mid-run | Running steps are marked interrupted | The run shows as interrupted; the session is usable |
| An upload is not an image, too large or cut short | Refused before it is saved | The reason under the field; the session or the layout is unchanged |
| The editor agent fails or answers nothing usable | The edit run is marked failed; nothing is applied | "The editor could not answer. Try again.", or its one question |
| An edit would leave the canvas, name a missing block or remove the logo | Dropped by code, the rest applied | The dropped edit named under the summary; Undo restores the layout |
| The designer presses Stop | The run is cancelled cleanly | "Stopped by you." and manual mode |

Every failure in an auto run ends in the same place: the session back in manual mode with
whatever was made, and one sentence saying why.

## 7. Visibility and explaining decisions

The Runs page lists every run with its steps; each step shows its status, duration, provider and a
one-line note. Child runs sit under their parent. The session page polls while a run is live and
keeps the decision lines: which direction was taken and why, how each round scored and what
changed, which layout the final check picked and its biggest flaw.

Every choice leaves a sentence. A direction carries a why and a recommended reason. A prompt
version carries a reason for the prompt and one for the words, and versions can be diffed. The
critic names the biggest weakness even on a strong photo. The ranker gives a reason per sample.
The final checker writes one line per layout it compared. The research shows its queries and its
numbered sources, and every fact a direction uses names its source.

The design-level reasons are in this document, in the six version specs and plans under
`docs/superpowers`, which were written before each build, and in the two Archify diagrams under
`docs/architecture`.

## 8. Quality: how we know

Three checks, in code and by agents:

1. The critic scores on brief, brand fit and craft from 1 to 5, raises hard flags (text in image,
   logo in image, distorted anatomy, unrealistic) and soft ones, and is held to the brand's
   quality bar, one image chosen on the Library page. The kit's ideal example is the default.
2. The judge may only say good enough when the top sample has no hard flag and reaches the stop
   score, and code enforces that.
3. The final checker compares the composed layouts and says whether the one it picks could be
   published as it is.

The honest part. The critic is generous: five of the six photos in the last two real runs scored
5.0, so the auto loop usually stops after one round and the revise path is rarely exercised. The
final checker gives 4 or 5 to nearly everything. The committed samples in `samples/README.md`
show the scores as the agents gave them, flags included. A person still makes the real call, and
the studio is built so that call is one click away at every step.

## 9. Decisions and rejected options

| Option | Decision | Why |
| --- | --- | --- |
| Gemini image generation | Rejected | Not on the free tier |
| Google Search grounding for the scout | Rejected after a spike | 2.5 Flash-Lite is closed to new users on the account; the 3.5 models refuse the search tool with 429 |
| Brave Search | Rejected | Needs a card |
| A local diffusion model | Rejected | No GPU on the development machine; containers must run anywhere |
| LangGraph | Not used | The brief prefers the ADK; the ADK's workflow graph fits a run of typed steps |
| A model-drawn layout | Rejected | Brand rules, the logo and contrast must be guaranteed, not hoped for |
| One-click generate (v1) | Removed in v2 | A designer wants to see and steer every step |
| Generated photos of the clinic, team or patients | Forbidden by rule | A generated picture must never pass for the real thing; such directions are marked as needing a real photo |
| Words-only posts in auto mode | Rejected on 7 October | The first grounded run took a words-only direction and produced words on a grey field; every auto post now shows a photograph |
| Copying reference designs into posts | Never | References are hot-linked and analysed into style cards; a reference shapes the prompt and is never copied |
| A free-form canvas editor | Limited to a custom layout with guardrails | The deadline; the renderer still enforces the kit's rules on custom layouts |
| Reference images as image-to-image input | Rejected | The free photo model is text to image; references guide the prompt and are never copied |
| An editor agent that redraws the layout | Rejected for a fixed set of edit operations | A small command set is reliable, keeps the guardrails in code, and makes every change explainable and reversible |
| A shade or image over the logo or words | Reported, never moved | A sticker over the logo's corner may be wanted; the guardrail line says what is covered |

## 10. Costs and limits

Everything runs on free tiers; the numbers and the model landscape are in
[MODELS-AND-COSTS.md](docs/MODELS-AND-COSTS.md). The limits that matter day to day:

- About 50 seconds for an auto post: research 10 s, draft 4 s, one round of three photos 33 s,
  compose and final check 4 s.
- The photo allowance is roughly 40 to 70 photos a day on the free plan.
- A dozen model calls per auto run against the free tier's per-minute cap, so two auto sessions
  at once can slow each other down.
- About 3 search credits per researched session against 1,000 a month.

## 11. Known gaps

- The critic and the final checker are too generous (section 8). The quality bar moves them
  little.
- The two brief gates disagree: the direction writer may refuse a brief that the prompt writer
  then accepts, and the page does not yet show the writer's question in that case.
- Nothing in code stops the prompt writer answering the words-only layout in auto mode when no
  direction was chosen; it is told not to, and the chosen direction's layout is enforced.
- The research's facts read as marketing advice, because the queries ask how organisations post.
  The caption rules keep that advice out of the post.
- Posts have no slot for a call to action, address, phone, booking link or offer terms, so a real
  campaign post still needs an edit.
- One format, 4:5. No 1:1, no 9:16, no carousels.
- Every post is a photo with a headline and a subline. Offer cards, event cards, quote cards,
  number cards, steps and comparisons do not exist yet.
- The editor has the designer's own text and images and an Ask mode with one step of undo, but
  no align, snap, crop, text styles or shapes, and the Ask mode's placing is the model's judgement
  from a picture, so "a bit to the left" lands roughly.
- The 99 tests cover the store, the renderer, the brand loader, the importer, the photo client,
  the demo workflows and the routes. They do not cover the auto loop, the scout, the critic
  context, or anything from v5 (the uploads, the references, the new guardrails, the edit
  applier, the Ask), which were tested by hand and by throwaway scripts.
- The Ask mode's coverage lines measure a text block's first line only, and the editor's
  context carries the words typed on the page, so what the agent sees is only as current as
  the last Preview.
- Only the Hybridge kit has been run. A second kit is the next proof of the brand-agnostic claim.
- One process, one container, SQLite, in-memory agent sessions. A run does not survive a restart
  and nothing queues.

## 12. What another week buys, in order

1. **Post types.** Offer, event, quote, number, steps and myth-versus-fact carousels, comparison.
   Each is a template, a words schema and a clause in the writer's instruction; most need no
   photo. The direction writer's format field gains the new values.
2. **Mandatory elements.** A contact block in the kit (address, phone, email, website, booking
   link, hours, legal line) and a footer slot in every template.
3. **Onboarding from the UI.** A Brand page that creates the kit folder: name, audience and feel;
   logos; fonts; colours; contact and legal; example posts and the quality bar; research policy.
   The folder stays the source of truth; the UI writes it.
4. **A brand asset library.** "Your uploads" on the Library page is the seed: real photos,
   product renders, icons and approved quotes still need type tags, a consent flag and a path
   into the direction writer's context.
5. **Sizes and carousels.** The composition already carries a width and height; templates that
   respond to the canvas give 1:1 and 9:16, and a slide set gives carousels.
6. **Brand devices.** Accent bars, corner shapes, badges, an icon set, photo treatments and
   eyebrow text, defined in the kit and drawn by the templates, so posts stop looking like the
   same still life.
7. **The critic as an inspector.** Hard flags and a one-line verdict instead of 1 to 5 grades,
   scoring optional in manual mode, anchor images at each score, and a structured prompt
   builder with a visual vocabulary file per kit, because a precisely described hero object is
   what makes the image model deliver.
8. **Approval and export.** Draft, review and approved states; alt text; an export of image plus
   caption.

## 13. Open questions for Hybridge

Built on assumptions that a short call would settle:

- What does the designer post in a typical month, and in what mix of photo posts, offers,
  events, education and stories?
- Which elements must every post carry?
- Do real assets exist: team and office photos, product renders, approved patient quotes?
- Which platforms and sizes matter, and are carousels part of the plan?
- Who approves a post before it goes out?

## 14. How it was built

Four versions in three days, each from a written spec and plan under `docs/superpowers`, each
built by implementer agents in short rounds, reviewed as a whole diff, fixed once and re-reviewed,
then tested by hand with real models. A fifth version followed the demo call on 8 October: the
designer's own images as references and in the editor, and the editor's Ask mode, from the spec
`docs/superpowers/specs/2026-10-08-design-studio-v5-design.md`, built the same way in three
tasks with a review, a fix round and a re-review each, then one review of the whole and its fix
wave. The rulings taken while building are listed at the end of each spec, under "As built". Tests were written for the foundations in v1 and v2; from
v3 on, speed was chosen over new tests, which section 11 records as a gap. Rulings made along the
way are in the specs' revision notes, the most consequential being the switch from Google
grounding to a search provider after the spike showed no zero-cost path.
