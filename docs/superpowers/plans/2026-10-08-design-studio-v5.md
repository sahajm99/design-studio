# Design Studio v5 plan: the designer's own images

Spec: [2026-10-08-design-studio-v5-design.md](../specs/2026-10-08-design-studio-v5-design.md).
UI rules: [2026-10-07-design-studio-ui-design.md](../specs/2026-10-07-design-studio-ui-design.md).

The contracts (`BlockKind`, the new `Block` fields, `Upload`, `SessionReference`, `EditorEdit`,
`EditorAnswer`, the `edit` run kind), the store's two tables and eight methods, the shared
upload helper and every route were written by the controller before the first task and applied
whole in Task 1, so later tasks never touch the contracts. The same process as v3 to v4: one
implementer per task, a code review of the task's diff, one fix round with a scoped re-review,
no new tests, the suite's 99 kept green, every page checked at 1440 and 390 in light and dark.

| Task | Spec part | What it builds |
| --- | --- | --- |
| 1 | B and C | Text and image blocks in the editor with add, move, resize with aspect kept, reorder and delete; the renderer draws them in order and checks contrast on text; guardrails cap the count, keep blocks on the canvas and refuse images outside the brand's uploads; `POST /editor/upload`, the uploads list, and the "Your uploads" strip on the Library page |
| 2 | A | Reference images on a session, up to six with notes, from the Studio form or the session page; the analyst cards each one; the prompt writer, the direction writer and the critic read them under the rule to draw on them and never copy |
| 3 | D | The editor's Ask mode: an editor agent that reads the request, the layout, the kit's rules and a preview picture and returns a fixed set of edit operations; code applies them, runs the guardrails, records an `edit` run, and the page shows a summary with Undo, or one question |

Order: 1, 2, 3. Each ships on its own; the repository is pushed when v5 is whole.
