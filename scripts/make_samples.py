"""The sample posts: a fixed set of briefs run through auto mode, with the scores that judged them.

Each brief starts an auto session on the running studio, one after the other so the free tiers
are not hammered. When a session's run ends, its post, the critic's scores of the post's photo
and the chosen layout's render report are read from the studio's store, and the post's image is
copied into the output folder. Once every brief is done, the folder gets a README with a table
of the posts and how each was made, and the same data as JSON.

Run it from the repo root, with the studio already running (`docker compose up`):

    docker compose run --rm --no-deps studio python scripts/make_samples.py

Inside the studio's own container, pass `--base-url http://localhost:8000`. The exit code is 0
when every brief made a post, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import httpx

from studio.contracts import Post, Sample, StudioSession
from studio.store import Store

POLL_SECONDS = 5
# The auto run saves its ending a moment before its last decision line, so a session still in
# auto mode once its run has ended is read again, for this long at most.
HANDBACK_SECONDS = 10

COLUMNS = (
    "#",
    "Brief",
    "Headline",
    "Layout",
    "Rounds / photos",
    "Critic (on brief · brand · craft · overall)",
    "Final check",
    "Contrast",
    "Post",
)
MADE = (
    "Made on {date} by scripts/make_samples.py through auto mode: {sample_count} photos a round, "
    "up to {max_rounds} rounds and {photo_budget} photos, stop score {stop_score}."
)
STARTED = "{number}/{total} {brief} → session {session_id}"
NOT_STARTED = "{number}/{total} {brief} → {problem}"
NO_SESSION = "Could not start a session: {error}"
NO_REDIRECT = "The studio answered {status} without starting a session."
NOT_ANSWERING = "The studio did not answer ({error}); trying again."
STILL_RUNNING = "Still running after {seconds:g} s; moving on."
NOT_IN_STORE = "The session is not in the store; check --db and --data."
NO_POST = "no post"
TIMED_OUT = "timed out"
SHIP = "{score} of 5, ship"
WOULD_NOT_SHIP = "{score} of 5, would not ship"
CHECK_SKIPPED = "skipped"
NOT_SCORED = "not scored"
NO_CONTRAST = "n/a"


@dataclass
class Row:
    """One brief and what auto mode made of it: the post, the scores that judged it, the steps."""

    number: int
    brief: str
    session_id: str = ""
    auto_run_id: str = ""
    trace: str = ""
    timed_out: bool = False
    rounds_done: int = 0
    photos_used: int = 0
    stopped_by: str | None = None
    decisions: list[str] = field(default_factory=list)
    # The session's last decision line, or why the brief has no session.
    last_line: str = ""
    final_score: int | None = None
    final_ship: bool | None = None
    biggest_flaw: str = ""
    post_id: str = ""
    post_image_path: str = ""  # relative to the studio's data folder
    image: str = ""  # the copy in the output folder, nn-slug.png
    headline: str = ""
    subline: str = ""
    layout: str = ""
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    auto_summary: str = ""
    on_brief: int | None = None
    brand_fit: int | None = None
    craft: int | None = None
    overall: float | None = None
    flags: list[str] = field(default_factory=list)
    template: str = ""
    text_contrast: float | None = None
    fits: bool | None = None
    scrim_added: bool = False


# ------------------------------------------------------------------------- run


def main(argv: list[str] | None = None) -> int:
    """Run every brief, write the README and the JSON, and print the table."""
    args = parse_args(argv)
    briefs = read_briefs(args.briefs) if args.briefs.is_file() else []
    if not briefs:
        sys.exit(f"No briefs in {args.briefs}.")
    if not args.db.is_file():
        sys.exit(f"No studio database at {args.db}.")
    store = Store(args.db, args.data)

    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        check_studio(client, args.base_url)
        args.out.mkdir(parents=True, exist_ok=True)
        rows = [
            run_brief(client, store, args, number, len(briefs), brief)
            for number, brief in enumerate(briefs, start=1)
        ]

    write_readme(rows, args)
    write_json(rows, args.out)
    print()
    print("\n".join(table_lines(rows)))
    return 0 if all(row.post_id for row in rows) else 1


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """The options, each with its default."""
    parser = argparse.ArgumentParser(
        description="Run the sample briefs through auto mode and write the posts and their scores."
    )
    parser.add_argument(
        "--briefs",
        type=Path,
        default=Path("scripts/sample-briefs.txt"),
        help="One brief per line; blank lines and lines starting with # are left out.",
    )
    parser.add_argument(
        "--out", type=Path, default=Path("samples"), help="Where the posts, README and JSON go."
    )
    parser.add_argument(
        "--base-url",
        default="http://studio:8000",
        help="The running studio; http://localhost:8000 inside its own container.",
    )
    parser.add_argument("--sample-count", type=int, default=2, help="Photos a round.")
    parser.add_argument("--max-rounds", type=int, default=2, help="The most rounds a session runs.")
    parser.add_argument(
        "--photo-budget", type=int, default=4, help="The most photos a session makes."
    )
    parser.add_argument(
        "--stop-score",
        type=float,
        default=4.2,
        help="The top sample's overall score that ends the rounds.",
    )
    parser.add_argument(
        "--timeout", type=float, default=900, help="Seconds to wait for each brief."
    )
    parser.add_argument(
        "--db", type=Path, default=Path("/app/db/studio.db"), help="The studio's database."
    )
    parser.add_argument(
        "--data", type=Path, default=Path("/app/data"), help="The studio's data folder."
    )
    return parser.parse_args(argv)


def read_briefs(path: Path) -> list[str]:
    """The briefs in the file, one per line, leaving out blank lines and lines starting with #."""
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def check_studio(client: httpx.Client, base_url: str) -> None:
    """Stop before anything is written when the studio does not answer."""
    try:
        client.get("/studio").raise_for_status()
    except httpx.HTTPError as error:
        sys.exit(f"The studio does not answer at {base_url}: {error}")


