# The pipeline, its bottlenecks, and the models and their costs

Written 2026-10-06 from the code as it stands and from prices checked on the web the same day
(sources at the end). Prices change; the method stays.

## 1. The poster pipeline, step by step

| Step | Who does it | What goes in | What comes out | Calls and time (real models) |
|---|---|---|---|---|
| Gather | code (`library/sources.py`, `importer.py`) | the kit's inspiration board, the designer's inbox folder | reference rows with the image files | no model; one HTTP fetch per image, once |
| Describe | the **analyst** agent, paced by `AnalysisJob` | one reference image | a style card (background, layout, text amount, subject, colour count, mood) | 1 Gemini call per reference, 10 a minute |
| Curate | the designer, then code (`taste.py`) | like and dislike clicks | a taste profile: counts per style field and a sentence | no model |
| Draft | the **prompt writer** agent | the brief, the brand kit (feel, rules, palette), the taste profile, up to 4 liked reference images and the kit's ideal example | a concept, a photo prompt (60 to 120 words), headline, subline, caption, hashtags, and the reasons | 1 Gemini call, about 5 s |
| Photos | the **photo provider** (Cloudflare FLUX.2 klein) | the prompt plus the backdrop clause, 1088 by 1344 | N PNGs | N calls in parallel, 25 to 90 s each; this is the critical path |
| Review | the **critic** agent, 3 at a time | each photo, the kit's ideal example, the prompt and concept | scores (on brief, brand fit, craft), flags, subject position, calm areas | N Gemini calls with 2 images each |
| Rank | the **ranker** agent | every scored photo of the round | an order with reasons | 1 Gemini call with N images |
| Decide | manual: the designer; auto: the **judge** agent inside code's limits | reviews, ranking, stop score | continue with changes, or stop | auto: 1 Gemini call, text only |
| Revise | the **prompt writer** again (task revise) | the current prompt, the feedback (designer's or critic's), reactions; a banned-term guard in code | the next prompt version with a change list | 1 Gemini call, retried once if a banned term survives |
| Compose | code (`compose.py` rules, the renderer) | the chosen photo's subject position and calm areas | 3 rendered layout candidates | no model; about 1 s each in Chromium |
| Final check | auto only: the **final checker** agent | the composed PNG and the ideal example | ship or not, a score, the biggest flaw | 1 Gemini call with 2 images |
| Finish | code | the chosen candidate | the post PNG and caption, saved | no model |
| Edit | the designer in the editor; code guards (`custom.py`) | any photo, blocks placed by hand | a custom layout the same renderer draws | no model |

One correction to the usual mental model: the analyst never writes prompts. It describes
references so that taste can be counted. The prompt writer is the only agent that writes the
photo prompt, and it is the only place the brief, the brand and the taste meet.

A manual session with one round of 3 photos costs 1 draft, 3 reviews and 1 rank: 5 Gemini
calls and 3 photos. An auto session of 2 rounds of 2 photos costs about 11 Gemini calls (1
draft, 4 reviews, 2 ranks, 2 judgements, 1 revision, 1 final check) and 4 photos, and takes
2 to 4 minutes, almost all of it waiting for photos.

## 2. Where the bottlenecks are

1. **Photo generation is the critical path.** 25 s is typical for one photo; under two or more
   concurrent requests the free tier has answered in 90 s or not at all. The provider now waits
   180 s and retries a timeout once. Everything else in a round takes seconds.
2. **Free-tier rate limits on Gemini.** The critic runs three at a time; a round of six photos
   is seven calls in about ten seconds. The gateway retries 429 and 5xx with backoff (three
   attempts), and the library analysis is paced at ten a minute. A busier studio would queue.
3. **The agents' instructions were written by an LLM in one pass, not tuned against an
   evaluation set.** There was no human prompt-engineering loop. The symptoms are visible in the
   sample table: the critic's scores spread from 2.7 to 5.0 and do discriminate, but the final
   checker gave 5 of 5 to every post, including one whose photo carried a distorted-anatomy flag;
   the judge's requested changes are sometimes generic. What limits the damage is that every
   agent answers a typed schema (a bad answer is retried, never trusted), the kit's ideal example
   anchors every judgement, and code owns the limits (banned terms, trimming, rounds, budget,
   hard flags). The first evaluation harness now exists: `scripts/make_samples.py` makes the
   table that shows the generosity. The fix is a labelled set of 10 to 20 photos scored by a
   person, then tuning the critic and checker rubrics until the scores track the labels.
4. **No seeds.** The model's API documents no seed, so a photo cannot be reproduced; variety
   comes from the prompt only.
5. **The subject is hard for small diffusion models.** Teeth, gums and metal fixtures are where
   FLUX.2 klein 4B fails most (the flag is `distorted_anatomy`); a larger model would fail less.
6. **Auto mode always ships the first layout.** The final checker accepted the shortlist's first
   candidate (Hero) every time; a comparative check across the three would spread the layouts.
7. **The taste signal is shallow.** Counts over five style fields and a sentence; it steers the
   mode and the mood, not much more. An embedding over liked references is the next step.
8. **One container, one SQLite file.** Right for one designer on one machine; not multi-user.

## 3. The image model

### What runs today

`@cf/black-forest-labs/flux-2-klein-4b` on Cloudflare Workers AI: Black Forest Labs' small,
distilled FLUX.2 model, hosted by Cloudflare behind one HTTPS call with a multipart body. The
studio calls it through `studio/photos/cloudflare.py`, which implements the `PhotoProvider`
interface; any other service or a local model is one more file.

