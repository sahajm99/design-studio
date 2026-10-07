"""The scout run: web research before the draft, and three directions to choose from.

check_brief → plan_queries → search → report → propose → save_directions.

Code owns the order, the limits (the month's search credits, at most three searches and ten
sources), the diversity check and every fallback. The scout owns the searches and the
research prose; the direction writer owns the directions. Text from the web is untrusted:
it reaches the agents only inside their JSON context block. When no search can be made, or
it finds nothing, the research is not grounded and the directions draw on the kit's
occasions and the model's own knowledge; the run goes on. "Other directions" is a scout run
`from_research`: it starts at propose, on the session's saved research, and spends no search.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any, TypeVar

from google.adk import Context, Event, Workflow
from google.adk.agents import LlmAgent
from google.adk.workflow import node
from google.adk.workflow._errors import DynamicNodeFailError
from google.genai import types
from pydantic import BaseModel, ValidationError

from studio.contracts import (
    BrandKit,
    Direction,
    DirectionSet,
    LayoutTemplate,
    ResearchReport,
    ResearchSource,
    Run,
    ScoutReport,
    SearchQueries,
    SearchResult,
    StudioSession,
    now,
)
from studio.library.taste import build_taste_profile
from studio.models import describe_llm
from studio.render import allowed_templates
from studio.research.base import SearchUnavailable
from studio.store import Store
from studio.workflows.agents import (
    build_direction_writer,
    build_scout_planner,
    build_scout_reporter,
)
from studio.workflows.shared import (
    EMPTY_ANSWER,
    Deps,
    _first_validation_message,
    brand_block,
    mentions,
    plural,
    run_workflow,
    settle,
    text_message,
    update_session,
)
from studio.workflows.steps import StepInfo, StepRecorder

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)

_MIN_BRIEF_WORDS = 6
_MAX_QUERIES = 3
_RESULTS_PER_QUERY = 5
_MAX_SOURCES = 10
_ATTEMPTS = 2
_MAX_RETRY_NOTE_CHARS = 320
_RECENT = 10
# Tags that would close the context block early if web text carried them.
_CONTEXT_TAG_RE = re.compile(r"</?\s*context\s*>", re.IGNORECASE)
# A citation in the research prose, such as [3].
_CITATION_RE = re.compile(r"\[(\d+)\]")
# The session state keys the scout's and the direction writer's instructions read.
_SCOUT_NOTE = "scout_retry_note"
_DIRECTIONS_NOTE = "directions_retry_note"

# Step notes.
BRIEF_QUESTION = "Say what the post is about and who it is for, in a sentence or two."
BRIEF_READY = "The brief is ready for research."
WILL_SEARCH = "Will search: {queries}."
SEARCHED = "Searched {queries}; {sources}."
CITED = "{sources} cited."
PROPOSED = '{directions}; recommended {number}: "{title}".'
ALIKE = "Two of these directions are alike."
NO_DIRECTIONS = "No directions this time: {reason}"
SAVED = "Saved."
ASKED_FOR_BRIEF = "Asked for a better brief."
SKIPPED = "Skipped."
# Why the research is not grounded: the session page shows "Not grounded: {problem}".
ALLOWANCE_USED = "The monthly search allowance is used up."
NO_SEARCH_PROVIDER = "No search provider is configured."
NO_SOURCES = "The search found no sources."
SEARCH_FAILED = "The search service failed."
PLAN_FAILED = "The scout could not plan the searches."
NO_QUERIES = "The scout planned no searches."
REPORT_FAILED = "The scout could not write the research."
# Why no directions came back, or why the run could not start.
DIRECTIONS_INVALID = (
    f"The model's answer did not match the directions after {_ATTEMPTS} attempts."
)
DIRECTIONS_FAILED = "The model could not write the directions."
NO_RESEARCH = "This session has no research yet."
# What the direction writer is told when it is asked again.
SAME_FORMAT = (
    "Directions {a} and {b} are the same kind of picture ({format}). Make each one different."
)
SAME_LAYOUT = (
    "All three directions use the {layout} layout. Use at least two layouts from "
    "allowed_layouts."
)
THREE_DIRECTIONS = "Write exactly three directions."
NO_PICTURE = (
    "No direction can be made as a generated photograph. Make at least two of them pictures "
    "that need no real photo, with subjects that could be anywhere."
)
# Ends the propose step's note, like ALIKE, when the set still has no picture that can be made.
NONE_CAN_BE_MADE = "None of these directions can be made as a generated photograph."


class AnswerInvalid(ValueError):
    """An agent's answers did not fit its schema. The message is the last problem found."""