def run_brief(
    client: httpx.Client,
    store: Store,
    args: argparse.Namespace,
    number: int,
    total: int,
    brief: str,
) -> Row:
    """Run one brief through auto mode and read back what it made."""
    try:
        session_id = start_session(client, brief, args)
    except httpx.HTTPError as error:
        problem = NO_SESSION.format(error=error)
        print(NOT_STARTED.format(number=number, total=total, brief=short(brief), problem=problem))
        return Row(number, brief, last_line=problem)

    print(STARTED.format(number=number, total=total, brief=short(brief), session_id=session_id))
    if not wait_for_run(client, session_id, args.timeout):
        print(f"  {STILL_RUNNING.format(seconds=args.timeout)}")
        return Row(number, brief, session_id=session_id, timed_out=True)

    row = collect(store, number, brief, session_id, args.base_url)
    if row.post_id:
        row.image = copy_image(store, row, args.out)
    return row


# ---------------------------------------------------------------- the studio


def start_session(client: httpx.Client, brief: str, args: argparse.Namespace) -> str:
    """Start an auto session for the brief and return its id, the last part of the redirect."""
    form = {
        "brief": brief,
        "sample_count": args.sample_count,
        "mode": "auto",
        "max_rounds": args.max_rounds,
        "photo_budget": args.photo_budget,
        "stop_score": args.stop_score,
    }
    response = client.post("/sessions", data=form)
    if not response.has_redirect_location:
        raise httpx.HTTPStatusError(
            NO_REDIRECT.format(status=response.status_code),
            request=response.request,
            response=response,
        )
    return response.headers["location"].rstrip("/").rsplit("/", 1)[-1]


