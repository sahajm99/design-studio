"""The auto run: a whole session, from the brief to a finished post, with no click.

Code owns the limits: the rounds, the photo allowance, the stop score and the hard-flag rule.
The critic owns whether a round is good enough, what the prompt should change, which of the
composed layouts ships and whether it could ship as it is. Each stage is the
manual mode's own run, started as a child of the auto run (`parent_run_id`) and awaited in
place, so the Runs page shows the whole tree and a stage fails or stops just as it does by
hand. Every decision is one line in the session's `auto_state`, saved at once so the session
page shows it while the run goes on. The session's run in flight stays the auto run
throughout, and the session is manual again when the run ends, however it ends. With
research on (v4), the scout runs first as a stage, and the draft follows a direction from
the research that can be made as a generated photograph, or the brief alone when none can;
research that fails never fails the auto run.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from google.adk import Context, Event, Workflow
from google.adk.agents import LlmAgent
from google.adk.workflow import node
from google.genai import types
from pydantic import ValidationError

from studio.contracts import (
    HARD_FLAGS,
    AutoSettings,
    AutoState,
    Direction,
    FinalReview,
    LayoutCandidate,
    Post,
    RoundFeedback,
    RoundJudgement,
    Run,
    RunKind,
    Sample,
    SampleReview,
    StoppedBy,
    StudioSession,
)
from studio.models import describe_llm
from studio.photos.base import PhotoUnavailable
from studio.render import describe
from studio.research.base import SearchUnavailable
from studio.store import Store
from studio.workflows.agents import build_final_checker, build_judge
from studio.workflows.layouts import run_compose, run_finish
from studio.workflows.scout import can_be_made, run_scout
from studio.workflows.session import (
    _critic_context,
    run_draft,
    run_revise_prompt,
    run_samples,
)
from studio.workflows.shared import (
    EMPTY_ANSWER,
    NO_POST_SAVED,
    Deps,
    current_version,
    image_part,
    plural,
    quality_bar,
    quality_bar_parts,
    run_workflow,
    saved_post,
    session_products,
    settle,
    text_message,
    update_session,
)
from studio.workflows.steps import StepInfo, StepRecorder

logger = logging.getLogger(__name__)

_JUDGE_ATTEMPTS = 2
# The most layouts the final check compares at once: as many as a compose run renders.
_MAX_COMPARED = 3
# A change that names text, lettering or a logo: the studio sets the words, so it is dropped.
_TEXT_ASK_RE = re.compile(
    r"\b(text|texts|typography|typographic|lettering|letters|logo|logos|font|fonts|slogan|"
    r"signage|sign|watermark|wordmark|words|headline|typeface|signboard)\b",
    re.IGNORECASE,
)

PLANNED = "Up to {rounds} and {photos}, stop at {stop_score}."
SKIPPED = "Skipped."
BRIEF_NEEDS_WORK = "Stopped: the brief needs work."
RESEARCH_DONE = 'Research: {sources}, {directions}; chose {number} "{title}". {reason}'
RESEARCH_UNGROUNDED = 'Research: not grounded ({problem}); chose {number} "{title}".'
RESEARCH_OFF = "Research: off."
RESEARCH_FAILED = "Research failed ({reason}); drafting from the brief alone."
# The reason in the line above for an unexpected error; its type goes to the log only.
RESEARCH_STEP_FAILED = "The research step failed."
# Ends the research line in place of the recommended reason when another direction was taken.
RECOMMENDED_NEEDS_PHOTO = "The recommended direction needs your own photo."
RECOMMENDED_IS_WORDS = "The recommended direction is words only."
# The research line when no direction can be made as a generated photograph, grounded or not.
RESEARCH_NO_PICTURE = (
    "Research: {sources}, {directions}; none can be made as a generated photograph, so "
    "drafting from the brief alone."
)
RESEARCH_UNGROUNDED_NO_PICTURE = (
    "Research: not grounded ({problem}); none of the {directions} can be made as a generated "
    "photograph, so drafting from the brief alone."
)
DRAFTED = "Version {number} drafted."
MAKING_ROUND = "Round {round} of {rounds}: making {photos}…"
REVISING_PROMPT = "Round {round} of {rounds}: revising the prompt…"
PHOTOS_FAILED = "Stopped: {error} Composing with the best photo so far."
# v6: a paid model that failed or reached a limit hands the rest of the session to a free one.
FINISHING_ON_FREE = "Round {round}: {model} stopped ({reason}); finishing on {fallback}."
# v6 Part B: never with product photos when the free model is words only, since it would
# invent the product.
NOT_FINISHING_ON_WORDS = (
    "Round {round}: {model} stopped ({reason}); not finishing on {fallback}, which cannot see "
    "product photos and would invent the product."
)
NOT_JUDGED = "Round {round}: the critic could not judge this round, so it was composed as it is."
GOOD_ENOUGH = "Round {round}: top {top} of 5, good enough, composing."
ROUNDS_LIMIT = "Round {round}: top {top} of 5, rounds limit reached, composing."
BUDGET_USED = "Round {round}: top {top} of 5, photo allowance used up, composing."
REVISING_UNDER = "Round {round}: top {top} under {stop}, revising: {changes}."
REVISING_FLAGGED = (
    "Round {round}: the critic says good enough, but the top sample has the {flag} flag; "
    "revising: {changes}."
)
REVISING_NOT_GOOD = "Round {round}: not good enough ({reason}), revising: {changes}."
# Added to the prompt's changes when one asked for words in the photo; alone when none is left.
KEEP_PHOTO_PLAIN = (
    "Keep the photograph free of text, lettering and logos; the studio adds the words."
)
COMPOSING = "Composing sample {index} of round {round}."
NO_PHOTO_TO_COMPOSE = "No photo could be made, so there is nothing to compose."
NO_LAYOUT = "No layout could be rendered."
LAYOUTS_READY = "{layouts} ready."
CHECK_SKIPPED = "Final check skipped: {reason}."
FINAL_CHECK = "Final check: picked {layout} ({score} of 5). Biggest flaw: {flaw}"
# The flaw the line names when the critic named none.
NO_FLAW_NAMED = "none named"
WOULD_NOT_SHIP = (
    "Final check: picked {layout} ({score} of 5), would not ship as it is. Biggest flaw: {flaw}"
)
FINAL_CHECK_FAILED = "The critic could not check the finished post."
PICK_NOT_SHOWN = "The critic picked post {pick}, which was not shown."
POST_SAVED = "Saved version {version} of the post."
SUMMARY = "Made automatically: {rounds}, {photos}, final check {score} of 5."
SUMMARY_UNCHECKED = "Made automatically: {rounds}, {photos}, final check skipped."
STOPPED_BY_YOU = "Stopped by you."
STOPPED_WITH = "Stopped: {error}"
STOPPED_AFTER_POST = "Stopped after the post was saved."
JUDGE_THIS_ROUND = "Judge this round."
POST_NUMBER = "Post {number}"
# The critic's line on each post it compared, after the decision line in the step's note.
POST_REASON = "Post {number}: {reason}"
# Every piece of the final check's note is a sentence, so one space joins them.
NOTE_SEPARATOR = " "


# ------------------------------------------------------------------------- run


async def run_auto(deps: Deps, run: Run, session: StudioSession) -> Post | None:
    """Make the session's post on its own: the draft, rounds of samples the critic judges, the
    layouts, the final check and the post.

    Returns the post, or None when the run stopped at the brief or failed. The outcome is
    recorded on the run and each decision on the session, which is manual again afterwards,
    however the run ended, with everything made so far. Like every run, it raises only when
    cancelled (the designer's Stop): the stage in flight and the auto run are then recorded as
    interrupted and the CancelledError raised again.
    """

    async def work() -> Post | None:
        state = await run_workflow(_auto_workflow(deps, run, session), run.id, session.brief)
        return saved_post(deps.store, state) if state.get("post_id") else None

    try:
        return await settle(deps.store, run, work, session)
    finally:
        # After settle, so a failed or stopped run hands the session back too.
        _back_to_manual(deps.store, session, run.id)


def _back_to_manual(store: Store, session: StudioSession, run_id: str) -> None:
    """Make the session manual again. A run that was stopped or failed says so in its last
    decision lines, whatever the loop had decided: stopped by the designer, or failed with
    its error. When it ended after its finish stage saved the post but before the post got
    its summary, the post gets it now and a last line says the post was saved. Never raises."""
    try:
        latest = _latest(store, session)
        auto_state = latest.auto_state or AutoState()
        run = store.get_run(run_id)
        ending: tuple[str, StoppedBy] | None = None
        if run is not None and run.status == "interrupted":
            ending = (STOPPED_BY_YOU, "designer")
        elif run is not None and run.status == "failed":
            ending = (STOPPED_WITH.format(error=run.error), "error")
        if ending is not None and auto_state.decisions[-1:] != [ending[0]]:
            auto_state = _with_decision(auto_state, ending[0], stopped_by=ending[1])
        post = _unsummarised_post(store, latest, run_id)
        if post is not None:
            post.auto_summary = _summary(auto_state)
            store.save_post(post)
            auto_state = _with_decision(auto_state, STOPPED_AFTER_POST)
        update_session(store, session, mode="manual", auto_state=auto_state)
    except Exception:
        logger.exception("Could not end auto mode on session %s", session.id)


def _unsummarised_post(store: Store, session: StudioSession, run_id: str) -> Post | None:
    """The session's post when a stage of this auto run saved it and it has no summary yet:
    the run ended between its finish stage and the summary."""
    post = store.get_post(session.post_id) if session.post_id else None
    if post is None or post.auto_summary:
        return None
    stage = store.get_run(post.run_id)
    return post if stage is not None and stage.parent_run_id == run_id else None


# -------------------------------------------------------------------- workflow


def _auto_workflow(deps: Deps, run: Run, session: StudioSession) -> Workflow:
    """plan → research → draft → round_loop → compose → final_check → finish → report.

    Once a step has ended the run early (`auto_done`), every later step passes through.
    """
    store = deps.store
    recorder = StepRecorder(store, run.id)
    judge = build_judge(deps.llm)
    checker = build_final_checker(deps.llm)

    async def plan() -> Event:
        async with recorder.step("plan") as info:
            settings = _latest(store, session).auto_settings or AutoSettings()
            update_session(
                store, session, mode="auto", auto_settings=settings, auto_state=AutoState()
            )
            info.note = PLANNED.format(
                rounds=plural(settings.max_rounds, "round"),
                photos=plural(settings.photo_budget, "photo"),
                stop_score=settings.stop_score,
            )
            return Event(output=settings.model_dump(mode="json"))

    async def research() -> Event:
        async with recorder.step("research") as info:
            latest = _latest(store, session)
            if not (latest.research_on and deps.kit.research.enabled):
                _record(store, session, RESEARCH_OFF)
                info.note = RESEARCH_OFF
                return Event(output={"research": False})
            try:
                line, question = await _research(deps, run, latest)
            except Exception as error:
                # Research that fails never fails the auto run: the draft goes on from the brief.
                reason = _research_problem(error)
                line, question = RESEARCH_FAILED.format(reason=reason), None
            if question is not None:
                _record(store, session, BRIEF_NEEDS_WORK, stopped_by="brief")
                info.note = question
                return Event(output={"auto_done": True}, state={"auto_done": True})
            _record(store, session, line)
            info.note = line
            return Event(output={"research": True})

    async def draft(auto_done: bool = False) -> Event:
        async with recorder.step("draft") as info:
            if auto_done:
                return _skipped(info)
            stage = _stage(deps, run, session, "draft")
            latest = await run_draft(deps, stage, _latest(store, session))
            _raise_if_failed(store, stage)
            if latest.status == "needs_brief":
                _record(store, session, BRIEF_NEEDS_WORK, stopped_by="brief")
                info.note = latest.question
                return Event(output={"auto_done": True}, state={"auto_done": True})
            version = current_version(store, latest)
            info.note = DRAFTED.format(number=version.number)
            return Event(output={"prompt_version_id": version.id})

    async def round_loop(ctx: Context, auto_done: bool = False) -> Event:
        async with recorder.step("round_loop") as info:
            if auto_done:
                return _skipped(info)
            info.provider = describe_llm(deps.llm)
            await _run_rounds(ctx, deps, run, session, judge, info)
            best = _best_sample(store, session)
            info.note = COMPOSING.format(index=best.index, round=best.round)
            return Event(output={"best_sample_id": best.id}, state={"best_sample_id": best.id})

    async def compose(best_sample_id: str = "", auto_done: bool = False) -> Event:
        async with recorder.step("compose") as info:
            if auto_done:
                return _skipped(info)
            best = store.get_sample(best_sample_id)
            if best is None:
                raise ValueError(NO_PHOTO_TO_COMPOSE)
            latest = _latest(store, session)
            stage = _stage(deps, run, latest, "compose")
            candidates = await run_compose(deps, stage, latest, best)
            _raise_if_failed(store, stage)
            if not candidates:
                raise ValueError(NO_LAYOUT)
            ids = [candidate.id for candidate in candidates]
            info.note = LAYOUTS_READY.format(layouts=plural(len(ids), "layout"))
            return Event(output={"candidate_ids": ids}, state={"candidate_ids": ids})

    async def final_check(
        ctx: Context, candidate_ids: list[str] | None = None, auto_done: bool = False
    ) -> Event:
        async with recorder.step("final_check") as info:
            if auto_done:
                return _skipped(info)
            info.provider = describe_llm(deps.llm)
            found = (store.get_layout_candidate(i) for i in candidate_ids or [])
            candidates = [candidate for candidate in found if candidate is not None]
            if not candidates:
                raise ValueError(NO_LAYOUT)
            chosen, note = await _final_check(ctx, checker, deps, session, candidates)
            info.note = note
            data = {"chosen_candidate_id": chosen.id}
            return Event(output=data, state=data)

    async def finish(chosen_candidate_id: str = "", auto_done: bool = False) -> Event:
        async with recorder.step("finish") as info:
            if auto_done:
                return _skipped(info)
            candidate = store.get_layout_candidate(chosen_candidate_id)
            if candidate is None:
                raise ValueError(NO_LAYOUT)
            latest = _latest(store, session)
            stage = _stage(deps, run, latest, "finish")
            post = await run_finish(deps, stage, latest, candidate)
            _raise_if_failed(store, stage)
            if post is None:
                raise ValueError(NO_POST_SAVED)
            post.auto_summary = _summary(_latest(store, session).auto_state or AutoState())
            store.save_post(post)
            info.note = POST_SAVED.format(version=post.version)
            return Event(output={"post_id": post.id}, state={"post_id": post.id})

    async def report(post_id: str = "", auto_done: bool = False) -> Event:
        async with recorder.step("report") as info:
            if auto_done:
                # Only the brief ends the run early, and the run page's last line says so.
                return _skipped(info, note=BRIEF_NEEDS_WORK)
            info.note = _summary(_latest(store, session).auto_state or AutoState())
            update_session(store, session, mode="manual")
            return Event(output={"post_id": post_id})

    return Workflow(
        name="auto",
        edges=[
            (
                "START",
                plan,
                research,
                draft,
                # Steps that call ctx.run_node must be rerunnable, as ADK requires.
                node(round_loop, name="round_loop", rerun_on_resume=True),
                compose,
                node(final_check, name="final_check", rerun_on_resume=True),
                finish,
                report,
            )
        ],
    )


def _skipped(info: StepInfo, note: str = SKIPPED) -> Event:
    """A step passing through, since an earlier step ended the run."""
    info.note = note
    return Event(output={"skipped": True})


# -------------------------------------------------------------------- research


async def _research(deps: Deps, run: Run, session: StudioSession) -> tuple[str, str | None]:
    """Run the scout as a stage and take a direction from it for the draft to follow.

    Returns the decision line, and the question when the brief is not usable (the line is
    then not used). The direction taken can be made as a generated photograph, and is saved
    as the session's chosen direction; when none can, the draft follows the brief alone. A
    stage that failed, or that brought back no directions, gives the line that drafts from
    the brief alone.
    """
    store = deps.store
    stage = _stage(deps, run, session, "scout")
    after = await run_scout(deps, stage, session)
    failed = _failed(store, stage)
    if failed is not None:
        return RESEARCH_FAILED.format(reason=_clause(failed.error or "")), None
    if after.status == "needs_brief":
        return "", after.question
    research = after.research
    if research is None or not after.directions:
        # The scout's propose step says why no directions came back.
        notes = [event.note for event in store.list_events(stage.id) if event.step == "propose"]
        return RESEARCH_FAILED.format(reason=_clause(notes[-1] if notes else "")), None
    chosen = _auto_direction(after.directions, after.recommended_direction)
    update_session(store, session, chosen_direction=chosen)
    if chosen is None:
        # None can be made: the draft follows the brief alone, and the directions stay on the
        # session for the designer to read.
        directions = plural(len(after.directions), "direction")
        if research.status == "ungrounded":
            problem = _clause(research.problem)
            line = RESEARCH_UNGROUNDED_NO_PICTURE.format(problem=problem, directions=directions)
        else:
            sources = plural(len(research.sources), "source")
            line = RESEARCH_NO_PICTURE.format(sources=sources, directions=directions)
        return line, None
    if research.status == "ungrounded":
        line = RESEARCH_UNGROUNDED.format(
            problem=_clause(research.problem), number=chosen.number, title=chosen.title
        )
    else:
        # The recommended reason explains the recommended direction only; when auto mode
        # passed it over, the line says why instead: it is words only, or needs a real photo.
        recommended = next(
            (d for d in after.directions if d.number == after.recommended_direction), None
        )
        passed_over = recommended is not None and chosen.number != recommended.number
        words_only = recommended is not None and recommended.format == "statement"
        why_passed = RECOMMENDED_IS_WORDS if words_only else RECOMMENDED_NEEDS_PHOTO
        line = RESEARCH_DONE.format(
            sources=plural(len(research.sources), "source"),
            directions=plural(len(after.directions), "direction"),
            number=chosen.number,
            title=chosen.title,
            reason=why_passed if passed_over else after.recommended_reason,
        ).strip()
    return line, None


def _auto_direction(directions: list[Direction], recommended: int | None) -> Direction | None:
    """The direction auto mode takes: the recommended one when it can be made as a
    generated photograph, else the first that can be; None when none can. A copy, so the
    session's list keeps its own."""
    pick = next((d for d in directions if d.number == recommended), directions[0])
    if not can_be_made(pick):
        pick = next((d for d in directions if can_be_made(d)), None)
    return pick.model_copy() if pick is not None else None


def _research_problem(error: Exception) -> str:
    """Why the research step could not be done, in plain words: the studio's own message for
    a ValueError or SearchUnavailable, else a fixed line, with the error's type in the log."""
    if isinstance(error, SearchUnavailable) or (
        isinstance(error, ValueError) and not isinstance(error, ValidationError)
    ):
        return _clause(str(error))
    # Only the type: the text of an unexpected error may hold a request address.
    logger.warning("The research step failed: %s", type(error).__name__)
    return RESEARCH_STEP_FAILED


# ---------------------------------------------------------------------- rounds


async def _run_rounds(
    ctx: Context, deps: Deps, run: Run, session: StudioSession, judge: LlmAgent, info: StepInfo
) -> None:
    """Make a round, have the critic judge it, and decide; until a decision stops the loop,
    revise the prompt from the critic's changes and make the next round.

    The limits are read again at the top of every pass, with the session as last saved. The
    step's note says which round is being made, or that the prompt is being revised for it.
    A round that makes no photo at all ends the loop when an earlier round made one, so the
    best photo so far is composed; with no photo from any round, the auto run fails. A paid
    model that fails or reaches a daily limit hands the rest of the session to the free
    default first, when the photo settings say so (v6), and a round that made no photo is
    made again on it.
    """
    store = deps.store
    while True:
        latest = _latest(store, session)
        settings = latest.auto_settings or AutoSettings()
        auto_state = latest.auto_state or AutoState()
        round_number = auto_state.rounds_done + 1
        left = settings.photo_budget - auto_state.photos_used
        count = max(1, min(latest.sample_count, left))
        info.note = MAKING_ROUND.format(
            round=round_number, rounds=settings.max_rounds, photos=plural(count, "photo")
        )
        store.update_step_note(info.event_id, info.note)

        stage = _stage(deps, run, latest, "samples")
        samples = await run_samples(deps, stage, latest, count=count)
        failed = _failed(store, stage)
        if _finish_on_free(deps, latest, round_number, samples, stage):
            if failed is not None:
                continue  # the round is made again, on the free model
        elif failed is not None:
            if not _has_photo(store, session):
                raise ValueError(failed.error)
            _record(store, session, PHOTOS_FAILED.format(error=failed.error), stopped_by="budget")
            return
        rounds_done = auto_state.rounds_done + 1
        photos_used = auto_state.photos_used + sum(1 for sample in samples if sample.image_path)

        top = _top_sample(samples)
        judgement = None
        if top is not None and top.review is not None:
            judged = _latest(store, session)  # as the round left it
            context = _judge_context(deps, judged, settings, round_number, samples, top)
            judgement = await _ask_judge(ctx, judge, context)
        line, stopped_by, changes = _decide(
            round_number, top, judgement, rounds_done, photos_used, settings
        )
        changed = {"rounds_done": rounds_done, "photos_used": photos_used, "stopped_by": stopped_by}
        _record(store, session, line, **changed)
        if stopped_by is not None:
            return
        # The revision prepares the next round, the one the session page now counts.
        info.note = REVISING_PROMPT.format(round=rounds_done + 1, rounds=settings.max_rounds)
        store.update_step_note(info.event_id, info.note)
        await _revise(deps, run, session, changes)


def _top_sample(samples: list[Sample]) -> Sample | None:
    """The round's top sample: the recommended one, else the one ranked first, else the best
    scored, else the first with a photo."""
    scored = [sample for sample in samples if sample.review is not None]
    picks = (
        next((sample for sample in samples if sample.recommended), None),
        next((sample for sample in samples if sample.rank == 1), None),
        max(scored, key=_overall, default=None),
        next((sample for sample in samples if sample.image_path), None),
    )
    return next((pick for pick in picks if pick is not None), None)


def _judge_context(
    deps: Deps,
    session: StudioSession,
    settings: AutoSettings,
    round_number: int,
    samples: list[Sample],
    top: Sample,
) -> dict[str, Any]:
    """What the judge reads: the round's scored samples best first, by rank and then by score,
    the top sample and the stop score, beside what the critic read. `top` is scored."""
    version = current_version(deps.store, session)
    scored = [sample for sample in samples if sample.review is not None]
    best_first = sorted(scored, key=lambda s: (s.rank is None, s.rank or 0, -_overall(s)))
    ranking = deps.store.get_round_review(session.id, session.rounds)
    return {
        "round": round_number,
        "stop_score": settings.stop_score,
        "top_index": top.index,
        "top_overall": _overall(top),
        "top_has_hard_flag": _review(top).has_hard_flag,
        "reviews": [_scored(sample) for sample in best_first],
        "ranking_note": ranking.note if ranking else "",
        **_critic_context(session, version, deps.kit),
    }


def _scored(sample: Sample) -> dict[str, Any]:
    """A scored sample as the judge reads it."""
    review = _review(sample)
    return {
        "index": sample.index,
        "rank": sample.rank,
        "overall": review.overall,
        "on_brief": review.on_brief,
        "brand_fit": review.brand_fit,
        "craft": review.craft,
        "flags": list(review.flags),
        "verdict": review.verdict,
        "suggested_change": review.suggested_change,
        # v6 Part B: how true the product stayed, when the round had product photos.
        "product_fidelity": review.product_fidelity,
    }


async def _ask_judge(
    ctx: Context, judge: LlmAgent, context: dict[str, Any]
) -> RoundJudgement | None:
    """The judge's answer on the round, asked for once more after a failure or an empty
    answer. None when both attempts failed."""
    ctx.state["judge_context"] = context
    for _ in range(_JUDGE_ATTEMPTS):
        try:
            answer = await ctx.run_node(
                judge, node_input=text_message(JUDGE_THIS_ROUND), use_sub_branch=True
            )
            if answer is not None:
                return RoundJudgement.model_validate(answer)
        except Exception as error:
            # Only the type: the text of an unexpected error may hold a request address.
            logger.warning("The judge could not judge a round: %s", type(error).__name__)
    return None


def _decide(
    round_number: int,
    top: Sample | None,
    judgement: RoundJudgement | None,
    rounds_done: int,
    photos_used: int,
    settings: AutoSettings,
) -> tuple[str, StoppedBy | None, list[str]]:
    """The round's one decision line, what stopped the loop (None when it goes on), and the
    changes the prompt should make when it goes on.

    The checks run in a fixed order: no judgement, good enough, the rounds limit, the photo
    allowance. Code holds the judge to its own rule: a top sample with a hard flag, or under
    the stop score, is never good enough. A change that asks for text, lettering or a logo in
    the photo is dropped, because the studio sets the words; when one is dropped, or nothing
    is left, the prompt is told to keep the photograph plain.
    """
    review = top.review if top is not None else None
    if judgement is None or review is None:
        return NOT_JUDGED.format(round=round_number), "error", []
    score, stop = f"{review.overall:.1f}", settings.stop_score
    if judgement.good_enough and not review.has_hard_flag and review.overall >= stop:
        return GOOD_ENOUGH.format(round=round_number, top=score), "score", []
    if rounds_done >= settings.max_rounds:
        return ROUNDS_LIMIT.format(round=round_number, top=score), "rounds", []
    if photos_used >= settings.photo_budget:
        return BUDGET_USED.format(round=round_number, top=score), "budget", []
    changes = _changes_for(judgement, review)
    listed = "; ".join(_clause(change) for change in changes)
    if review.overall < stop:
        line = REVISING_UNDER.format(round=round_number, top=score, stop=stop, changes=listed)
    elif judgement.good_enough:
        # Good enough at a passing score: only the hard flag held it back.
        flag = _hard_flag(review)
        line = REVISING_FLAGGED.format(round=round_number, flag=flag, changes=listed)
    else:
        reason = _clause(judgement.reason)
        line = REVISING_NOT_GOOD.format(round=round_number, reason=reason, changes=listed)
    return line, None, changes


def _changes_for(judgement: RoundJudgement, review: SampleReview) -> list[str]:
    """The changes the prompt should make: the judge's, else the critic's suggested change,
    less any that asks for text, lettering or a logo in the photo, since the studio sets the
    words. When one was dropped, the prompt is also told to keep the photograph plain, so a
    text flag still gets its fix; when none is left, that is the only change."""
    judged = _lines(judgement.changes)
    kept = _without_text_asks(judged)
    dropped = len(kept) < len(judged)
    if not kept:
        # The critic's suggested change stands in when the judge left none to keep.
        suggested = _lines([review.suggested_change])
        kept = _without_text_asks(suggested)
        dropped = dropped or len(kept) < len(suggested)
    if not kept:
        return [KEEP_PHOTO_PLAIN]
    if dropped and KEEP_PHOTO_PLAIN not in kept:
        return [*kept, KEEP_PHOTO_PLAIN]
    return kept


async def _revise(deps: Deps, run: Run, session: StudioSession, changes: list[str]) -> None:
    """Save the critic's changes as its feedback on the latest round, then revise the prompt
    from them in a revise_prompt stage."""
    store = deps.store
    latest = _latest(store, session)
    text = "\n".join(changes)
    store.save_round_feedback(
        RoundFeedback(session_id=session.id, round=latest.rounds, text=text, author="critic")
    )
    stage = _stage(deps, run, latest, "revise_prompt", comment=text)
    await run_revise_prompt(deps, stage, latest)
    _raise_if_failed(store, stage)


def _best_sample(store: Store, session: StudioSession) -> Sample:
    """The photo to compose: the latest round's recommended sample; else, over every round,
    the best scored with no hard flag; else the best scored; else any photo. Raises when no
    round made a photo."""
    latest = _latest(store, session)
    photos = [s for s in store.list_samples(session.id) if s.round >= 1 and s.image_path]
    scored = [sample for sample in photos if sample.review is not None]
    clean = [sample for sample in scored if not _review(sample).has_hard_flag]
    picks = (
        next((s for s in photos if s.round == latest.rounds and s.recommended), None),
        max(clean, key=_overall, default=None),
        max(scored, key=_overall, default=None),
        photos[0] if photos else None,
    )
    best = next((pick for pick in picks if pick is not None), None)
    if best is None:
        raise ValueError(NO_PHOTO_TO_COMPOSE)
    return best


def _has_photo(store: Store, session: StudioSession) -> bool:
    """Whether a round of the session has made a photo for `_best_sample` to compose."""
    return any(s.round >= 1 and s.image_path for s in store.list_samples(session.id))


def _finish_on_free(
    deps: Deps, session: StudioSession, round_number: int, samples: list[Sample], stage: Run
) -> bool:
    """When the round's model is paid and a photo of it failed or met a daily limit, make the
    rest of the session use the free default and say so in a decision line; True when it did
    (v6). Only with the photo settings' fallback on, and only for a failure making photos: a
    stage that failed after its photos were made keeps its model. Never a paid model.

    v6 Part B: never a words-only model for a session with product photos, since its photos
    would invent the product; the decision line says so, and the session goes on as it would
    with the fallback off."""
    registry = deps.photos
    if not registry.photo_settings().auto_fallback_to_default:
        return False
    failed = _failed(deps.store, stage)
    if failed is not None:
        steps = deps.store.list_events(stage.id)
        photo_step = any(e.step == "generate_samples" and e.status == "failed" for e in steps)
        reason = failed.error if photo_step else None
    else:
        reason = next((sample.error for sample in samples if sample.error), None)
    if not reason:
        return False
    try:
        used = registry.resolve(session.photo_model_id).model
    except PhotoUnavailable:
        return False
    fallback = registry.fallback_model()
    if not used.paid or fallback is None or fallback.id == used.id:
        return False
    if not registry.takes_photos(fallback) and session_products(deps.store, session.id):
        line = NOT_FINISHING_ON_WORDS.format(
            round=round_number, model=used.label, reason=_sentence(reason), fallback=fallback.label
        )
        _record(deps.store, session, line)
        return False
    update_session(deps.store, session, photo_model_id=fallback.id)
    line = FINISHING_ON_FREE.format(
        round=round_number, model=used.label, reason=_sentence(reason), fallback=fallback.label
    )
    _record(deps.store, session, line)
    return True


# ----------------------------------------------------------------- final check


async def _final_check(
    ctx: Context,
    checker: LlmAgent,
    deps: Deps,
    session: StudioSession,
    candidates: list[LayoutCandidate],
) -> tuple[LayoutCandidate, str]:
    """The layout to finish, and the step's note: the decision line, which is also saved,
    then the critic's line on each post, `Post {n}: {reason}`. Each is a sentence ending in
    a full stop (or its own `!` or `?`), joined by a space; a flaw the critic left blank
    reads "none named".

    The critic compares every layout of the compose run at once (at most three) and picks the
    one to ship; the session's auto state keeps its review and the picked layout's caption.
    When the check cannot be done, or the pick is not one of the layouts shown, the first
    layout is finished without a check, and the note is the decision line alone.
    """
    store = deps.store
    shown = candidates[:_MAX_COMPARED]
    try:
        review = await _ask_checker(ctx, checker, deps, session, shown)
        if review.pick > len(shown):
            raise ValueError(PICK_NOT_SHOWN.format(pick=review.pick))
    except Exception as error:
        line = CHECK_SKIPPED.format(reason=_clause(_check_problem(error)))
        _record(store, session, line, final_review=None)
        return candidates[0], line
    chosen = shown[review.pick - 1]
    layout = describe(chosen.composition)
    template = FINAL_CHECK if review.ship else WOULD_NOT_SHIP
    flaw = review.biggest_flaw.strip() or NO_FLAW_NAMED
    line = _sentence(template.format(layout=layout, score=review.score, flaw=flaw))
    _record(store, session, line, final_review=review, picked_layout=layout)
    reasons = [
        _sentence(POST_REASON.format(number=number, reason=reason.strip()))
        for number, reason in enumerate(review.reasons, start=1)
        if reason.strip()
    ]
    return chosen, NOTE_SEPARATOR.join([line, *reasons])


async def _ask_checker(
    ctx: Context,
    checker: LlmAgent,
    deps: Deps,
    session: StudioSession,
    candidates: list[LayoutCandidate],
) -> FinalReview:
    """The final checker's comparison of the rendered layouts, held to the quality bar: one
    call, each layout after a line with its number (Post 1, Post 2, ...), then the bar."""
    store, kit = deps.store, deps.kit
    latest = _latest(store, session)
    version = current_version(store, latest)
    ctx.state["final_check_context"] = {
        "concept": latest.concept.model_dump(mode="json") if latest.concept else None,
        "brand": {"name": kit.name, "feel": kit.feel},
        "words": {
            "headline": version.headline,
            "subline": version.subline,
            "caption": version.caption,
        },
        "post_count": len(candidates),
        "layouts": [describe(candidate.composition) for candidate in candidates],
    }
    parts: list[types.Part] = []
    for number, candidate in enumerate(candidates, start=1):
        parts += [
            types.Part(text=POST_NUMBER.format(number=number)),
            image_part(store.media_path(candidate.image_path)),
        ]
    bar, _ = quality_bar(store, kit)
    parts += quality_bar_parts(bar)
    # On a branch of its own, so the check sees only these posts.
    answer = await ctx.run_node(
        checker, node_input=types.Content(role="user", parts=parts), use_sub_branch=True
    )
    if answer is None:
        raise ValueError(EMPTY_ANSWER)
    return FinalReview.model_validate(answer)


def _check_problem(error: Exception) -> str:
    """Why the final check could not be done, in plain words."""
    if isinstance(error, ValueError) and not isinstance(error, ValidationError):
        return str(error)
    # Only the type: the text of an unexpected error may hold a request address.
    logger.warning("The final check failed: %s", type(error).__name__)
    return FINAL_CHECK_FAILED


# ------------------------------------------------------------------- helpers


def _latest(store: Store, session: StudioSession) -> StudioSession:
    """The session as last saved."""
    return store.get_session(session.id) or session


def _stage(deps: Deps, run: Run, session: StudioSession, kind: RunKind, comment: str = "") -> Run:
    """A new stage of the auto run: a run of its own for the session, linked to its parent.

    The session's run in flight stays the auto run, so a stage's ending never frees it.
    """
    return deps.store.create_run(
        kind,
        deps.kit.id,
        brief=session.brief,
        comment=comment,
        session_id=session.id,
        parent_run_id=run.id,
    )


def _failed(store: Store, stage: Run) -> Run | None:
    """The stage as last saved, when it failed; None when it did not."""
    finished = store.get_run(stage.id)
    return finished if finished is not None and finished.status == "failed" else None


def _raise_if_failed(store: Store, stage: Run) -> None:
    """Fail the auto run with the stage's own message when the stage failed."""
    failed = _failed(store, stage)
    if failed is not None:
        raise ValueError(failed.error)


def _record(store: Store, session: StudioSession, line: str, **changes: Any) -> None:
    """Add one decision line, with any other changes, to the session's auto state, and save
    it at once so the session page shows it."""
    auto_state = _latest(store, session).auto_state or AutoState()
    update_session(store, session, auto_state=_with_decision(auto_state, line, **changes))


def _with_decision(auto_state: AutoState, line: str, **changes: Any) -> AutoState:
    """A copy of the auto state with the changes and one more decision line."""
    updated = auto_state.model_copy(update=changes)
    updated.decisions = [*auto_state.decisions, line]
    return updated


def _summary(auto_state: AutoState) -> str:
    """The post's line on how it was made."""
    rounds = plural(auto_state.rounds_done, "round")
    photos = plural(auto_state.photos_used, "photo")
    review = auto_state.final_review
    if review is None:
        return SUMMARY_UNCHECKED.format(rounds=rounds, photos=photos)
    return SUMMARY.format(rounds=rounds, photos=photos, score=review.score)


def _lines(texts: list[str]) -> list[str]:
    """The texts that are not blank, trimmed."""
    return [text.strip() for text in texts if text.strip()]


def _without_text_asks(lines: list[str]) -> list[str]:
    """The lines that name no text, lettering, font, sign or logo: the studio sets the words
    after the photo is made, so a change about words in the photo never reaches the prompt."""
    return [line for line in lines if not _TEXT_ASK_RE.search(line)]


def _clause(text: str) -> str:
    """A sentence set inside another one: trimmed, without its closing full stop."""
    return text.strip().rstrip(".")


def _sentence(text: str) -> str:
    """The text as a sentence of its own: trimmed, and ending with a full stop unless it
    already ends with a full stop, an exclamation mark or a question mark."""
    trimmed = text.strip()
    return trimmed if trimmed.endswith((".", "!", "?")) else f"{trimmed}."


def _hard_flag(review: SampleReview) -> str:
    """The review's first hard flag, in words."""
    flag = next((flag for flag in review.flags if flag in HARD_FLAGS), "")
    return flag.replace("_", " ")


def _review(sample: Sample) -> SampleReview:
    """The review of a sample known to be scored."""
    assert sample.review is not None
    return sample.review


def _overall(sample: Sample) -> float:
    """A scored sample's overall score."""
    return _review(sample).overall
