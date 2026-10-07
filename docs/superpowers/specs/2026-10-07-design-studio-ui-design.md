# Design Studio UI: the Hybridge design system

Date: 2026-10-07. Status: proposed, waiting for Sahaj's go. Scope: the studio's own pages, not
the posts it makes.

## Why

The studio works, and its chrome is generic: a teal accent, a system font stack, a dark theme,
a coloured pill on every row. Hybridge has a design system of its own, the look of its treatment
estimator: calm, precise, Apple quiet, black ink on white cards over a soft porcelain canvas,
Inter everywhere, headings in deep Hybridge blue, colour only where it carries meaning, glass
for the navigation layer only. Sampreeth rejected the previous look as too gaudy. The studio
should look like a Hybridge tool, and that look is also the monotone, minimal one Sahaj wants
the project to carry.

The source is the `hybridge-design` skill on the development machine. Its rules are adopted
here in full; its files are not copied. The studio is a plain-CSS Jinja app, so the React
shell and Tailwind preset do not apply, and the tokens and rules are re-expressed in the
studio's own stylesheet. That keeps the public repo free of files from another project, and
lets the chrome take its colours from the active brand kit (D2).

## Goals and non-goals

Goals: every page restyled on the system; light only; one glass bar; two-tone page titles;
white surfaces; ink buttons; colour only for outcomes; Inter from the kit; every screen right at
1440 and 390; a "How it works" page that introduces the eight agents; new README screenshots.

Non-goals: no change to what any page does, saves or shows; no change to the post renderer,
the layouts or the editor's features; no new tests (the suite's 99 must stay green); no docked
titles, no sliding pill, no sheets (the studio has no dialogs); no Tailwind.

## Decisions

- **D1. Light only.** The theme toggle, the theme script in the base template and the dark
  token sets are removed. The system has no dark theme and says not to invent one.
- **D2. The chrome's tokens come from the active kit.** `brand.yaml` already names the roles
  the system uses: `heading_blue`, `ink`, `ink_soft`, `ink_faint`, `line`, `wash`, `porcelain`,
  `white`. The base template writes them as CSS variables on `:root`; a kit that lacks a role
  gets the system's value. The Hybridge kit yields the Hybridge system exactly; another kit
  tints the tool its own way. The status hues (success, warning, danger, live) are the
  system's and never come from a kit, so an outcome always reads the same.
- **D3. Type from the kit, offline.** An `@font-face` for the kit's family through the
  existing `/brand/font` route (upright and italic), with the system's fallback stack. For
  Hybridge that is Inter's variable build with the optical-size axis. No Google Fonts import:
  the container works without the internet. `font-variation-settings` is never set.
- **D4. Glass is the navigation layer only.** The top bar and the phone tab bar are glass.
  Every piece of content sits on a solid white surface.
- **D5. One primary action per view.** Ink fill, white text. Everything else is a white
  secondary button with a hairline border, a ghost button, or an underlined ink link.
- **D6. Colour only for outcomes.** A finished session or a succeeded run is success, a failed
  one danger, an interrupted one warning, a running one a live dot with a pulse. Every other
  state is neutral text or a neutral badge. The brand's blue is for headings only.
- **D7. Words.** No em dashes. The final check's note joins lines with "; " and the stage
  line on the session page with " · ". Sentence case and plain verbs stay as they are.
- **D8. A "How it works" page** at `/about`, linked from the right of the bar and from the
  README: the loop in three steps and the eight agents, each with a line icon, its job in one
  line, what it reads and what it returns. Deferred on 2026-10-07 by Sahaj: the existing pages
  are restyled first, and new pages follow this document when they are built.
- **D9. The bar's name is the kit's name followed by "Design Studio"**, "Hybridge Design
  Studio" for the Hybridge kit, so another kit shows its own name. Decided by Sahaj on
  2026-10-07.

## Tokens