def wait_for_run(client: httpx.Client, session_id: str, timeout: float) -> bool:
    """Poll the session every 5 s until its run ends, printing the status line and the latest
    decision when they change. False when the run is still going after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    shown: dict[str, str] = {}
    while True:
        try:
            data = client.get(f"/api/sessions/{session_id}").raise_for_status().json()
        except httpx.HTTPError as error:
            show(shown, "status", NOT_ANSWERING.format(error=error))
        else:
            decisions = (data.get("auto_state") or {}).get("decisions") or []
            show(shown, "status", data.get("auto_status"))
            show(shown, "decision", decisions[-1] if decisions else None)
            run = data.get("run")
            if run is None or run.get("status") != "running":
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)


def show(shown: dict[str, str], key: str, text: str | None) -> None:
    """Print the text, indented, when it differs from the last one printed for its key."""
    if text and text != shown.get(key):
        print(f"  {text}")
        shown[key] = text


# ----------------------------------------------------------------- the store


def collect(store: Store, number: int, brief: str, session_id: str, base_url: str) -> Row:
    """What the session made, read from the store: its decisions, the post, the critic's review
    of the photo the post used and the chosen layout's render report."""
    row = Row(number, brief, session_id=session_id)
    session = handed_back(store, session_id)
    if session is None:
        row.last_line = NOT_IN_STORE
        return row

    runs = [run for run in store.list_runs_for_session(session_id) if run.kind == "auto"]
    if runs:
        row.auto_run_id = runs[0].id
        row.trace = f"{base_url.rstrip('/')}/runs/{runs[0].id}"
    state = session.auto_state
    if state is not None:
        row.rounds_done = state.rounds_done
        row.photos_used = state.photos_used
        row.stopped_by = state.stopped_by
        row.decisions = list(state.decisions)
        if state.final_review is not None:
            row.final_score = state.final_review.score
            row.final_ship = state.final_review.ship
            row.biggest_flaw = state.final_review.biggest_flaw
    row.last_line = row.decisions[-1] if row.decisions else ""

    post = store.get_post(session.post_id) if session.post_id else None
    if post is None:
        return row
    read_post(row, post)
    read_review(row, store.get_sample(post.sample_id) if post.sample_id else None)
    candidate_id = session.picked_candidate_id
    candidate = store.get_layout_candidate(candidate_id) if candidate_id else None
    if candidate is not None:
        row.template = candidate.composition.template
        row.text_contrast = candidate.render_report.text_contrast
        row.fits = candidate.render_report.fits
        row.scrim_added = candidate.render_report.scrim_added
    return row


def handed_back(store: Store, session_id: str) -> StudioSession | None:
    """The session as last saved, once the auto run has handed it back in manual mode."""
    deadline = time.monotonic() + HANDBACK_SECONDS
    session = store.get_session(session_id)
    while session is not None and session.mode == "auto" and time.monotonic() < deadline:
        time.sleep(1)
        session = store.get_session(session_id)
    return session


def read_post(row: Row, post: Post) -> None:
    """The post's words, layout and image."""
    row.post_id = post.id
    row.post_image_path = post.image_path
    row.headline = post.spec.headline
    row.subline = post.spec.subline
    row.layout = post.spec.layout
    row.caption = post.caption
    row.hashtags = list(post.spec.hashtags)
    row.auto_summary = post.auto_summary


def read_review(row: Row, sample: Sample | None) -> None:
    """The critic's scores and flags for the photo the post used, when it was scored."""
    if sample is None or sample.review is None:
        return
    row.on_brief = sample.review.on_brief
    row.brand_fit = sample.review.brand_fit
    row.craft = sample.review.craft
    row.overall = sample.review.overall
    row.flags = list(sample.review.flags)


def copy_image(store: Store, row: Row, out: Path) -> str:
    """Copy the post's PNG into the output folder as nn-slug.png, and return that name."""
    name = f"{row.number:02d}-{slugify(row.brief)}.png"
    shutil.copyfile(store.media_path(row.post_image_path), out / name)
    return name


# -------------------------------------------------------------------- output