# ------------------------------------------------------------------------- run


async def run_scout(
    deps: Deps, run: Run, session: StudioSession, *, from_research: bool = False
) -> StudioSession:
    """Research the brief on the web and propose three directions, saved on the session for
    the designer to choose from (status `choosing`).

    A brief under six words gets its question back before any search is spent, and the
    session waits for a better brief; so does a brief the direction writer cannot work with.
    With `from_research` ("Other directions") the run starts at propose, on the session's
    saved research. When no directions come back, the session is `choosing` with none, and
    the propose step's note says why. Returns the session as saved at the end, also when the
    run failed; the outcome is recorded on the run. Like every run, it raises only when
    cancelled.
    """

    async def work() -> None:
        if from_research:
            if session.research is None:
                raise ValueError(NO_RESEARCH)
            state: dict[str, Any] = {"research": session.research.model_dump(mode="json")}
        else:
            state = await run_workflow(_check_workflow(deps, run, session), run.id, session.brief)
            if not state.get("brief_ready"):
                return
        workflow = _scout_workflow(deps, run, session, from_research=from_research)
        await run_workflow(workflow, run.id, session.brief, state)

    await settle(deps.store, run, work, session)
    return deps.store.get_session(session.id) or session


def directions_alike(directions: list[Direction], kit: BrandKit) -> bool:
    """Whether the diversity check fails for these directions: two share a kind of picture, or
    all three share one layout while the kit allows more than one."""
    return bool(_alike(directions, allowed_templates(kit)))


def can_be_made(direction: Direction) -> bool:
    """Whether a direction can be made as a generated photograph: a picture, not a
    statement, that does not need a real photo."""
    return direction.format != "statement" and not direction.needs_real_photo


# ------------------------------------------------------------------- workflows


def _check_workflow(deps: Deps, run: Run, session: StudioSession) -> Workflow:
    """check_brief: the code part of the brief gate, before any search is spent."""
    store = deps.store
    recorder = StepRecorder(store, run.id)

    async def check_brief() -> Event:
        async with recorder.step("check_brief") as info:
            ready = len(session.brief.split()) >= _MIN_BRIEF_WORDS
            if ready:
                info.note = BRIEF_READY
            else:
                update_session(store, session, status="needs_brief", question=BRIEF_QUESTION)
                info.note = BRIEF_QUESTION
            return Event(output={"brief_ready": ready}, state={"brief_ready": ready})

    return Workflow(name="check_brief", edges=[("START", check_brief)])