| Token | Value | Use |
| --- | --- | --- |
| `--heading` | #0F4B7B (kit `heading_blue`) | page, section and panel titles |
| `--ink` | #18181B (kit `ink`) | body text, primary buttons, links, the active state |
| `--ink-soft` | #52525B (kit `ink_soft`) | secondary text |
| `--ink-faint` | #A1A1AA (kit `ink_faint`) | labels, quiet text, finished things, the title tail |
| `--ink-line` | #E4E4E7 (kit `line`) | borders and hairlines |
| `--ink-wash` | #F4F4F5 (kit `wash`) | hover, insets, neutral badges |
| `--canvas` | #F3F4F7 (kit `porcelain`) | the page, with three faint washes of light |
| `--white` | #FFFFFF (kit `white`) | surfaces |
| `--status-made` | #16A34A | done, success |
| `--status-noshow` | #F2B200 | warning, interrupted |
| `--status-cancelled` | #E5484D | errors, danger buttons |
| `--status-live` | #22C55E | running now, the pulse |
| `--ease-glass` | cubic-bezier(0.32, 0.72, 0, 1) | every transition |

Radii: bar 22px, tab bar 26px, surfaces 20px, cards and insets 16px, fields 12px, buttons,
badges, pills and segmented controls fully round.

Type: page title 30px at phone width and 36px from 640px, weight 500, line height 1.08,
tracking -0.03em, heading blue, with the tail in ink-faint on its own line. Page description
15px ink-soft. Section title 15px semibold heading blue, sentence case. Body 14 to 15px ink.
Meta 12 to 13px ink-soft or ink-faint. Field label 13px medium above the field. Buttons 14px
medium. Badges 11.5px medium. Numbers that line up get tabular figures. No tracked capitals.

Glass: white 60%, blur 22px, saturate 1.8, white 70% border, a 0.5px ink ring, a soft drop
and a bright inner top edge. Fallbacks: no blur support means more white; reduced transparency
means solid white.

Motion: `--ease-glass`; 150ms for colour, 200 to 300ms for size and position; nothing animates
on its own except the live pulse; every transition carries the reduced-motion override.

## The shell

- A bar floating 12px from the top in a fixed 72px slot, 60px tall, glass on a layer behind
  its contents. Left: the kit's lockup at 32px height, a hairline, then "Design Studio" in 14px
  semibold ink. Centre-right: Studio, Library, Archive, Editor, Runs as 14px medium items, the
  current one lifted on a white 92% pill. Right: "How it works" as a quiet link.
- Page content starts 84px down. The demo-mode and no-photo-source lines sit in a neutral
  inset under the bar, 13px ink-soft, never yellow.
- Below 768px the bar keeps the lockup and the current page's name, and the five items move to
  a glass tab bar floating 12px above the bottom, with 20px line icons and 10.5px labels,
  honouring the safe area.
- The canvas is the porcelain with three faint washes of light, as the system draws it.

## Page by page

**Studio.** Title "Start a session", tail "Describe the post you want." The form on one
surface: the brief as a textarea starting at 88px; the counters (photos made today, searches
this month) as one 13px meta line above it; Manual and Auto as a segmented control on a glass
track; "Research first" as a checkbox with its label; in auto mode the three limits as three
small fields in one row; "Samples a round" beside them; one primary, Start. Recent sessions
below as soft cards with 10px between them: the brief, the status as neutral meta, the time
in tabular figures; finished sessions faded; an empty state saying what to do first.