def write_readme(rows: list[Row], args: argparse.Namespace) -> None:
    """The README: when and how the posts were made, the table, then a short section per post."""
    made = MADE.format(
        date=date.today().isoformat(),
        sample_count=args.sample_count,
        max_rounds=args.max_rounds,
        photo_budget=args.photo_budget,
        stop_score=args.stop_score,
    )
    lines = ["# Sample posts", "", made, "", *table_lines(rows)]
    for row in rows:
        if row.post_id:
            lines += ["", *post_section(row)]
    (args.out / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_json(rows: list[Row], out: Path) -> None:
    """The same data as a list of objects, for the tests of a future version."""
    text = json.dumps([asdict(row) for row in rows], indent=2, ensure_ascii=False)
    (out / "results.json").write_text(text + "\n", encoding="utf-8")


def table_lines(rows: list[Row]) -> list[str]:
    """The Markdown table: the header, then one line per brief."""
    lines = [table_line(COLUMNS), table_line(["---"] * len(COLUMNS))]
    return lines + [table_line(cells(row)) for row in rows]


def table_line(values: Sequence[str]) -> str:
    """One line of the table, with any pipe or line break in a cell made safe."""
    safe = (value.replace("|", "\\|").replace("\n", " ") for value in values)
    return "| " + " | ".join(safe) + " |"


def cells(row: Row) -> list[str]:
    """A brief's cells: its post and the scores, or why it made no post."""
    if not row.post_id:
        return [str(row.number), row.brief, NO_POST, "", "", "", final_check(row), "", ""]
    return [
        str(row.number),
        row.brief,
        row.headline,
        row.layout.replace("_", " ").capitalize(),
        f"{row.rounds_done} / {row.photos_used}",
        critic(row),
        final_check(row),
        contrast(row),
        f"[{row.image}]({row.image})",
    ]


def critic(row: Row) -> str:
    """The critic's scores for the photo, with its flags after them when it has any."""
    if row.overall is None:
        return NOT_SCORED
    scores = f"{row.on_brief} · {row.brand_fit} · {row.craft} · {row.overall}"
    flags = ", ".join(flag.replace("_", " ") for flag in row.flags)
    return f"{scores} ({flags})" if flags else scores


def final_check(row: Row) -> str:
    """The final check's score and verdict; or, with no post, why there is none."""
    if row.timed_out:
        return TIMED_OUT
    if not row.post_id:
        return row.last_line
    if row.final_score is None:
        return CHECK_SKIPPED
    return (SHIP if row.final_ship else WOULD_NOT_SHIP).format(score=row.final_score)


def contrast(row: Row) -> str:
    """The words' contrast over the photo, and whether a shade was added; n/a on solid colour."""
    if row.text_contrast is None:
        return NO_CONTRAST
    ratio = f"{row.text_contrast:.1f}"
    return f"{ratio} shade" if row.scrim_added else ratio


def post_section(row: Row) -> list[str]:
    """One post's section: the image, the caption with its hashtags, how it was made, the trace."""
    lines = [f"## {row.number}. {row.brief}", "", f"![{row.headline}]({row.image})", ""]
    if row.caption:
        lines += [row.caption, ""]
    if row.hashtags:
        lines += [" ".join(row.hashtags), ""]
    lines += ["### How it was made", ""]
    if row.auto_summary:
        lines += [row.auto_summary, ""]
    lines += [f"- {decision}" for decision in row.decisions]
    if row.trace:
        lines += ["", f"Trace: [{row.trace}]({row.trace})"]
    return lines


def slugify(brief: str) -> str:
    """The brief's first six words, lower-case and hyphenated, in letters and digits only."""
    words = re.findall(r"[a-z0-9]+", brief.lower())
    return "-".join(words[:6]) or "brief"


def short(brief: str) -> str:
    """The brief's first six words, with an ellipsis when it goes on."""
    words = brief.split()
    return " ".join(words[:6]) + (" …" if len(words) > 6 else "")


if __name__ == "__main__":
    sys.exit(main())
