# Design Studio v3.1: the fix round after the first hands-on test

Date: 2026-10-07. Status: for a parallel build session. Builds on v3 (`2026-10-06-design-studio-v3-design.md`); everything not mentioned stays as it is. The taste changes (review point 2) are deliberately left out of this round.

## 1. What v3.1 is

Six changes from Sahaj's hands-on test of v3 and the v3 reviews, in build order:

| # | Change | Kind | Size |
|---|---|---|---|
| 1 | The final check compares the three layouts and picks one, instead of accepting the first | fix | small |
| 2 | Photos stop converging on one centred still life: the framing and the layout follow the brief | fix, prompts and one helper | small |
| 3 | The quality bar is visible and changeable in the studio | feature | medium |
| 4 | An Editor tab: start from the editor with any photo, post or upload | feature | small to medium |
| 5 | More ways to add references: upload files and paste links on the Library page | feature | small |
| 6 | The parked minors from the v3 reviews | fixes | small each |

### Assumptions to confirm

1. The final checker sees all the composed candidates in one call and names the one to ship; Hero may still win when the model prefers it.
2. The prompt writer chooses the layout from the kit's allowed templates, so manual sessions get varied first layouts too, not only auto mode.
3. The quality bar is one studio-wide choice (not per session). Setting it never writes into the brand folder; the kit's file stays the default and "Back to the kit's example" restores it.
4. The Editor tab creates a session quietly behind the picker; finished work lands on the post page and in the archive as today.
5. Pasted links are fetched once into the library like the board's images; the page says the designer confirms they may use them. A reference is never copied into a post.
6. The two sessions share one checkout: the build session owns `studio/` and `tests/` and the restarts of the live app; the documentation session owns `docs/`, `README.md` and `brands/`. No commits from either until the first commit is agreed.

### v3.1 is done when

| # | Check |
|---|---|
| 1 | `scripts/make_samples.py` on six briefs no longer yields six Hero posts, and each session's decisions name the layout the final check picked and why. |
| 2 | Two drafts for briefs that differ in subject (an object, a person) get different framings and different layout hints, and a split or full-bleed hint renders first in the shortlist. |
| 3 | The Library page shows the current quality bar and its origin; "Set as the quality bar" on a post, a sample or an archive photo, and an upload, change it; the next draft's load-context note names the new bar; "Back to the kit's example" restores the kit file. |
| 4 | The Editor tab opens a picker; choosing a post, a photo or an upload opens the editor on it; Preview and "Use this layout" make a post. |
| 5 | Upload on the Library page and a pasted list of links both add references that get analysed; a bad link is reported, not fatal. |
| 6 | Each minor in section 7 is verifiably gone. |
| 7 | Everything runs in demo mode on the stand-ins; the suite passes. |

## 2. The final check compares the layouts

**Contract.** `FinalReview` gains `pick: int = 1` (1-based index of the candidate to ship among those shown) and `reasons: list[str] = []` (one line per candidate, in order). `AutoState` gains `picked_layout: str = ""` (the `describe()` of the chosen composition).

**Agent.** `FINAL_CHECK_PROMPT` becomes a comparison: the message carries every candidate of the compose run (at most three), each after the line `Post 1`, `Post 2`, `Post 3`, then the kit's quality bar after `The brand's quality bar.`; the instruction says: compare them for readability of the words over the picture, the logo's clarity, how the layout serves the photo, and the brand's feel; set `pick` to the one that should ship; set `ship` and `score` for that one; write `reasons` as one line per post in order; name the biggest flaw of the pick and a one-line fix. The stand-in answers `pick=1`, `reasons=["Demo mode: no model compared the posts."] * n`.

**Run.** `final_check` sends one call with all candidates. `pick` out of range or a failed call: the first candidate, with the decision `Final check skipped: {reason}.` as today. Decision line: `Final check: picked {describe(composition)} ({score} of 5). Biggest flaw: {biggest_flaw}` (with `, would not ship as it is` after the score when `ship` is false). `auto_state.picked_layout` is set. The summary line is unchanged.

**Pages.** The finished panel shows `Final check picked {picked_layout}.` above the existing final-check line. The run page's `final_check` step note holds the decision line.

## 3. Framing and layout follow the brief

**The centring force is in code.** `with_backdrop` in `studio/workflows/shared.py` appends `… with the subject centred and empty space around it.` to every photo prompt. Change it to `with_backdrop(photo_prompt, mode, kit, layout_hint)`: always `Set on a seamless, evenly lit {mode} backdrop close to the colour {background}.`; add `, with the subject centred and empty space around it` only for the hint `hero` or `type_only`; add `Keep the {top|bottom} third of the frame plain, backdrop only.` for `full_bleed` and `caption_strip` (bottom unless the prompt version says otherwise); add `Place the subject in the lower right of the frame.` for `corner`; nothing extra for `split`. Every caller passes the current version's layout.

**The prompt writer** (`PROMPT_WRITER_PROMPT`):
- Replace "Choose the layout hero unless the brief asks for words only, which is type_only." with: `Choose layout from allowed_layouts to fit the brief: hero for one object on its own; full_bleed for a scene or a person; split for a product with a message; corner for a detail or a close-up; caption_strip for a wide scene; type_only for words only. Do not choose hero by habit.`
- Add: `Choose a framing that fits the brief and the layout: a close detail, a wider environmental shot, hands at work, a person, or a still life. The brand's ideal example sets the bar for finish and restraint; do not copy its composition.`
- The draft and revise contexts gain `allowed_layouts` (from `allowed_templates(kit)`).