def _scout_workflow(
    deps: Deps, run: Run, session: StudioSession, *, from_research: bool
) -> Workflow:
    """plan_queries → search → report → propose → save_directions, or propose →
    save_directions when `from_research`, on the research the run starts with in state."""
    store, kit = deps.store, deps.kit
    recorder = StepRecorder(store, run.id)
    planner = build_scout_planner(deps.llm)
    reporter = build_scout_reporter(deps.llm)
    writer = build_direction_writer(deps.llm)
    # Every agent here receives the brief as its message; the rest is in its context.
    message = text_message(session.brief)

    async def plan_queries(ctx: Context) -> Event:
        async with recorder.step("plan_queries") as info:
            queries: list[str] = []
            problem = _cannot_search(deps)
            if not problem:
                info.provider = describe_llm(deps.llm)
                ctx.state["scout_context"] = _scout_context(deps, session, "queries")
                queries, problem = await _plan(ctx, planner, message, info, kit)
            info.note = WILL_SEARCH.format(queries=_quoted_list(queries)) if queries else problem
            data = {"queries": queries, "problem": problem}
            return Event(output=data, state={"planned": data})

    async def search(planned: dict[str, Any]) -> Event:
        async with recorder.step("search") as info:
            research, results = await _search(deps, planned["queries"], planned["problem"], info)
            data = research.model_dump(mode="json")
            return Event(output=data, state={"research": data, "results": results})

    async def report(
        ctx: Context, research: dict[str, Any], results: list[dict[str, Any]]
    ) -> Event:
        async with recorder.step("report") as info:
            found = ResearchReport.model_validate(research)
            if found.status == "ungrounded":
                # The directions draw on the occasions instead.
                info.note = SKIPPED
                return Event(output=research)
            info.provider = describe_llm(deps.llm)
            ctx.state["scout_context"] = _scout_context(deps, session, "report", results)
            found = await _report(ctx, reporter, message, info, found)
            data = found.model_dump(mode="json")
            return Event(output=data, state={"research": data})

    async def propose(ctx: Context, research: dict[str, Any]) -> Event:
        async with recorder.step("propose") as info:
            info.provider = describe_llm(deps.llm)
            found = ResearchReport.model_validate(research)
            ctx.state["directions_context"] = _directions_context(
                deps, session, found, from_research=from_research
            )
            proposal = await _propose(ctx, writer, message, info, kit, found)
            data = proposal.model_dump(mode="json")
            return Event(output=data, state={"proposal": data})

    async def save_directions(proposal: dict[str, Any], research: dict[str, Any]) -> Event:
        async with recorder.step("save_directions") as info:
            answer = DirectionSet.model_validate(proposal)
            if not answer.usable:
                update_session(store, session, status="needs_brief", question=answer.question)
                info.note = ASKED_FOR_BRIEF
                return Event(output={"usable": False})
            directions = answer.directions
            update_session(
                store,
                session,
                research=ResearchReport.model_validate(research),
                directions=directions,
                recommended_direction=answer.recommended if directions else None,
                recommended_reason=answer.recommended_reason if directions else "",
                # A new set waits for a pick; each prompt version keeps the title it followed.
                chosen_direction=None,
                question="",
                status="choosing",
            )
            info.note = SAVED
            return Event(output={"directions": len(directions)})

    research_steps = (
        []
        if from_research
        else [
            # Steps that call ctx.run_node must be rerunnable, as ADK requires.
            node(plan_queries, name="plan_queries", rerun_on_resume=True),
            search,
            node(report, name="report", rerun_on_resume=True),
        ]
    )
    return Workflow(
        name="scout",
        edges=[
            (
                "START",
                *research_steps,
                node(propose, name="propose", rerun_on_resume=True),
                save_directions,
            )
        ],
    )


# ------------------------------------------------------------------- research


def _cannot_search(deps: Deps) -> str:
    """Why no search can be made now, or "" when one can."""
    if deps.search_provider is None:
        return NO_SEARCH_PROVIDER
    if deps.store.search_credits_this_month() >= deps.settings.research_monthly_limit:
        return ALLOWANCE_USED
    return ""


async def _plan(
    ctx: Context, planner: LlmAgent, message: types.Content, info: StepInfo, kit: BrandKit
) -> tuple[list[str], str]:
    """The searches the scout plans, and why there are none when there are none. A planner
    that fails leaves the research not grounded; it never fails the run."""
    try:
        answer = await _ask(ctx, planner, message, info, SearchQueries, _SCOUT_NOTE)
    except Exception as error:
        # Only the type: the text of an unexpected error may hold a request address.
        logger.warning("The scout could not plan the searches: %s", type(error).__name__)
        return [], PLAN_FAILED
    queries = _clean_queries(answer.queries, kit)
    return queries, "" if queries else NO_QUERIES


