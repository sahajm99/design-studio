# Design Studio v5: the designer's own images

Date: 2026-10-08, from the demo call. Status: proposed, waiting for Sahaj's go. Builds on the
v4 studio and the UI spec of 2026-10-07; new pages and panels follow that spec.

## Why

Today a designer can only type. The quality bar is one image, brand-wide, and it tells the
critic what finish to expect, not what this post should look like. The Library's liked
references shape taste for every post, not for one. There is no way to say, for one brief,
"make it look like these" with pictures.

The editor can arrange only what the studio made: the headline, the subline, a shade, the
photo and the kit's logo. A second logo, a badge, a sticker, a third line of text cannot be
placed, so a post that needs them leaves the studio half done.

Three additions close this: reference images on a session, image and text blocks in the
editor, and a shelf of the brand's uploads so an image uploaded once can be used again.

## Scope

- A. **Session references.** Up to six images attached to a brief, in the Studio form and on
  the session page, each with an optional note, that guide the prompt writer for that post.
- B. **Editor content.** Free text blocks and image blocks the designer adds, moves, resizes,
  layers and deletes, rendered by the normal renderer.
- C. **The brand's uploads.** Everything uploaded through the editor or as a session
  reference is kept under the brand and offered again in the editor's picker.
- D. **The editor's Ask mode.** A box in the editor where the designer types what to change,
  with an optional file, and an agent applies it to the layout as a list of edits the designer
  can undo.

Non-goals: image-to-image generation (the photo model is text to image; references guide the
prompt, not the pixels); the full asset library with tags and consent flags, which stays on
the next-week list; new sizes or carousels; SVG uploads; an agent that redraws the whole
layout (D applies edits to the layout that exists).

## A. Session references

**What the designer sees.**

- The Studio form gains "Reference images" under the brief: a file input for several PNG or
  JPEG files, each up to 15 MB, at most six. The hint says what they are for: "How this post
  should look. The studio draws on them and never copies them."
- The session page's brief panel shows them as thumbnails beside the quality bar, each with a
  one-line note field ("the light", "this framing", "a product this size") of at most 80
  characters, a Remove link, and an Add control for more. A session with no references shows
  the Add control only.
- The post page's summary line counts them: "Made with 2 references."

**What the agents get.**

- The analyst cards each reference the moment it is uploaded, through the Library's existing
  analysis job, so a style card (mood, technique, palette, layout traits) exists for it.
- The prompt writer's draft, revise and words tasks receive the images after the brief and the
  quality bar, each labelled "Designer's reference n" followed by its note, and the context
  gains `designer_references`: the notes and the style cards. Its instruction gains one rule:
  "designer_references are the designer's own pictures of how this post should look. Draw on
  their subject, composition, light, materials and mood as the brief's intent, in the order
  they are given; say in reason_prompt what you took from each; never describe their text or
  logos; never copy one."
- The direction writer's context gains the same notes and cards as text, so the three
  directions fit what the designer showed, with the rule "the directions fit
  designer_references when they are given".
- The critic receives up to three of them after the quality bar, labelled the same way, with
  the rule "hold the photograph to the quality bar for finish; judge on_brief against the
  prompt and the designer's references". The judge reads the critic's scores as today.

**Storage and contracts.**

- `SessionReference(id, session_id, image_path, note, card: StyleCard | None, created_at)`;
  table `session_references`; files under `data/uploads/sessions/<session_id>/`.
- Routes: `POST /sessions` accepts the files with the brief; `POST /sessions/{id}/references`
  adds; `POST /sessions/{id}/references/{ref_id}/note` saves a note;
  `POST /sessions/{id}/references/{ref_id}/remove`. The same validation as the Library's
  upload (type, size, dimensions, duplicates by content hash).
- A session started from the archive or from a post inherits nothing; references are per
  session.

## B. Editor content