**The stand-in** keeps its template but reads `allowed_layouts` and picks `full_bleed` when the brief mentions a person, scene, room or clinic, else `hero`, so demo runs vary too.

## 4. The quality bar in the studio

**Contract.**

```python
QualityBarSource = Literal["kit", "post", "sample", "upload"]

class QualityBar(BaseModel):
    source: QualityBarSource
    image_path: str          # relative to the data folder; the kit's file is not copied
    label: str = ""          # "a post", "a photo from session …", the upload's name
    set_at: datetime = Field(default_factory=now)
```

**Store.** A `settings` table (`key TEXT PRIMARY KEY, json TEXT`) with `get_setting(key) -> dict | None` and `set_setting(key, value: BaseModel | None)`; `quality_bar_dir = data_dir / "quality-bar"` created by `init()`. The bar is saved under the key `quality_bar`; a chosen image is copied into `quality_bar_dir / "{new_id()}.png"` so archive deletes never break it.

**One reader.** `quality_bar(store, kit) -> tuple[Path | None, str]` in `studio/workflows/shared.py`: the setting's file when it exists, labelled `set by you from {label}`; else the kit's `examples/ideal-output.png` when it exists, labelled `the kit's example`; else `(None, "none")`. It replaces the three readers today (`_ideal_example` in the draft images, `_quality_bar` for the critic, the final check in `auto.py`). The draft's `load_context` note ends with `Quality bar: {label}.`; the samples run's `review_samples` note too.

**Routes.**