def _clean_queries(queries: list[str], kit: BrandKit) -> list[str]:
    """At most three distinct searches, none blank and none naming the brand itself."""
    tidied = (" ".join(query.split()) for query in queries)
    kept = [query for query in dict.fromkeys(tidied) if query and not mentions(query, kit.name)]
    return kept[:_MAX_QUERIES]


async def _search(
    deps: Deps, queries: list[str], problem: str, info: StepInfo
) -> tuple[ResearchReport, list[dict[str, Any]]]:
    """Each planned search through the provider, five results each, deduplicated by url and
    numbered in order, at most ten sources; and the numbered results as the reporter reads
    them.

    Each search sent spends one of the month's credits, and none is sent past the monthly
    limit. The first search that fails ends the searching: the research keeps what came back
    before it, and is not grounded, with the failure's message, when nothing did. Results
    from the fake provider make demo research.
    """
    provider = deps.search_provider
    name = provider.name if provider is not None else ""
    info.provider = name
    made: list[str] = []
    found: dict[str, SearchResult] = {}
    if not problem and provider is None:
        problem = NO_SEARCH_PROVIDER
    if not problem and provider is not None:
        left = deps.settings.research_monthly_limit - deps.store.search_credits_this_month()
        if left <= 0:
            problem = ALLOWANCE_USED
        for query in queries[: max(left, 0)]:
            # Counted before the call: a search sent may be charged even when it fails.
            deps.store.add_search_credits(1)
            made.append(query)
            try:
                results = await provider.search(query, limit=_RESULTS_PER_QUERY)
            except SearchUnavailable as error:
                # Written for the designer and holding no key, so it is logged in full.
                problem = str(error).strip() or SEARCH_FAILED
                logger.warning("The search failed: %s", problem)
                break
            except Exception as error:
                # Only the type: the text of an unexpected error may hold a request address.
                logger.warning("The search provider failed with %s", type(error).__name__)
                problem = SEARCH_FAILED
                break
            for result in results:
                if result.url and result.url not in found and len(found) < _MAX_SOURCES:
                    found[result.url] = result
    sources = [
        ResearchSource(number=number, title=result.title, url=result.url, domain=result.domain)
        for number, result in enumerate(found.values(), start=1)
    ]
    # Web text, quoted inside the reporter's context block.
    results = [
        {
            "number": number,
            "title": _quoted(result.title),
            "domain": result.domain,
            "snippet": _quoted(result.snippet),
        }
        for number, result in enumerate(found.values(), start=1)
    ]
    if sources:
        status = "demo" if name == "fake" else "grounded"
        research = ResearchReport(status=status, queries=made, sources=sources, provider=name)
    else:
        problem = problem or NO_SOURCES
        research = ResearchReport(
            status="ungrounded", queries=made, provider=name, problem=problem
        )
    note = SEARCHED.format(queries=_searches(len(made)), sources=plural(len(sources), "source"))
    info.note = f"{note} {problem}" if problem else note
    return research, results


async def _report(
    ctx: Context,
    reporter: LlmAgent,
    message: types.Content,
    info: StepInfo,
    research: ResearchReport,
) -> ResearchReport:
    """The research with the scout's prose. When the scout cannot write it, the research is
    not grounded and loses its sources, so the directions draw on the occasions instead."""
    try:
        answer = await _ask(
            ctx, reporter, message, info, ScoutReport, _SCOUT_NOTE, check=_blank_report
        )
    except Exception as error:
        # Only the type: the text of an unexpected error may hold a request address.
        logger.warning("The scout could not write the research: %s", type(error).__name__)
        info.note = REPORT_FAILED
        return research.model_copy(
            update={"status": "ungrounded", "sources": [], "problem": REPORT_FAILED}
        )
    cited = _cited(answer, research.sources)
    info.note = CITED.format(sources=plural(len(cited), "source"))
    return research.model_copy(update={"text": answer.text.strip(), "model": info.provider})


def _blank_report(answer: ScoutReport) -> str:
    return EMPTY_ANSWER if not answer.text.strip() else ""