**Blocks.** `BlockKind` gains `text` and `image`. `Block` gains `text` (text blocks, at most
200 characters), `image_path` (image blocks, a path under the brand's uploads), `fit`
(`cover` or `contain`, image blocks) and `keep_aspect` (image blocks, default true). The
blocks list's order is the stacking order, back to front. The headline and subline stay the
kit-set roles with the kit's words; a text block is the designer's own line.

**What the designer sees.**

- Two new controls beside "Add a shade": "Add text" and "Add image".
- Add text places a block in the centre reading "New text", selected, with the headline's
  controls (font, weight, italic, size, alignment, colour) plus a field for the words.
- Add image opens a small picker: upload a PNG or JPEG, or choose from "Your uploads" (part
  C). The image lands at a third of the canvas width, aspect kept, selected, with fit,
  opacity and a "Keep aspect" toggle. Resizing with aspect kept scales from the dragged
  corner.
- Every added block has Delete, Bring forward and Send back. The kit's logo, the headline and
  the subline keep their existing rules (the logo cannot be deleted, as the kit requires it).
- Preview and "Use this layout" work as today; the saved composition carries the new blocks.

**Rendering.** `custom.html` draws text blocks like headline blocks and image blocks as
absolutely positioned images with the chosen fit and opacity, in list order. The contrast
check runs on every text block as it does for the headline. Uploaded images are served through
the existing media route.

**Guardrails** (`apply_guardrails`): at most twelve blocks; every block inside the canvas;
an image block's path must resolve inside the brand's uploads folder, never elsewhere; a text
block must have words; a minimum size for text (12 px) and images (4 percent of the canvas);
the kit's rules on its own logo still apply, and the studio cannot enforce them on an uploaded
logo, which the hint under Add image says.

## C. The brand's uploads

- `Upload(id, brand_id, image_path, name, width, height, source: editor | session, created_at)`;
  table `uploads`; files under `data/uploads/<brand_id>/`. Session references are registered
  here too, so a photo used as a reference can be placed in a post later.
- The editor's picker lists this brand's uploads newest first, with the name and a thumbnail;
  the Library page gains a small "Your uploads" strip under the quality bar with the same list
  and a Remove control, which is the seed of the asset library.

## D. The editor's Ask mode

**What the designer sees.** An "Ask" panel at the top of the editor's settings column: a
one-line field ("Make the headline smaller and move it up", "Change the logo to this one"), an
optional file input, and an Apply button. After a request the preview re-renders, a line under
the field says what was done ("Headline size 64 to 48; moved up 6 percent."), and an Undo
button restores the layout from before the request. A request the agent cannot settle comes
back as one question in the same line ("Which text should move, the headline or the
subline?"), and the designer answers in the field.

**The editor agent.** One ADK agent, built like the others, on the studio's model with a
pydantic answer.

- Reads: the request; the current `CustomLayout`; the kit's palette, fonts and rules; the post's
  words; the uploaded file, if any, as an upload id; and a rendered preview PNG of the
  current layout, so it judges positions from the picture and not from numbers alone.
- Returns `EditorAnswer(usable, question, edits, summary)`, where `edits` is a list of
  operations from a fixed set:
  `move(block, x, y)`, `resize(block, w, h)`, `set_text(block, text)`,
  `set_style(block, font, weight, italic, size_px, align, colour, opacity)`,
  `replace_image(block, upload_id)`, `add_text(text, x, y, w, …)`,
  `add_image(upload_id, x, y, w, fit)`, `add_shade(…)`, `delete(block)`,
  `reorder(block, forward | back)`, `set_photo(fit, offset_x, offset_y, box)`,
  `set_background(colour)`. Blocks are named by their index and kind in the context
  ("block 2, headline"), and the answer refers to them the same way.
- Instruction, in short: apply the request with the fewest edits; keep everything the request
  does not name; keep the kit's rules (the logo stays, sentence case, the palette's colours
  only); say in summary what changed in one sentence; when the request names something that
  does not exist or could mean two things, set usable false and ask one question.
- A stand-in in demo mode handles a handful of verbs (smaller, larger, up, down, left, right,
  change the logo, change the text) so the feature can be shown without a key.

**What code does.** The route `POST /sessions/{id}/editor/ask` takes the field, the file and
the current layout from the page, registers the upload (part C), renders the preview through
the existing preview endpoint, calls the agent once (one retry on an answer that does not
fit), applies the edits in order to a copy of the layout, runs `apply_guardrails` and the
contrast check, and returns the new layout, the summary and the question to the page. The
page loads the layout into the canvas, keeps the previous one for Undo, and shows the line.
An edit that the guardrails reject is dropped and named in the line ("The image could not
leave the canvas."). Nothing is saved until the designer presses "Use this layout", as today.

**Limits, stated.** Positions are the model's judgement from a picture, so "a bit to the
left" lands roughly and the handles finish the job. One model call per request. The agent
never touches the photo's pixels or the kit's logo file.

## Decisions

- References guide the prompt and never the pixels: the photo model has no image input on the
  free tier, and a copied picture would break the brief's originality rule.
- The Ask mode edits the layout that exists through a fixed set of operations, never a
  regenerated layout: a small command set is reliable, keeps the guardrails in code, and makes
  every change explainable in a line and reversible with Undo.
- Notes are optional but encouraged: a picture without a note is taken whole; a note narrows
  it, which is what a designer means by "like this, but only the light".
- No SVG: a PNG with transparency carries a logo and needs no sanitising.
- The kit's own logo keeps its guardrails; an uploaded one is the designer's responsibility.

## Effort and order

1. B's contracts, renderer and guardrails, then the editor controls: about a day.
2. C: about two hours, built with B's upload path.
3. A's storage, routes, panels and the agent context: about half a day.
4. D's contracts, agent, stand-in, route and panel: about a day, last, because it needs B
   and C.

About three days with reviews, after the hand-in. Each part ships on its own.

## Acceptance checks

- A session carries up to six references with notes, shown on the session page; the prompt
  writer's reason names what it took from them; the run's draft step notes how many were sent.
- In the editor, Add text and Add image create blocks that move, resize, reorder and delete;
  the rendered PNG matches the preview; an image path outside the uploads folder is refused;
  the suite passes.
- An image uploaded as a reference appears in the editor's picker.
- In the editor, "Change the logo to this one" with a file replaces the logo block's image
  and keeps its box; "Make the headline smaller and move it up" changes the size and the
  position and nothing else; Undo restores the layout; a request that names nothing on the
  canvas comes back as a question; an edit the guardrails reject is named in the line.

## Open questions

1. Should the critic see the references, or only the prompt writer? Proposed: the critic too,
   up to three, as written above.
2. Should the Studio form take references before the session exists, or only the session
   page after? Proposed: both, since a designer often has the pictures before the words.
3. Should the Ask mode also be offered on the post page, as the "Ask for a change" box is?
   Proposed: no; that box changes the words or the photo through the session, and the Ask
   mode changes the layout in the editor. Keeping them apart keeps each explainable.