**Session.** Title "Session", tail the brief, balanced, at most two lines. Above the title,
only while a run is live, a small pill with a live dot and the stage line ("Making 3
samples…"); Stop as a danger-soft button beside it; "See the run" as a link. Static statuses
(drafted, reviewing, finished) are neutral meta, not pills. The brief textarea with "Draft
again" and "Research again" as secondary buttons. The quality bar as a labelled inset beside
the brief. The concept (audience, idea, feeling, offer, tone) as five facts inside an inset.
Research on its own surface: section title, the report in body type with its own line breaks,
the queries as pills, the sources as a dense numbered list with the domain in meta. Directions
as three soft cards, the recommended one lifted; "Use this direction" on the recommended card
is the view's primary; Edit as a link; "Other directions" and "Draft without a direction" as
secondary buttons under the cards. Prompt and words on one surface: fields per the system,
"Generate again" primary when samples are the next step. Feedback: a textarea and "Revise the
prompt with this feedback" as secondary. Samples as soft cards in a grid: the photo, the
verdict in body type, the scores as one tabular meta line, flags as warning badges, liked and
disliked as ghost icon buttons, the recommended card lifted with "Recommended" in meta. Layouts
as cards with "Use this layout" (secondary, the first card's primary), Adjust and "Edit this
layout" as links. "How it was made" as a dense log, one line per decision.

**Post.** Two columns from 900px, stacked below. Left: the post in an inset with the scores
as one tabular meta line, then Download PNG as the primary, "Change the photo" and "Open in the
editor" as secondary. Right: version and status as meta, the direction as a link, the caption in
an inset with "Copy caption" as a small secondary, the hashtags as ink links, the summary line,
the "Why it looks like this" disclosure, the change box with Revise as secondary, Approve and
"Set as the quality bar" as secondary, the session and run links as links.

**Editor.** The canvas on the left as it is, inside a surface; the settings column on the
right as a surface with the system's fields, labels and segmented controls for alignment;
Preview as the primary. The handles, colour circles and drag behaviour keep their own CSS. The
picker (choosing a post to edit) as soft cards.

**Library.** Title "Library", tail "References, taste and the quality bar." The quality bar on
its own surface with the image in an inset, the two explanatory lines as body and meta, Upload
as secondary, "Back to the kit's example" as a link. Imports on one surface: the two import
buttons as secondary, the upload and the pasted links side by side from 900px. References as
soft cards in a grid; liked and disliked as ghost icon buttons; a liked card gets an ink
hairline, a disliked one fades; the filters as small pills. Taste as a surface with facts.

**Archive.** Title "Archive", tail "Every post the studio made." Posts as soft cards in a
grid with the headline, the layout and the date as meta; filters as pills; "Start a session
from this post" as a link on the card.

**Runs.** Title "Runs", tail "Every run, every step." A table inside a surface: kind, brief,
status, started, took; sentence-case 12px column labels on a faint wash; hairlines between
rows; times tabular and right aligned; status as a live dot, success, danger or warning text.
Child runs indented under their parent.

**Run.** Title the run's kind in words ("Automatic session"), tail the brief. Status as meta
with the outcome colour. The steps as a dense log: one line per step with its name, status,
duration, provider and note; the newest decision lifted in an inset; an error in danger text.
Child runs as a list of links under the log.

**How it works (`/about`).** Title "How it works", tail "Eight agents, one post." Three
surfaces for the loop: Gather, Curate, Create, each with two sentences. Then eight soft cards,
one per agent: a 20px line icon, the name as the section title, its job in one line, and two
meta lines, "Reads" and "Returns", from DESIGN.md's table. A closing line links the README and
DESIGN.md.

## Components, old to new

| Today | After |
| --- | --- |
| `.panel` | surface: white, 20px radius, hairline, soft shadow, 24px padding (20px top) |
| `.btn` | secondary button: 36px, white, hairline, round |
| `.btn-primary` | primary button: ink fill, white text, one per view |
| `.btn-link`, `.run-link` | ink link with underline |
| `.btn-small` | the 32px size |
| `.status-pill`, `.status-*` | badge only for an outcome (success, warning, danger, live); otherwise meta text |
| `.pill-option`, `.pill-group`, `.choice-btn` | segmented control on a glass track |
| `.filter-btn` | small pill sized to its words |
| `.hint` | 12px ink-faint under the field |
| `.field-error`, `.warning` | 12px danger text; a warning badge where it is an outcome |
| `.lede` | page description, 15px ink-soft, max width 42rem |
| `.sample-card`, `.contact-card`, `.ref-card` | soft card: 16px radius, white, hairline, 10px apart; lifted, faded or live variants |
| `.run-list`, `.run-row` | a table on the Runs page; a dense log on the run page |
| `.empty` | empty state: what the space means and the next step |
| `.banner` | neutral inset line under the bar |
| `.top-bar`, `.top-nav`, `.wordmark` | the glass bar and the tab bar |
| `.theme-toggle` | removed |

The `ref-card` class name stays on reference cards, because a test counts it.

## Icons

Line icons in a 20-unit box, stroke 1.6, round caps and joins, `currentColor`, 18px in the
bar, 20px in the tab bar, 14px inline, as the skill's shell icons are drawn. A Jinja macro file
holds them. Five for the pages: Studio (a pen nib), Library (a grid of four), Archive (a box),
Editor (a frame with a handle), Runs (a list with a clock). Eight for the agents: analyst (an
eye), scout (a compass), direction writer (a signpost), prompt writer (a pen), critic (a
magnifier), ranker (three bars of falling height), judge (scales), final checker (a shield
with a tick). Each icon means one thing everywhere; no legends, a hover title is enough.

## Phones

Every page right at 390 wide. The bar keeps the lockup and the page name; the tab bar carries
the items. Columns stack; the sample and layout grids go to one column; names wrap rather than
truncate; an input gets its own line. The shell pads 16px at phone width and 24px from 640px,
and no page adds a second layer of padding.

## Implementation notes

- Files: `studio/web/static/app.css` rewritten section by section, keeping the editor's
  functional CSS (handles, colour circles, drag states); `studio/web/templates/base.html` for
  the shell, the token block and the font face; every template for the class changes;
  `studio/web/static/app.js` loses the theme code and gains nothing it does not need;
  `studio/web/routes.py` gains the `/about` route and a `chrome_tokens(kit)` helper for the
  base template; new `templates/about.html` and `templates/icons.html`.
- The renderer, its layouts and `base.css` are untouched; posts look exactly as before.
- Every `data-*` attribute the scripts and the tests rely on stays. The tests check copy and
  routes, and one class name, `ref-card`.
- The `NOTE_SEPARATOR` constant in the auto workflow and the stage line in the routes carry
  D7.
- Build one page at a time and look at each live at 1440 and 390 before the next.
- When all pages are done: recapture the seven README screenshots with the script in
  `data/screenshots.py`, add one of the How it works page, update the README's walkthrough,
  commit as "UI: the Hybridge design system" and push.

## Acceptance checks

- No blue button and no blue link anywhere; every heading in heading blue; body text ink.
- One primary button per view.
- No badge on a neutral row; colour only on finished, failed, interrupted and running.
- No dark theme, no toggle, no theme script.
- Inter is served from the kit through `/brand/font`; no request leaves the machine for a
  font.
- Glass only on the bar and the tab bar.
- No em dash in any page or note.
- Every page right at 1440 and 390, nothing cut off, nothing truncated.
- The suite's 99 tests pass; demo mode's banner line is present and readable.
- The seven README screenshots plus the How it works page, recaptured, and the README updated.

## Open questions

1. The bar's name. Decided: the kit's name followed by "Design Studio" (D9).
2. Whether `/` should become How it works for a first visitor, or stay the Studio page.
   Deferred with D8; the Studio page stays home.
3. Whether the bar condenses on scroll (60px to 52px) as the estimator's does. Proposed: yes if
   it costs a few lines of script, otherwise skipped.

## Effort

One implementer round with a review: about four hours, the stylesheet being most of it. No
risk to the posts, the workflows or the data.