def _cited(answer: ScoutReport, sources: list[ResearchSource]) -> list[int]:
    """The numbers of the sources the report cites, in its source_numbers or as [n] in its
    text; numbers the research does not have are left out."""
    numbers = {source.number for source in sources}
    cited = {*answer.source_numbers, *(int(n) for n in _CITATION_RE.findall(answer.text))}
    return sorted(number for number in cited if number in numbers)


# ----------------------------------------------------------------- directions


async def _propose(
    ctx: Context,
    writer: LlmAgent,
    message: types.Content,
    info: StepInfo,
    kit: BrandKit,
    research: ResearchReport,
) -> DirectionSet:
    """The three directions, tidied; or the question about the brief; or no directions, with
    the reason in the step's note.

    An answer that does not fit is asked for once more with the problem. Directions that are
    alike, or of which none can be made as a generated photograph, are asked for once more
    with the diversity note, the picture note or both. The second set replaces the first
    unless only the first has a picture that can be made; the set kept may still fail, and
    the step's note then names each problem left. Any other failure of the first answer (the
    model service down, say) gives no directions too, so the research is still saved; only a
    cancellation is raised.
    """
    allowed = allowed_templates(kit)
    numbers = {source.number for source in research.sources}
    try:
        answer = await _ask(
            ctx, writer, message, info, DirectionSet, _DIRECTIONS_NOTE, check=_misfit
        )
    except Exception as error:
        # CancelledError is not an Exception, so the designer's Stop still stops the run.
        info.note = NO_DIRECTIONS.format(reason=_directions_problem(error))
        return DirectionSet()
    if not answer.usable:
        question = answer.question.strip() or BRIEF_QUESTION
        info.note = question
        return DirectionSet(usable=False, question=question)
    proposal = _tidy(answer, allowed, numbers)
    problem = _problems(proposal.directions, allowed)
    if problem:
        earlier = info.attempts
        again: DirectionSet | None
        try:
            again = await _ask(
                ctx,
                writer,
                message,
                info,
                DirectionSet,
                _DIRECTIONS_NOTE,
                attempts=1,
                note=problem,
                check=_misfit,
            )
        except Exception as error:
            # The first set is kept. Only the type: the text may hold a request address.
            logger.warning("The direction writer's second set failed: %s", type(error).__name__)
            again = None
        info.attempts += earlier
        if again is not None and again.usable:
            second = _tidy(again, allowed, numbers)
            # Auto mode needs a picture it can make: never swap a set with one for a set without.
            if not _no_picture(second.directions) or _no_picture(proposal.directions):
                proposal = second
            problem = _problems(proposal.directions, allowed)
    pick = proposal.directions[proposal.recommended - 1]
    note = PROPOSED.format(
        directions=plural(len(proposal.directions), "direction"),
        number=pick.number,
        title=pick.title,
    )
    suffixes: list[str] = []
    if problem:
        # The set kept still fails a check: the note names each problem it has.
        suffixes = [
            *([ALIKE] if _alike(proposal.directions, allowed) else []),
            *([NONE_CAN_BE_MADE] if _no_picture(proposal.directions) else []),
        ]
    info.note = " ".join([note, *suffixes])
    return proposal


def _directions_problem(error: Exception) -> str:
    """Why the direction writer gave no directions, in plain words."""
    if isinstance(error, AnswerInvalid):
        return DIRECTIONS_INVALID
    if isinstance(error, ValueError) and not isinstance(error, ValidationError):
        return str(error).strip() or DIRECTIONS_FAILED
    # Only the type: the text of an unexpected error may hold a request address.
    logger.warning("The direction writer failed: %s", type(error).__name__)
    return DIRECTIONS_FAILED


def _misfit(answer: DirectionSet) -> str:
    """What is wrong with an answer that fits the schema but not the task, or ""."""
    return THREE_DIRECTIONS if answer.usable and len(answer.directions) != 3 else ""