Cloudflare bills in "neurons": 10,000 free a day, then $0.011 per 1,000. This model is priced per
512 by 512 output tile at 26.05 neurons. The studio asks for 1088 by 1344 (the post size rounded
to multiples of 32), which is 6 to 9 tiles depending on whether partial tiles count, so **145 to
234 neurons a photo, or about 40 to 70 free photos a day**; beyond that, **$0.0016 to $0.0026 a
photo**. Today's live studio made about 90 photos without hitting the allowance, which suggests
the lower count.

### Why this one

- The assessment's rules: zero cost, no payment details, runs locally. Cloudflare's free
  allocation needs an account and a token, not a card, and the model answers in under a minute.
- FLUX is the strongest open image family for clean product and still-life photography, which
  is what a dental implant brand posts; klein 4B is its fast variant.
- One HTTP call, no GPU in the container. The host has no GPU, so local diffusion was out.
- The cost is a rounding error even when paid: an auto session's 4 photos cost about a cent.

What it costs us: no seed, a small model's anatomy errors, and a daily cap that is enough for
about seven auto sessions or fifteen manual rounds.

### The landscape (prices checked 2026-10-06)

| Option | Free tier | Paid price per 1 MP image | Notes for this project |
|---|---|---|---|
| **Cloudflare Workers AI, FLUX.2 klein 4B** (used) | 10,000 neurons a day, no card | about $0.002 at our size | fast, hosted, no seed, small model |
| Cloudflare Workers AI, FLUX.1 schnell | same allocation | lower per tile | the older, faster model; weaker prompt adherence |
| fal.ai, FLUX.1 schnell | credits on sign-up, then card | about $0.003 per megapixel | fastest hosted FLUX; needs a card |
| fal.ai or Replicate, FLUX.1 dev / FLUX.2 dev | card required | $0.0084 (FLUX.2 dev on fal) to $0.025 | the quality step up; seeds supported |
| Google Imagen 4 Fast / Ultra (Gemini API) | not in the free tier | $0.02 / $0.06 | strong photoreal quality; reported to be retired from the Gemini API in August 2026 |
| OpenAI GPT Image | none | $0.005 to $0.21 by size and quality | best text rendering, but the studio never wants text in the photo |
| Hugging Face Inference | $0.10 of credit a month | varies | a handful of images a month; too little |
| Pollinations | needs a key; rate limited | free | quality and reliability below the bar |
| Together AI free FLUX | withdrawn | | was an option in 2025 |
| Local FLUX.2 klein / SDXL via ComfyUI or Ollama | free | electricity | needs a 12 to 24 GB GPU; the host has none; would add seeds and privacy |

### Cost per post, at paid rates, 4 photos a post

| Provider | Photos for one post | Compared with today |
|---|---|---|
| Cloudflare FLUX.2 klein (today) | about $0.01 | 1x |
| fal.ai FLUX.1 schnell | about $0.02 | 2x |
| fal.ai FLUX.2 dev | about $0.03 | 3x, better quality, seeds |
| Replicate FLUX dev | about $0.10 | 10x |
| Imagen 4 Fast | about $0.08 | 8x, while it lasts |
| GPT Image, medium quality | about $0.20 | 20x |

A designer making 10 posts a day on the paid Cloudflare rate spends about $0.10 a day on
photos; on FLUX.2 dev about $0.30. The text model is cheaper still.

## 4. The text and vision model

Gemini 3.5 Flash-Lite through Google AI Studio's free tier: multimodal (it judges photos and
posts), native JSON schema output (every agent's answer is a pydantic model), and the model ADK
speaks to directly. The free tier is rate-limited rather than capped by money; the reported
allowance is in the order of 1,500 requests a day for the lite model, which is about 130 auto
sessions. Paid, the model lists at $0.30 per million input tokens and $2.50 per million output
tokens; an image counts as about 1,000 tokens at our size, so an auto session's 11 calls are
about 35,000 input and 3,000 output tokens, or **about two cents**. Alternatives considered:
Groq's free tier (fast, but weak on images), OpenRouter's free models (no guarantees, rate
limited), a local model through Ollama (no GPU; vision on CPU is minutes per image). Gemini was
the only free option that sees photos well enough to be the critic.

## 5. What another week buys

A labelled evaluation set and a tuned critic and checker; a second photo provider (FLUX.2 dev
through fal.ai for paying users, or a local ComfyUI endpoint for a GPU machine) selected per
brand in `brand.yaml`; seeds where the provider supports them; a comparative final check across
the three layouts; an embedding-based taste profile.

## Sources

- Cloudflare Workers AI pricing: https://developers.cloudflare.com/workers-ai/platform/pricing/
- Gemini API pricing and free tier: https://ai.google.dev/gemini-api/docs/pricing and https://geotoolbox.ai/blog/gemini-api-pricing
- FLUX prices on fal.ai and Replicate: https://pricepertoken.com/image/model/black-forest-labs-flux-2-dev and https://www.gmicloud.ai/en/blog/fal-ai-vs-replicate
- Image API price survey (Imagen 4, GPT Image, market range): https://invideo.io/blog/ai-image-model-pricing/ and https://blog.laozhang.ai/en/posts/ai-image-api-pricing-comparison
