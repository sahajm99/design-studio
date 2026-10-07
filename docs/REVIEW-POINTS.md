# Review points

Things to look at closely before the hand-in, one at a time, in the order Sahaj raised them.
Started 2026-10-06. Each point gets a status line when it is done.

## 1. The agents' instructions

Status 2026-10-07: the writer's layout and framing rules and the comparison final check are specified in `docs/superpowers/specs/2026-10-07-design-studio-v3-1-design.md`; the full prompt review and the labelled set stay open.

Question: does each of the six prompts actually ask for what the step needs, and is it good
enough? They were written by an LLM in one pass, with no human tuning loop.

- Analyst (`ANALYST_PROMPT`): describes one reference as a style card. Check the five fields are
  the ones taste needs and that "mood" and "technique" are used anywhere (technique is not).
- Prompt writer (`PROMPT_WRITER_PROMPT`): four tasks in one prompt (draft, revise, words,
  words for photo). Check the draft rules (60 to 120 words, order of the paragraph, the fixed
  ending), how the taste is referred to (only "draw on what the liked reference images share"),
  the layout hint, and the revise rules (what the feedback names wins; the banned-term guard).
  Evidence of weakness: once invented "polished chrome" for an implant.
- Critic (`CRITIC_PROMPT`): the scores spread (2.7 to 5.0 in the sample table) but 5 is common.
  Check the anchors and whether the flags fire (distorted anatomy did).
- Ranker (`RANKER_PROMPT`): check that it prefers realism and brand fit over polish, as told.
- Judge (`JUDGE_PROMPT`): its changes are sometimes generic ("softer light"). Check it is told
  to draw them from the critic's suggested changes and flags, and whether it should see the top
  photo (it sees text only).
- Final checker (`FINAL_CHECK_PROMPT`): gave 5 of 5 to all six sample posts, including one with
  a flagged photo. The rubric needs a stricter anchor or a comparison across the three layouts.
- Method: a labelled set of 10 to 20 photos and posts scored by a person; tune each prompt
  until the model's scores track the labels; `scripts/make_samples.py` is the harness.

Status: open.

## 2. How the designer provides taste

Today: import the kit's inspiration board, or drop files into `data/inbox` and press import,
then like or dislike each reference. Wanted: more ways in, and a better signal.

- An upload button on the Library page (the session page already has one for photos).
- Paste image URLs (for example from a search) and have them fetched into the library. Note the
  assessment's rule, "only permitted sources": the kit's board and the brand's own files are
  permitted; an arbitrary URL is a licensing question. If we add URL import, say on the page
  that the designer confirms they may use the image, and keep the reference out of posts (it
  never is: references shape the prompt, they are never copied).
- Show what the taste did: on the session page, the taste summary, the liked cards and the
  four images the prompt writer saw, so the designer can see what shaped the draft.
- The majority rule at small counts: with 2 dislikes every field with 1 vote is reported, so the
  summary reads as noise ("1 few words, 1 abstract subject, …"). Require 3 or more before a
  field is reported, or weight by share.
- Which images go to the prompt writer: the first four liked in library order, not the most
  recently liked or the most representative. Let the designer pin up to four, or pick by the
  fields that match the brief.
- Disliked references are counted but never shown; consider showing one or two as "avoid".
- Later: an embedding over liked references instead of counts over five fields.

Status: open.

## 3. The quality bar (the ideal example) in the UI

Status 2026-10-07: specified in the v3.1 design (section 4).

Today the ideal example is one file the kit ships, `examples/ideal-output.png` under the brand
folder. It is sent as "The brand's quality bar" to the prompt writer with the draft, to the
critic beside every photo and to the final checker beside the composed post. The designer
cannot see it in the studio and cannot change it; the brands folder is mounted read-only.

Wanted, as Sahaj asked on 2026-10-06:

- Show it on the **Library** page, which is where taste lives: a "Quality bar" panel with the
  current ideal image and where it came from (the kit, or a designer's choice), with a
  thumbnail on the session page.
- Change it: "Set as the quality bar" on any post, sample, archive photo or uploaded image; an
  upload field on the Studio page for an outside image; "Back to the kit's example" to reset.
- Where it lives: a copy under `data/quality-bar/` and a pointer in a small settings row in the
  store (studio-wide, with an optional per-session override). The kit's file stays the default
  and is never written to, so another brand still works unchanged.
- Who reads it: one helper (`quality_bar(kit, store)`) replaces the three places that read the
  kit's file today, so every agent sees the same image.
- Trace it: the run page's step note names which image was the bar for that run.

Status: open.

## 4. An Editor tab

Status 2026-10-07: specified in the v3.1 design (section 5).

Today the editor is reached only through a photo: "Open in the editor" on a sample, archive or
post card. Wanted: a fifth tab, Editor, next to Studio, Library, Archive and Runs, where the
designer starts from the editor itself.

- The tab opens a picker: every photo in the archive, the posts, and an upload field, in one
  grid; choosing one opens the editor on it.
- Behind the scenes it needs a session to hang the candidate and the post on, so the picker
  creates one quietly (the way "Open in the editor" from the archive does today) and the
  words start empty.
- Finished work still lands on the post page and in the archive, so nothing else changes.

Status: open.

## 5. Monotony: the same centred still life in the Hero layout

Status 2026-10-07: specified in the v3.1 design (sections 2 and 3).

Two causes, stacked. The prompt writer's rules ask for a studio still life, subject centred,
plain backdrop, and hold it to the kit's ideal example, so every photo converges on one look.
And auto mode always ships the Hero layout, because the final checker accepts the shortlist's
first candidate, which is the writer's hint, which is always Hero.

- Tell the writer to vary the framing (close, environmental, hands, person) when the brief or
  the layout hint calls for it, and to treat the ideal example as a bar for finish, not as a
  composition to copy.
- Make the auto run's final check compare the three candidates and choose, instead of
  accepting the first; or rotate the hint across the allowed templates.

Status: open.

## 6. A scout agent and an ideation step (diversity of concept)

Sahaj's idea, 2026-10-06: when the designer does not know what the post should look like
(for example, how implant clinics mark dental awareness week), an agent researches online and
brings back the trends, so taste is not fixed to one reference.

- Text only, never images: angles, campaign ideas and visual directions with source links. The
  assessment allows permitted sources only, and other clinics' posts are not that; ideas are.
- The scout uses Gemini's Google Search grounding tool through ADK (verify it is free within
  the API's limits before building on it).
- A new ideation step: the prompt writer proposes three distinct directions (angle, subject,
  framing, mood, layout hint); the designer picks one, or the judge does in auto mode; only
  then is the photo prompt written. The directions and their sources show on the session page.
- Cost: one or two calls per draft, about a day of work. Build the three-directions step first;
  the scout second.

Status: open; documented as "what another week buys" unless the hand-in documents are done by
Wednesday night.

Status 2026-10-07: specified as Part A of the v4 design,
`docs/superpowers/specs/2026-10-07-design-studio-v4-design.md`.

Decided 2026-10-07, and what was checked that day:

- Search backend: Gemini 2.5 Flash-Lite with ADK's built-in `google_search` tool, the model id in
  configuration. Gemini 3.5 Flash-Lite has no Google Search grounding on the free tier ("Not
  available" on Google's pricing page); 2.5 Flash-Lite has it free up to 500 requests a day,
  shared with Flash. The paid allowance needs a billing account, which the zero-cost rule forbids.
- ADK runs `google_search` only on an agent with no other tools, so the scout is its own agent;
  turning its answer into typed angles is a second step.
- Google's terms for grounded results: show them with their Search Suggestions, without changing
  or mixing other content into them, and do not cache them. So the session page shows the
  scout's answer as returned, with Google's suggestion chip; it is stored only as part of that
  session's history, with no cache across sessions.
- Fallback when search fails: the model's own knowledge plus an occasions calendar in
  `brand.yaml`, labelled "not grounded".
- Memes and trends are used for their mechanic (format, structure, feeling), never their image,
  characters or recognisable people; how much trend the brand wants becomes a `brand.yaml`
  setting, since Hybridge's feel is premium and restrained.

## 7. Educational posts: a post type the studio cannot make yet

Sahaj's finding, 2026-10-07: an image search for "what are dental implants made of" is almost
all explainers (labelled parts, numbered steps, natural tooth beside implant), not lifestyle
photos. The studio has no layout for that format: `samples/02`, whose brief asked to explain
something, came out as another hero photo, and its implant posts stick out sideways from the
ends of the bridge, which is not how a full-arch bridge is built.

- Never reuse the found images; they belong to the clinics and the academy that made them, or to
  stock libraries that licensed them to those clinics. They are format references only.
- New templates: `steps` (one to three numbered steps with icons), `parts` (one object with
  labelled callouts), `compare` (two things side by side). Words, numbers, icons and leader lines
  are drawn by code, which is why this suits the studio's architecture: an infographic is mostly
  text and layout, the part image models do worst.
- The picture is one clean render (an exploded view of crown, abutment and implant on a plain
  backdrop); Gemini returns bounding boxes for named parts, and code draws the leader lines to
  them. Icons come from an open-licence set, never from the image model.
- The facts come from the scout's grounded sources, shown with their links, then a claims check.
- Risks: technically wrong renders are misinformation in healthcare. No jaw or bone cutaways; a
  `technically_wrong` critic flag; a person approves every educational post.
- Brand restraint: at most three labels and about fifteen words for a brand whose feel is "few
  words, one strong image".
- Order: these templates first, the scout second; without them the scout's finding (which
  format this niche uses) has nowhere to go.

Status: open; for DESIGN.md as next week's work, with `samples/02` as the evidence.

## 8. A words-only direction in auto mode wastes the photo loop

Seen 2026-10-07 on the first grounded auto run (session 2a2abbc95689): the two picture directions
needed a real photo (the clinic, the team), so auto mode took the words-only one; the draft then
wrote a photo prompt about "typography on a background", the photos came back as plain grey
fields, the critic marked them as lacking typography, the judge asked to "incorporate the
typography", and the post is the words on a grey field after two rounds and four photos.

- In auto mode the direction writer must offer at least one direction that neither needs a real
  photo nor is words only; a code check asks once more when none is, and the auto run prefers
  such a direction.
- When a words-only direction is chosen anyway, skip the photo rounds and compose the
  type-only layout directly, or make the writer describe a quiet backdrop and tell the critic and
  the judge that no typography is expected in the photo.
- The quality bar is for finish, not style: a loud flyer as the bar contradicts the kit's feel and
  confuses the critic. Say so on the Library page, and maybe refuse a bar that the kit's own
  analyst would card as "many words".

Status: done 2026-10-07 (generation fix round in the v4a ledger). Sahaj's rule: every topic gets a post with a
real, appealing picture; auto mode never makes a words-only post. The direction writer must offer
at least two directions that can be made (a format other than statement, no real photo needed, and
a picture never carries type_only), with one retry and a note; auto mode takes the recommended one
when it can be made, else the first that can, else it drafts from the brief alone; in auto mode the
prompt writer is not offered type_only; the critic and the judge read the layout and the direction
and a code guard drops any ask for text, lettering or a logo in the photo; the prompt writer never
describes words in the photo and treats a statement direction as a backdrop; facts are for the
reader, never marketing advice; the Library page says the bar sets the finish, not the style.
Two grounded runs of the Long Island brief then gave picture posts (final check 4 of 5, ship).
Still open: nothing in code stops the writer answering type_only in auto mode without a direction;
the critic scored every sample 5.0 again (see point 1).

## Already noted elsewhere

- The final checker's generosity and the Hero-only auto layouts (`docs/MODELS-AND-COSTS.md`, section 2).
- Photo timeouts under concurrency, no seeds, small-model anatomy errors (same document).
- The parked minors from the v3 reviews (the ledger in `.superpowers/sdd/2026-10-06-design-studio-v3/progress.md`).