def _tidy(
    answer: DirectionSet, allowed: list[LayoutTemplate], numbers: set[int]
) -> DirectionSet:
    """The directions numbered 1 to 3 in order, each with a layout the kit allows, and a picture
    never with type_only, which shows no photo, unless the kit allows nothing else. A layout
    that breaks this gives way to the first fitting one not yet used in the set, or the first
    fitting one when all are. Each keeps only the source numbers the research has. The
    recommendation points at the same direction as before, or at the first when it pointed at
    none."""
    directions: list[Direction] = []
    for position, direction in enumerate(answer.directions, start=1):
        choices = allowed
        if direction.format != "statement":
            # type_only shows no photo: a picture takes it only when the kit allows nothing else.
            choices = [template for template in allowed if template != "type_only"] or allowed
        layout = direction.layout
        if layout not in choices:
            used = {earlier.layout for earlier in directions}
            layout = next((template for template in choices if template not in used), choices[0])
        sources = [n for n in dict.fromkeys(direction.source_numbers) if n in numbers]
        directions.append(
            direction.model_copy(
                update={
                    "number": position,
                    "title": direction.title.strip(),
                    "layout": layout,
                    "source_numbers": sources,
                    "edited": False,
                }
            )
        )
    recommended = next(
        (
            position
            for position, direction in enumerate(answer.directions, start=1)
            if direction.number == answer.recommended
        ),
        1,
    )
    return DirectionSet(
        directions=directions,
        recommended=recommended,
        recommended_reason=answer.recommended_reason.strip(),
    )


def _alike(directions: list[Direction], allowed: list[LayoutTemplate]) -> str:
    """The diversity check: the note naming the first two directions that share a kind of
    picture, or the layout all three share when the kit allows more than one; "" when they
    differ."""
    first: dict[str, int] = {}
    for direction in directions:
        if direction.format in first:
            return SAME_FORMAT.format(
                a=first[direction.format], b=direction.number, format=direction.format
            )
        first[direction.format] = direction.number
    layouts = {direction.layout for direction in directions}
    if len(directions) > 1 and len(layouts) == 1 and len(allowed) > 1:
        return SAME_LAYOUT.format(layout=directions[0].layout)
    return ""


def _no_picture(directions: list[Direction]) -> str:
    """The picture check: the note asking for pictures that can be made as generated
    photographs when no direction can be; "" when one can, or when there are none."""
    if directions and not any(can_be_made(direction) for direction in directions):
        return NO_PICTURE
    return ""


def _problems(directions: list[Direction], allowed: list[LayoutTemplate]) -> str:
    """Every problem the checks find, as one note for the direction writer: the diversity
    note and the picture note, each when it applies; "" when the directions pass both."""
    found = (_alike(directions, allowed), _no_picture(directions))
    return " ".join(problem for problem in found if problem)


# ------------------------------------------------------------------- contexts