| Method and path | Does |
|---|---|
| `GET /library` | The page gains a `Quality bar` panel at the top: the image, the line `The brand's ideal example from the kit.` or `Set by you from {label} on {date}.`, an upload field `Upload an image` (PNG or JPEG, 15 MB, the same checks as the session upload) and, when a designer's bar is set, `Back to the kit's example`. |
| `POST /quality-bar/upload` | multipart `image` → copy into `quality_bar_dir`, save the setting with source `upload`; refuse with `That file is not an image the studio can use.` |
| `POST /quality-bar/from-post/{post_id}` | copies the post's PNG; label `a post`; redirect back to the referring page |
| `POST /quality-bar/from-sample/{sample_id}` | copies the sample's photo; label `a photo`; redirect back |
| `POST /quality-bar/reset` | deletes the setting (the copied file may stay); redirect to the library |

**Pages.** `Set as the quality bar` as a small button on the post page (beside Approve), on every sample card with a photo, and on every archive card. The session page shows a thumbnail with the caption `Quality bar` beside the brief. The run page shows nothing new; the step notes carry the label.

## 5. The Editor tab

**Navigation.** `Editor` after `Archive` in the top bar.

**Routes.**

| Method and path | Does |
|---|---|
| `GET /editor` | The picker page: `Start from an upload` (a file field and a button), `Start from a post` (the posts as a contact sheet), `Start from a photo` (the archive's photos with an image, newest first). Each card is a form. |
| `POST /editor/start` | Fields: one of `post_id`, `sample_id`, or the file `photo`. Creates a session (brief `""`, `source_sample_id` for a sample, the post's `sample_id` for a post), copies the photo in with `pick_source_photo` (an upload becomes a round-0 upload sample as on the session page), and redirects to `/sessions/{id}/editor?sample={copy.id}` (with `from=` the post's chosen layout when starting from a post, so the arrangement is restored). A post with no photo (words only) is not offered. |

Reuse the archive's `/archive/{id}/edit` helper rather than a second copy of it; that route may stay as an alias.

## 6. More ways to add references

**Upload.** `POST /library/upload`, multipart field `images` (several files; each PNG or JPEG, 15 MB): each is saved into `data/inbox/` under a server-chosen name, then the inbox import runs as today (`FolderSource` plus `import_references`), and the analysis job starts. Refused files are listed in the import summary (`2 added, 1 refused: notes.txt is not an image the studio can use.`).

**Links.** `POST /library/import-links`, a textarea `links` (one link per line, at most 20): a new `LinkListSource(urls)` in `studio/library/sources.py` yields a `SourceItem` per `http(s)` link (label from the last path part, category `pasted`), then `import_references` with the HTTP fetcher; a link that fails to fetch is `unavailable` with the reason, as the board's links are. The page shows, under the two forms: `Only add images you are allowed to use. A reference shapes the prompt; it is never copied into a post.`

**Page texts.** `Upload images`, `Paste image links`, `One link per line, up to 20.`, the note above.

## 7. The parked minors

| # | Defect | Fix |
|---|---|---|
| a | The upload file button follows the system theme, not the page toggle | `color-scheme: light` on `.upload-row input[type=file]`, `dark` under `:root[data-theme="dark"]` and inside the dark `prefers-color-scheme` block |
| b | A mid-run reload can drop a sample comment being typed | `pollSession` skips the reload while a `[data-sample-comment]` has focus and tries again next tick |
| c | A Stop in the last moments leaves a saved post under an interrupted run, without its summary | In `_back_to_manual`, when the session has a post whose `run_id` is a child of this auto run and `auto_summary` is empty, set the summary and record the decision `Stopped after the post was saved.` |
| d | `1 layouts ready.` and the manual-mode texts `Making {n} samples…`, `{n} of {count} samples made.` do not pluralise | use `plural()` |
| e | The finished panel's line does not say when the checker would not ship | `Final check: {score} of 5, would not ship as it is. …` (section 2 covers it) |
| f | The custom contrast check decodes the photo once per text block | decode once, measure per block |
| g | After a hard restart, an interrupted run's steps stay `running` | `mark_interrupted_runs` ends every running step of a run it marks, with the error `The studio restarted while this step was in progress.` |
| h | A hand-made preview body without a logo reopens without the logo the render added | `editor_preview` stores the guarded layout as the candidate's composition |

## 8. Failure behaviour

| What fails | What the studio does | What the designer sees |
|---|---|---|
| The comparison call fails or picks out of range | the first candidate ships | `Final check skipped: …` in the decisions |
| The quality bar's copied file is missing | the kit's file is used | the label says `the kit's example` |
| A pasted link is not an image or cannot be fetched | that reference is `unavailable` with the reason | the import summary lists it |
| The Editor tab's upload is refused | the picker page shows the error | `That file is not an image the studio can use.` |

## 9. Implementation steps (the build session)

1. **Contracts** (`studio/contracts.py`): `FinalReview.pick`, `FinalReview.reasons`, `AutoState.picked_layout`, `QualityBarSource`, `QualityBar`.
2. **Store** (`studio/store.py`): the `settings` table, `get_setting`, `set_setting`, `quality_bar_dir`; `mark_interrupted_runs` ends running steps (7g).
3. **Stand-ins and agents** (`studio/models.py`, `studio/workflows/agents.py`): the comparison final check, the writer's layout and framing rules, `allowed_layouts` in the stand-in's draft.
4. **Runs** (`studio/workflows/shared.py`, `session.py`, `auto.py`): `with_backdrop(…, layout_hint)`, `quality_bar()`, `allowed_layouts` in the contexts, the comparison step, `picked_layout`, 7c, 7d.
5. **Library** (`studio/library/sources.py`): `LinkListSource`.
6. **Web** (`studio/web/routes.py`, templates, `app.js`, `app.css`): the quality-bar panel and routes, the Editor tab and picker, the library upload and links, the finished-panel line, 7a, 7b, 7h; `studio/render/renderer.py` for 7f.
7. **A demo run-through** on the stand-ins, then one real auto session and one run of `scripts/make_samples.py` with three briefs to see the layouts vary.

## 10. Rules for the build session

- Everything runs in the container; the suite must pass; no new tests unless a change breaks one.
- The live app on port 8000 and `.env` are off limits to agents; the build session's controller restarts the live app after each checkpoint.
- No commits. Checkpoints are `git add -A && git write-tree` ids recorded in a ledger at `.superpowers/sdd/2026-10-07-design-studio-v3-1/progress.md`.
- The build session touches only `studio/` and `tests/`. `docs/`, `README.md`, `brands/` and `samples/` belong to the documentation session running at the same time.
- Person-facing texts verbatim from this document. Nothing brand-specific inside `studio/`.

## 11. Not in v3.1

The taste changes (review point 2), the scout agent and the ideation step (point 6), the embedding taste profile, per-session quality bars, editing the kit from the studio.