def _scout_context(
    deps: Deps, session: StudioSession, task: str, results: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """What the scout reads: its task, the brief, the brand's name and audience, the kit's
    research policy and today's date; for the report, the numbered results too."""
    kit = deps.kit
    policy = kit.research
    context: dict[str, Any] = {
        "task": task,
        "brief": session.brief,
        "brand": {"name": kit.name, "audience": kit.audience},
        "field": policy.field,
        "location": policy.location,
        "trend_appetite": policy.trend_appetite,
        "occasions": [occasion.model_dump(mode="json") for occasion in policy.occasions],
        "avoid": list(policy.avoid),
        "today": now().date().isoformat(),
    }
    if results is not None:
        context["results"] = results
    return context


def _directions_context(
    deps: Deps, session: StudioSession, research: ResearchReport, *, from_research: bool = False
) -> dict[str, Any]:
    """What the direction writer reads: the brief, the brand, the allowed layouts, the research
    with its numbered sources when it is grounded or the kit's occasions when it is not, the
    taste summary, the recent headlines and concepts, and the topics to avoid. For "Other
    directions" (`from_research`), also the session's current directions, which the new set
    must differ from."""
    store, kit = deps.store, deps.kit
    policy = kit.research
    grounded = research.status != "ungrounded"
    taste = build_taste_profile(store.list_references())
    occasions = [occasion.model_dump(mode="json") for occasion in policy.occasions]
    context: dict[str, Any] = {
        "task": "directions",
        "brief": session.brief,
        "brand": brand_block(kit),
        "allowed_layouts": allowed_templates(kit),
        "grounded": grounded,
        # Web text, quoted: the writer is told to ignore any instruction inside it.
        "research": _research_block(research) if grounded else None,
        "not_grounded_because": "" if grounded else research.problem,
        "occasions": [] if grounded else occasions,
        "today": now().date().isoformat(),
        "trend_appetite": policy.trend_appetite,
        "taste": taste.summary,
        "recent_headlines": [line for line in store.recent_headlines(kit.id, _RECENT) if line],
        "recent_concepts": _recent_concepts(store, kit, session),
        "avoid": list(policy.avoid),
    }
    if from_research:
        # Model text that may echo the web, quoted like the research.
        context["previous_directions"] = [
            {
                "title": _quoted(direction.title),
                "format": direction.format,
                "angle": _quoted(direction.angle),
            }
            for direction in session.directions
        ]
    return context


def _research_block(research: ResearchReport) -> dict[str, Any]:
    """The research as the direction writer reads it: the report and its numbered sources."""
    return {
        "text": _quoted(research.text),
        "sources": [
            {"number": source.number, "title": _quoted(source.title), "domain": source.domain}
            for source in research.sources
        ],
    }


def _recent_concepts(store: Store, kit: BrandKit, session: StudioSession) -> list[str]:
    """The ideas of the brand's last ten sessions that have a concept, newest first, this
    session left out."""
    ideas = [
        other.concept.idea.strip()
        for other in store.list_sessions()
        if other.brand_id == kit.id
        and other.id != session.id
        and other.concept is not None
        and other.concept.idea.strip()
    ]
    return ideas[:_RECENT]


# ---------------------------------------------------------------- the agents


async def _ask(
    ctx: Context,
    agent: LlmAgent,
    message: types.Content,
    info: StepInfo,
    schema: type[M],
    note_key: str,
    *,
    attempts: int = _ATTEMPTS,
    note: str = "",
    check: Callable[[M], str] | None = None,
) -> M:
    """The agent's answer, asked for again with the problem fed back until `attempts` are used.

    `note`, when given, tells the first attempt why it is asked; `check` names what is wrong
    with an answer that fits the schema but not the task. Only an answer that does not fit,
    or a blank one, is asked for again; then AnswerInvalid is raised with the last problem.
    Any other failure is raised as it is, as for the prompt writer. The calling step must be
    a node with rerun_on_resume=True, as ADK requires of a step that runs another node.
    """
    ctx.state[note_key] = note
    problem = EMPTY_ANSWER
    for attempt in range(1, attempts + 1):
        info.attempts = attempt
        try:
            answer = await ctx.run_node(agent, node_input=message)
        except DynamicNodeFailError as failure:
            if not isinstance(failure.error, ValidationError):
                raise failure.error from None
            problem = _first_validation_message(failure.error)
        else:
            if answer is None:
                problem = EMPTY_ANSWER
            else:
                result = schema.model_validate(answer)
                problem = check(result) if check is not None else ""
                if not problem:
                    return result
        # The agent's instruction reads this on the next attempt.
        ctx.state[note_key] = problem[:_MAX_RETRY_NOTE_CHARS]
    raise AnswerInvalid(problem)


# ------------------------------------------------------------------- helpers


def _quoted(text: str) -> str:
    """Web text as it may sit inside a context block: without context tags, so it cannot end
    the block early."""
    return _CONTEXT_TAG_RE.sub("", text)


def _quoted_list(queries: list[str]) -> str:
    """The searches in quotes, separated by commas: "q1", "q2"."""
    return ", ".join(f'"{query}"' for query in queries)


def _searches(count: int) -> str:
    return f"{count} query" if count == 1 else f"{count} queries"
