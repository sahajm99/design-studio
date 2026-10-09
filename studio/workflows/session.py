"""The session runs: draft, samples and revise_prompt.

A session is one post's journey from brief to finished post. The designer acts between
the runs, so each run is short: it does one stage of the work and saves what it made.
Code fixes the order of each run's steps; the prompt writer and the critic decide only
what goes in their answers. The session passed to a run is read for its inputs; every
change is saved on top of the session as last saved (`update_session`). The compose and
finish runs, which turn the chosen photo into the post, are in `layouts`; the scout run,
which researches the brief and proposes the directions a draft can follow, is in `scout`.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from itertools import zip_longest
from pathlib import Path
from typing import Any

from google.adk import Context, Event, Workflow
from google.adk.agents import LlmAgent
from google.adk.workflow import node
from google.genai import types

from studio.contracts import (
    BrandKit,
    CreativeBrief,
    LayoutTemplate,
    PromptAuthor,
    PromptDraft,
    PromptVersion,
    Reference,
    RoundRanking,
    RoundReview,
    Run,
    Sample,
    SampleReview,
    StudioSession,
)
from studio.library.taste import build_taste_profile
from studio.media import product_photo
from studio.models import banned_terms, describe_llm
from studio.photos.base import (
    PRODUCT_UNREADABLE,
    WORDS_ONLY,
    LimitReached,
    PhotoProvider,
    PhotoUnavailable,
    fidelity_sentence,
    too_many_products,
)
from studio.photos.catalogue import ImageModel
from studio.photos.letterbox import trim_letterbox
from studio.render import allowed_templates
from studio.store import Store
from studio.workflows.agents import build_critic, build_critic_ranker, build_prompt_writer
from studio.workflows.shared import (
    EMPTY_ANSWER,
    TRIMMED,
    Deps,
    ask_prompt_writer,
    brand_block,
    clean_hashtags,
    current_version,
    designer_reference_context,
    designer_reference_images,
    designer_references,
    designer_references_sent,
    existing_media,
    image_part,
    labelled_parts,
    mentions,
    plural,
    product_photo_context,
    product_photo_images,
    product_references,
    products_sent,
    quality_bar,
    quality_bar_parts,
    run_workflow,
    session_products,
    settle,
    style_references,
    trim_prompt,
    update_session,
    with_backdrop,
    with_images,
)
from studio.workflows.steps import StepInfo, StepRecorder

logger = logging.getLogger(__name__)

_MAX_LIKED_CARDS = 12
_MAX_REFERENCE_IMAGES = 4
# The critic sees the designer's first references, no more than this, with each photo (v5).
_CRITIC_DESIGNER_REFERENCES = 3
_CRITICS_AT_ONCE = 3
_MAX_SEED = 2**31 - 1

ANSWER_UNUSABLE = "The model's answer could not be used."
NO_PHOTO_PROVIDER = "No photo provider is configured."
PHOTO_SERVICE_FAILED = "The photo service failed."
# The generate_samples step's note (v6): the round's count and its total cost.
ROUND_MADE = "{made} of {count} made · {cost}"
NONE_MADE = "0 of {count} made"
LETTERBOX_TRIMMED = "Trimmed a letterbox off {photos}."
# v6 Part B: the step's note when the image model received product photos.
WITH_PRODUCTS = "With {photos}."
CRITIC_FAILED = "The critic could not score this sample."
SOURCE_PHOTO_MISSING = "The photo this session started from is no longer available."
CHOSEN_PHOTO = "The chosen photo."
JUDGE_THIS_SAMPLE = "Judge this sample."
IDEAL_EXAMPLE = "The brand's ideal example"
QUALITY_BAR_NOTE = "Quality bar: {label}."
RANKING_FAILED = "The critic could not compare the samples."
RANKING_INCOMPLETE = "The ranking left out a scored sample."
ONE_RANKED = "One sample was scored, so it ranks first."
NONE_RANKED = "No sample was scored, so none was ranked."


class RankingUnusable(ValueError):
    """The critic's ranking came back, but could not be used. The message says why."""


# ------------------------------------------------------------------------ runs


async def run_draft(deps: Deps, run: Run, session: StudioSession) -> StudioSession:
    """Write the session's concept, photo prompt and words from its brief.

    For a session started from an archived photo, the words are written for that photo, and
    the photo is copied into the session as its pick. A brief that cannot be worked with gets
    a question back instead, and the session waits for a better brief. Returns the session as
    saved at the end, also when the run failed; the outcome is recorded on the run. Like every
    run, it raises only when cancelled: the run is then recorded as interrupted and the
    CancelledError raised again.
    """

    async def work() -> None:
        await run_workflow(_draft_workflow(deps, run, session), run.id, session.brief)

    await settle(deps.store, run, work, session)
    return deps.store.get_session(session.id) or session


async def run_samples(
    deps: Deps,
    run: Run,
    session: StudioSession,
    count: int | None = None,
    *,
    compare: list[str] | None = None,
) -> list[Sample]:
    """Make a round of sample photos from the current prompt, have the critic score each one,
    and rank them against each other.

    `count`, when given, is the round's size in place of the session's sample count; either
    way it is held to the most photos one round may ask for. `compare` (v6), when given, names
    the models of a compare round: one photo from each, from the same prompt version, ranked
    together; the round's size is then the number of models, and the session keeps its own
    model. Returns the round's samples, failed ones included with their reason, or an empty
    list when the run failed. A stopped run keeps the samples that finished as a partial round.
    """

    async def work() -> list[Sample]:
        workflow = _samples_workflow(deps, run, session, count, compare)
        state = await run_workflow(workflow, run.id, session.brief)
        return [Sample.model_validate(data) for data in state["samples"]]

    return await settle(deps.store, run, work, session) or []


async def run_revise_prompt(deps: Deps, run: Run, session: StudioSession) -> PromptVersion | None:
    """Rewrite the current prompt from the designer's feedback on the latest round.

    Returns the new prompt version, or None when the run failed or the answer was a
    question about the brief.
    """

    async def work() -> PromptVersion | None:
        workflow = _revise_prompt_workflow(deps, run, session)
        state = await run_workflow(workflow, run.id, session.brief)
        version_id = state.get("prompt_version_id")
        return deps.store.get_prompt_version(version_id) if version_id else None

    return await settle(deps.store, run, work, session)


# ----------------------------------------------------------------------- draft


def _draft_workflow(deps: Deps, run: Run, session: StudioSession) -> Workflow:
    """load_context → write_prompt → save_draft."""
    store, kit = deps.store, deps.kit
    recorder = StepRecorder(store, run.id)
    prompt_writer = build_prompt_writer(deps.llm)

    async def load_context(ctx: Context) -> str:
        async with recorder.step("load_context") as info:
            direction = session.chosen_direction
            references = store.list_references()
            taste = build_taste_profile(references)
            source = _source_photo(store, session)
            # The designer's own pictures of this post come right after the brief (v5): the
            # product photos first (v6 Part B), each named "Product photo n", then the style
            # references.
            session_refs = designer_references(store, session.id)
            designer = style_references(session_refs)
            products = product_references(session_refs)
            designer_images = [
                *product_photo_images(store, products),
                *designer_reference_images(store, designer),
            ]
            if source is None:
                bar, bar_label = quality_bar(store, kit)
                images = _draft_images(store, references, bar, designer_images)
                verb = "goes" if len(images) == 1 else "go"
                shown = f"{plural(len(images), 'image')} {verb} with the brief. " + (
                    QUALITY_BAR_NOTE.format(label=bar_label)
                )
            else:
                # Words for the chosen photo: the photo goes with the brief and the quality bar
                # is not sent, so the note does not name one.
                photo = str(store.media_path(source.image_path))
                images = [*designer_images, {"label": CHOSEN_PHOTO, "path": photo}]
                shown = "the chosen photo goes with the brief."
            context: dict[str, Any] = {
                "task": "draft" if source is None else "words_for_photo",
                "brief": session.brief,
                "brand": brand_block(kit),
                "taste": taste.model_dump(mode="json"),
                "liked_cards": _liked_cards(references),
                "recent_headlines": [line for line in store.recent_headlines(kit.id) if line],
                "backdrops": _backdrops(kit),
                # The prompt writer chooses the layout from these (v3.1).
                "allowed_layouts": _layouts_for(session, kit),
                # No layout is chosen before the draft, so no third of the frame is kept plain.
                "layout_hint": None,
                "text_position": "bottom",
                # The direction the designer chose, or auto mode took, from the research (v4).
                "direction": direction.model_dump(mode="json") if direction else None,
            }
            # Only a session with references carries them, so one without reads as before.
            if designer:
                context["designer_references"] = designer_reference_context(designer)
            # v6 Part B: the photo prompt describes only the scene around these.
            if products:
                context["product_photos"] = product_photo_context(products)
            ctx.state["prompt_writer_context"] = context
            ctx.state["prompt_images"] = images
            ctx.state["source_sample"] = source.model_dump(mode="json") if source else None
            liked, disliked = taste.liked_count, taste.disliked_count
            info.note = f"{liked} liked and {disliked} disliked references; {shown}"
            info.note += designer_references_sent(len(designer))
            info.note += products_sent(len(products))
            if direction is not None:
                info.note += f' Direction {direction.number}: "{direction.title}".'
            # The prompt writer receives the brief as its message, with the images after it.
            return session.brief

    async def write_prompt(
        ctx: Context, node_input: str, prompt_images: list[dict[str, str]]
    ) -> Event:
        async with recorder.step("write_prompt") as info:
            info.provider = describe_llm(deps.llm)
            message = with_images(node_input, prompt_images)
            draft = await ask_prompt_writer(ctx, prompt_writer, message, info)
            info.note = draft.concept.idea if draft.usable else draft.question
            data = draft.model_dump(mode="json")
            return Event(output=data, state={"prompt_draft": data})

    return Workflow(
        name="draft",
        edges=[
            (
                "START",
                load_context,
                # A step that calls ctx.run_node must be rerunnable, as ADK requires.
                node(write_prompt, name="write_prompt", rerun_on_resume=True),
                _save_draft_step(store, session, recorder, author="agent"),
            )
        ],
    )


def _source_photo(store: Store, session: StudioSession) -> Sample | None:
    """The archived photo the session started from, or None for a session started from a brief.

    When the original was deleted from the archive, the session's own copy of it (its round 0
    pick, which shares the file) stands in.
    """
    if session.source_sample_id is None:
        return None
    source = store.get_sample(session.source_sample_id)
    if source is not None and source.status == "deleted":
        source = next(
            (
                sample
                for sample in store.list_samples(session.id)
                if sample.round == 0 and sample.image_path == source.image_path
            ),
            None,
        )
    if source is None or existing_media(store, source.image_path) is None:
        raise ValueError(SOURCE_PHOTO_MISSING)
    return source


def _draft_images(
    store: Store,
    references: list[Reference],
    bar: Path | None,
    designer_images: list[dict[str, str]],
) -> list[dict[str, str]]:
    """The designer's references first (`designer_images`, already named), then up to four
    liked reference images and the quality bar (`bar`, when there is one), each with its name."""
    liked: list[dict[str, str]] = []
    for ref in references:
        if len(liked) == _MAX_REFERENCE_IMAGES:
            break
        relative = existing_media(store, ref.image_path) if ref.choice == "liked" else None
        if relative is not None:
            label = f"Liked reference: {ref.label or ref.id}"
            liked.append({"label": label, "path": str(store.media_path(relative))})
    images = [*designer_images, *liked]
    if bar is not None:
        images.append({"label": IDEAL_EXAMPLE, "path": str(bar)})
    return images


def _liked_cards(references: list[Reference]) -> list[dict[str, Any]]:
    """Up to 12 liked, analysed references as {id, label, card}, in library order."""
    cards = [
        {"id": ref.id, "label": ref.label, "card": ref.style_card.model_dump(mode="json")}
        for ref in references
        if ref.choice == "liked" and ref.status == "analysed" and ref.style_card is not None
    ]
    return cards[:_MAX_LIKED_CARDS]


def _backdrops(kit: BrandKit) -> dict[str, str]:
    """Each mode's background colour, which the photo's backdrop should match."""
    return {mode: kit.mode_hex(mode)["background"] for mode in kit.modes}


# --------------------------------------------- save_draft, shared by both prompt runs


def _save_draft_step(
    store: Store, session: StudioSession, recorder: StepRecorder, author: PromptAuthor
) -> Callable[..., Awaitable[Event]]:
    """The step that ends the draft and revise_prompt runs.

    A draft for a session started from an archived photo (`source_sample`) also makes that
    photo the session's pick. A revision passes the terms its feedback asked to remove
    (`banned`), and the version records any the prompt still mentions.
    """

    async def save_draft(
        prompt_draft: dict[str, Any],
        banned: list[str] | None = None,
        source_sample: dict[str, Any] | None = None,
    ) -> Event:
        async with recorder.step("save_draft") as info:
            draft = PromptDraft.model_validate(prompt_draft)
            version = _save_draft(store, session, draft, author, banned or [], info)
            if version is not None and source_sample is not None:
                pick_source_photo(store, session, Sample.model_validate(source_sample), version.id)
            version_id = version.id if version else None
            return Event(
                output={"prompt_version_id": version_id}, state={"prompt_version_id": version_id}
            )

    return save_draft


def _save_draft(
    store: Store,
    session: StudioSession,
    draft: PromptDraft,
    author: PromptAuthor,
    banned: list[str],
    info: StepInfo,
) -> PromptVersion | None:
    """Save the draft as the session's next prompt version, or its question about the brief.

    The photo prompt is trimmed to 120 words. The version keeps the draft's change list, any
    banned term the prompt still mentions, and the title of the session's chosen direction.
    A draft that follows a chosen direction takes the direction's layout, whatever the
    draft named.
    """
    if not draft.usable:
        if author == "agent_after_feedback":
            # The reviser only ever runs on a session that already has a prompt; an unusable
            # answer there is a run failure, not a reason to send it back to the brief.
            raise ValueError(ANSWER_UNUSABLE)
        update_session(store, session, status="needs_brief", question=draft.question.strip())
        info.note = "Asked for a better brief."
        return None
    photo_prompt, trimmed = trim_prompt(draft.photo_prompt.strip())
    changes = [line.strip() for line in draft.changes if line.strip()]
    if trimmed:
        changes.append(TRIMMED)
    left = [term for term in banned if mentions(photo_prompt, term)]
    direction = session.chosen_direction
    # The chosen direction's layout is enforced here, not left to the prompt writer.
    layout = direction.layout if author == "agent" and direction is not None else draft.layout
    version = PromptVersion(
        session_id=session.id,
        number=len(store.list_prompt_versions(session.id)) + 1,
        author=author,
        photo_prompt=photo_prompt,
        layout=layout,
        mode=draft.mode,
        headline=draft.headline.strip(),
        subline=draft.subline.strip(),
        caption=draft.caption.strip(),
        hashtags=clean_hashtags(draft.hashtags),
        reason_prompt=draft.reason_prompt.strip(),
        reason_words=draft.reason_words.strip(),
        changes=changes,
        banned_terms_left=left,
        direction_title=direction.title if direction is not None else "",
    )
    store.save_prompt_version(version)
    changes_to_session: dict[str, Any] = {
        "current_prompt_version_id": version.id,
        "question": "",
        "status": "drafted",
    }
    # A revision's draft carries no concept, so the session keeps the one it has.
    if not _is_blank(draft.concept):
        changes_to_session["concept"] = draft.concept
    update_session(store, session, **changes_to_session)
    info.note = " ".join(
        [
            f"Saved version {version.number}.",
            *([TRIMMED] if trimmed else []),
            *(f'The prompt still mentions "{term}".' for term in left),
        ]
    )
    return version


def _is_blank(concept: CreativeBrief) -> bool:
    return not any(value.strip() for value in concept.model_dump().values())


def pick_source_photo(
    store: Store, session: StudioSession, source: Sample, version_id: str | None
) -> Sample:
    """Copy the archived photo into the session as its round 0 pick, once per session, and
    return the copy.

    The copy is a new row pointing at the same file, so the archive's delete rules keep
    working on each row. It is made with the prompt version `version_id`, or with none when
    the session has no prompt yet. The session is left drafted, with the copy as its pick.
    """
    copies = [
        sample
        for sample in store.list_samples(session.id)
        if sample.round == 0 and sample.image_path == source.image_path
    ]
    copy = copies[0] if copies else None
    if copy is None:
        copy = Sample(
            session_id=session.id,
            prompt_version_id=version_id or "",
            round=0,
            index=1,
            image_path=source.image_path,
            provider=source.provider,
            review=source.review,
            status="picked",
        )
        store.save_sample(copy)
    update_session(store, session, picked_sample_id=copy.id, status="drafted")
    return copy


# --------------------------------------------------------------------- samples


def _samples_workflow(
    deps: Deps, run: Run, session: StudioSession, count: int | None, compare: list[str] | None = None
) -> Workflow:
    """load_prompt → generate_samples → review_samples → rank_samples → save_round."""
    store, kit = deps.store, deps.kit
    recorder = StepRecorder(store, run.id)
    critic = build_critic(deps.llm)
    ranker = build_critic_ranker(deps.llm)

    async def load_prompt() -> Event:
        async with recorder.step("load_prompt") as info:
            version = current_version(store, session)
            if not deps.photos.has_photo_source():
                raise ValueError(NO_PHOTO_PROVIDER)
            info.note = f"Version {version.number}."
            data = version.model_dump(mode="json")
            return Event(output=data, state={"prompt_version": data})

    async def generate_samples(prompt_version: dict[str, Any]) -> Event:
        async with recorder.step("generate_samples") as info:
            version = PromptVersion.model_validate(prompt_version)
            samples = await _make_samples(deps, session, version, count, info, compare)
            data = [sample.model_dump(mode="json") for sample in samples]
            return Event(output=data, state={"samples": data})

    async def review_samples(
        ctx: Context, prompt_version: dict[str, Any], samples: list[dict[str, Any]]
    ) -> Event:
        async with recorder.step("review_samples") as info:
            info.provider = describe_llm(deps.llm)
            version = PromptVersion.model_validate(prompt_version)
            context = _critic_context(session, version, kit)
            # v6 Part B: the critic scores product fidelity against the session's product
            # photos, which go first in its message, each named "Product photo n".
            products = session_products(store, session.id)
            if products:
                context["product_photos"] = product_photo_context(products)
                context["product_fidelity"] = session.product_fidelity
            ctx.state["critic_context"] = context
            round_samples = [Sample.model_validate(item) for item in samples]
            bar, bar_label = quality_bar(store, kit)
            reference_parts = _designer_reference_parts(store, session)
            product_parts = labelled_parts(product_photo_images(store, products))
            with _kept_if_stopped(store, session, round_samples):
                await _review_samples(
                    ctx,
                    critic,
                    store,
                    round_samples,
                    quality_bar_parts(bar),
                    reference_parts,
                    info,
                    product_parts=product_parts,
                )
            info.note = f"{info.note} {QUALITY_BAR_NOTE.format(label=bar_label)}"
            data = [sample.model_dump(mode="json") for sample in round_samples]
            return Event(output=data, state={"samples": data})

    async def rank_samples(
        ctx: Context, prompt_version: dict[str, Any], samples: list[dict[str, Any]]
    ) -> Event:
        async with recorder.step("rank_samples") as info:
            version = PromptVersion.model_validate(prompt_version)
            context = _critic_context(session, version, kit)
            round_samples = [Sample.model_validate(item) for item in samples]
            with _kept_if_stopped(store, session, round_samples):
                await _rank_samples(ctx, ranker, deps, session, round_samples, context, info)
            data = [sample.model_dump(mode="json") for sample in round_samples]
            return Event(output=data, state={"samples": data})

    async def save_round(samples: list[dict[str, Any]]) -> Event:
        async with recorder.step("save_round") as info:
            for item in samples:
                store.save_sample(Sample.model_validate(item))
            round_number = session.rounds + 1
            update_session(store, session, rounds=round_number, status="reviewing")
            info.note = f"Round {round_number}."
            return Event(output={"round": round_number})

    return Workflow(
        name="samples",
        edges=[
            (
                "START",
                load_prompt,
                generate_samples,
                # Steps that call ctx.run_node must be rerunnable, as ADK requires.
                node(review_samples, name="review_samples", rerun_on_resume=True),
                node(rank_samples, name="rank_samples", rerun_on_resume=True),
                save_round,
            )
        ],
    )


async def _make_samples(
    deps: Deps,
    session: StudioSession,
    version: PromptVersion,
    count: int | None,
    info: StepInfo,
    compare: list[str] | None = None,
) -> list[Sample]:
    """The round's photos from the session's model, each with a seed of its own.

    The registry turns the session's model id into the model and its adapter (v6); a model that
    can no longer be used gives way to the default, and the step's note says so. At most the
    model's `max_parallel` photos are asked for at once on its provider, and before each paid
    photo the registry checks the daily limits: a photo over a limit is not asked for, and its
    sample carries the limit's message. A letterbox the model painted is trimmed off.

    The round holds `count` photos, or the session's sample count when `count` is None, and
    never more than one round may ask for. A compare round (`compare`, the models' ids) holds
    one photo from each model instead, all asked for at once. The step's note counts the photos
    as they land, and ends with the round's total cost. A sample that could not be made is kept
    with the reason. When none could be made the step fails with the first reason, and no
    sample is kept. When the run is stopped, the photos still on their way are abandoned and
    the samples that finished are kept as a partial round.

    v6 Part B: the session's product photos are prepared once (an upright PNG of at most 2048
    pixels, without metadata) and each model is sent as many as it takes, with the fidelity
    sentence before the prompt; a words-only model gets the words alone. A sample records the
    product photos its model received, and a line when not all of them reached it.
    """
    registry = deps.photos
    if compare:
        chosen = [registry.resolve(model_id) for model_id in compare]
        models = [resolved.model for resolved in chosen]
    else:
        chosen = [registry.resolve(session.photo_model_id)]
        wanted = count if count is not None else session.sample_count
        models = [chosen[0].model] * max(1, min(wanted, deps.settings.max_samples))
    count = len(models)
    # Each model's adapter and options, once; each provider's parallel limit, once.
    plans = {
        model.id: (registry.adapter_for(model.id), registry.options_for(model)) for model in models
    }
    gates: dict[str, asyncio.Semaphore] = {}
    for model in models:
        gates.setdefault(model.provider, asyncio.Semaphore(model.max_parallel))
    round_number = session.rounds + 1
    prompt = with_backdrop(version.photo_prompt, version.mode, deps.kit, version.layout)
    size = deps.kit.post_size
    info.provider = ", ".join(
        dict.fromkeys(registry.describe(model, plans[model.id][1]) for model in models)
    )
    trimmed: list[int] = []
    products, unreadable = await _prepared_products(deps.store, session)
    sentence = fidelity_sentence(session.product_fidelity)

    async def make(index: int, model: ImageModel) -> Sample:
        adapter, options = plans[model.id]
        limit = registry.input_limit(model)
        sent = products[:limit]
        notes = list(unreadable)
        if products and not limit:
            notes.append(WORDS_ONLY)
        elif len(products) > limit:
            notes.append(too_many_products(model.label, limit))
        sample = Sample(
            session_id=session.id,
            prompt_version_id=version.id,
            round=round_number,
            index=index,
            seed=random.randint(1, _MAX_SEED),
            model_id=model.id,
            product_note=" ".join(notes),
        )
        # The fidelity sentence goes first, only to a model that sees the product photos.
        asked = f"{sentence} {prompt}" if sent else prompt
        out_path = deps.store.samples_dir / session.id / f"{round_number}-{index}.png"
        async with gates[model.provider]:
            photo = await _ask_for_photo(
                deps,
                adapter,
                model,
                options,
                sample,
                asked,
                (size.width, size.height),
                out_path,
                products=sent,
            )
        if photo is not None and await asyncio.to_thread(trim_letterbox, photo):
            trimmed.append(index)
        return sample

    tasks = [
        asyncio.create_task(make(index, model)) for index, model in enumerate(models, start=1)
    ]
    landed: list[Sample] = []
    try:
        for next_sample in asyncio.as_completed(tasks):
            landed.append(await next_sample)
            deps.store.update_step_note(info.event_id, f"{_made(landed)} of {count} made…")
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        info.note = f"Stopped after {_made(landed)} of {count}."
        _keep_stopped_round(deps.store, session, landed)
        raise
    samples = sorted(landed, key=lambda sample: sample.index)
    made = [sample for sample in samples if sample.image_path]
    total = (
        ROUND_MADE.format(made=len(made), count=count, cost=registry.round_cost(made))
        if made
        else NONE_MADE.format(count=count)
    )
    lines = list(dict.fromkeys(resolved.note for resolved in chosen if resolved.note))
    if trimmed:
        lines.append(LETTERBOX_TRIMMED.format(photos=plural(len(trimmed), "photo")))
    # v6 Part B: what reached the image model of the product photos, once per kind of line.
    received = max((len(sample.product_reference_ids) for sample in samples), default=0)
    if received:
        lines.append(WITH_PRODUCTS.format(photos=plural(received, "product photo")))
    lines += list(dict.fromkeys(sample.product_note for sample in samples if sample.product_note))
    info.note = " ".join([*lines, total])
    if not made:
        raise PhotoUnavailable(samples[0].error or PHOTO_SERVICE_FAILED)
    return samples


async def _prepared_products(
    store: Store, session: StudioSession
) -> tuple[list[tuple[str, Path]], list[str]]:
    """The session's product photos as the image model is sent them (v6 Part B): each one's
    reference id and its PNG copy, in their order; and a line for each one that could not be
    read or converted, which is left out while the round goes on."""
    prepared: list[tuple[str, Path]] = []
    notes: list[str] = []
    for number, reference in enumerate(session_products(store, session.id), start=1):
        try:
            path = await asyncio.to_thread(product_photo, store, reference.image_path)
        except Exception as error:
            # Only the type: Pillow's and the disk's errors alike.
            logger.warning("A product photo could not be prepared: %s", type(error).__name__)
            notes.append(PRODUCT_UNREADABLE.format(number=number))
            continue
        prepared.append((reference.id, path))
    return prepared, notes


async def _ask_for_photo(
    deps: Deps,
    adapter: PhotoProvider,
    model: ImageModel,
    options: dict[str, str],
    sample: Sample,
    prompt: str,
    size: tuple[int, int],
    out_path: Path,
    *,
    products: list[tuple[str, Path]] | None = None,
) -> Path | None:
    """Ask the model for the sample's photo and fill the sample in: its photo, provider and
    cost, or the reason it has none. Returns the photo's file, or None.

    A paid photo over a daily limit is not asked for. The registry counts a paid photo from
    the moment it is asked for, so photos asked for at once cannot together pass a limit.
    `products` (v6 Part B) are the product photos the model is sent, each with its reference
    id; the sample records those the model received.
    """
    registry = deps.photos
    products = products or []
    try:
        registry.hold(sample, model, options, len(products))
    except LimitReached as error:
        sample.error = str(error)
        return None
    try:
        result = await adapter.generate(
            prompt,
            size[0],
            size[1],
            out_path,
            model=model,
            options=options,
            seed=sample.seed,
            images=[path for _, path in products],
        )
    except PhotoUnavailable as error:
        # Written for the designer, and every known key is taken out before it is kept.
        message = registry.redact(str(error)).strip()
        logger.warning("Photo %s-%s failed: %s", sample.round, sample.index, message)
        sample.error = message or PHOTO_SERVICE_FAILED
        return None
    except Exception as error:
        # Only the type: the text of an unexpected error may hold a request address.
        logger.warning("The photo provider failed with %s", type(error).__name__)
        sample.error = PHOTO_SERVICE_FAILED
        return None
    else:
        photo = Path(result.path)
        sample.image_path = deps.store.relative(photo)
        sample.provider = result.provider
        sample.cost_usd = result.cost_usd
        sample.cost_basis = result.cost_basis
        # As many as the adapter says it sent: "With 1 product photo" on the card.
        sample.product_reference_ids = [ref_id for ref_id, _ in products][: result.input_images]
        return photo
    finally:
        registry.record(sample)


def _made(samples: list[Sample]) -> int:
    """How many of the samples have a photo."""
    return sum(1 for sample in samples if sample.image_path)


def _keep_stopped_round(store: Store, session: StudioSession, samples: list[Sample]) -> None:
    """Save a stopped round's finished samples as a partial round, when any photo was made."""
    if not _made(samples):
        return
    for sample in sorted(samples, key=lambda sample: sample.index):
        store.save_sample(sample)
    update_session(store, session, rounds=session.rounds + 1, status="reviewing")


@contextmanager
def _kept_if_stopped(
    store: Store, session: StudioSession, samples: list[Sample]
) -> Iterator[None]:
    """When the run is stopped, keep the round's samples as they stand, then let the stop go on."""
    try:
        yield
    except asyncio.CancelledError:
        _keep_stopped_round(store, session, samples)
        raise


def _critic_context(
    session: StudioSession, version: PromptVersion, kit: BrandKit
) -> dict[str, Any]:
    """What the critic reads beside the photos: their prompt, as the photo service received it
    for the version's layout, the layout the words will be set in, the direction the post
    follows when there is one, the concept and the brand's feel."""
    return {
        "prompt": with_backdrop(version.photo_prompt, version.mode, kit, version.layout),
        "layout": version.layout,
        "direction": (
            {"title": d.title, "format": d.format, "angle": d.angle}
            if (d := session.chosen_direction) is not None
            else None
        ),
        "concept": session.concept.model_dump(mode="json") if session.concept else None,
        "brand": {"name": kit.name, "feel": kit.feel},
    }


def _designer_reference_parts(store: Store, session: StudioSession) -> list[types.Part]:
    """What follows the quality bar in a critic's message (v5): the designer's first three
    style references, each image (its agent copy, as the prompt writer gets) after the line
    naming it, numbered as the prompt writer saw them. Empty for a session without references.
    The product photos (v6 Part B) go first in the message instead."""
    references = style_references(designer_references(store, session.id))
    return labelled_parts(
        designer_reference_images(store, references[:_CRITIC_DESIGNER_REFERENCES])
    )


def _layouts_for(session: StudioSession, kit: BrandKit) -> list[LayoutTemplate]:
    """The layouts the prompt writer may choose: the kit's, without type_only in auto mode,
    where every post shows a photograph; the kit's as they are when that would leave none."""
    allowed = allowed_templates(kit)
    if session.mode != "auto":
        return allowed
    with_photo = [layout for layout in allowed if layout != "type_only"]
    return with_photo or allowed


async def _review_samples(
    ctx: Context,
    critic: LlmAgent,
    store: Store,
    samples: list[Sample],
    bar_parts: list[types.Part],
    reference_parts: list[types.Part],
    info: StepInfo,
    *,
    product_parts: list[types.Part] | None = None,
) -> None:
    """Give each sample with a photo the critic's review, at most three at a time, and
    recommend the one its scores put first; the ranking may move the recommendation later.
    A sample the critic cannot score keeps the reason instead.

    Each photo goes with the quality bar (`bar_parts`) and, after it, the designer's
    references (`reference_parts`, v5); either may be empty. The session's product photos
    (`product_parts`, v6 Part B) go before the photo, and the critic scores product fidelity
    against them; without any, code drops a fidelity score and a `product_changed` flag, which
    would be about nothing.
    """
    limit = asyncio.Semaphore(_CRITICS_AT_ONCE)
    guide_parts = [*bar_parts, *reference_parts]
    product_parts = product_parts or []

    async def review(sample: Sample) -> float:
        async with limit:
            started = time.monotonic()
            try:
                image = store.media_path(sample.image_path)
                answer = await _ask_critic(ctx, critic, image, guide_parts, product_parts)
                if not product_parts:
                    flags = [flag for flag in answer.flags if flag != "product_changed"]
                    answer = answer.model_copy(update={"product_fidelity": None, "flags": flags})
                sample.review = answer
            except Exception as error:
                logger.warning("The critic could not score a sample: %s", type(error).__name__)
                sample.review_error = CRITIC_FAILED
            return time.monotonic() - started

    made = [sample for sample in samples if sample.image_path]
    durations = await asyncio.gather(*(review(sample) for sample in made))
    scored = [sample for sample in made if sample.review is not None]
    slowest = f"; slowest {max(durations):.0f}s" if durations else ""
    info.note = f"{len(scored)} of {len(made)} scored{slowest}."
    # Sorting keeps the order of equal scores, so a tie goes to the lower index.
    _recommend(samples, sorted(scored, key=lambda sample: sample.review.overall, reverse=True))


async def _ask_critic(
    ctx: Context,
    critic: LlmAgent,
    image: Path,
    guide_parts: list[types.Part],
    product_parts: list[types.Part] | None = None,
) -> SampleReview:
    """The critic's review of one sample photo, held to the brand's quality bar and the
    designer's references, which `guide_parts` holds after the photo, each named. The product
    photos (`product_parts`, v6 Part B), each named, come first in the message."""
    parts = [
        *(product_parts or []),
        image_part(image),
        types.Part(text=JUDGE_THIS_SAMPLE),
        *guide_parts,
    ]
    # Each call runs on a branch of its own, so calls made at the same time never see
    # each other's photo or answer.
    answer = await ctx.run_node(
        critic, node_input=types.Content(role="user", parts=parts), use_sub_branch=True
    )
    if answer is None:
        raise ValueError(EMPTY_ANSWER)
    return SampleReview.model_validate(answer)


async def _rank_samples(
    ctx: Context,
    ranker: LlmAgent,
    deps: Deps,
    session: StudioSession,
    samples: list[Sample],
    critic_context: dict[str, Any],
    info: StepInfo,
) -> None:
    """Rank the round's scored samples against each other and recommend the best one with no
    hard flag.

    One scored sample ranks first without a call. When the ranking fails, the samples keep
    the recommendation their scores gave them, and the round's review says why the ranking
    was skipped.
    """
    scored = [sample for sample in samples if sample.review is not None]
    if len(scored) < 2:
        for sample in scored:
            sample.rank = 1
        info.note = ONE_RANKED if scored else NONE_RANKED
        return
    info.provider = describe_llm(deps.llm)
    round_number = scored[0].round
    indexes = [sample.index for sample in scored]
    ctx.state["ranking_context"] = {"sample_indexes": indexes, **critic_context}
    try:
        ranking = await _ask_ranker(ctx, ranker, deps.store, scored)
        order = _ranked(ranking, indexes)
    except Exception as error:
        note = f"Ranking skipped: {_ranking_problem(error)}"
        skipped = RoundReview(session_id=session.id, round=round_number, note=note)
        deps.store.save_round_review(skipped)
        info.note = note
        return
    by_index = {sample.index: sample for sample in scored}
    for rank, (index, reason) in enumerate(order, start=1):
        by_index[index].rank = rank
        by_index[index].rank_reason = reason
    _recommend(samples, [by_index[index] for index, _ in order])
    deps.store.save_round_review(
        RoundReview(
            session_id=session.id,
            round=round_number,
            order=[index for index, _ in order],
            reasons=[reason for _, reason in order],
            note=ranking.note.strip(),
        )
    )
    info.note = f"Ranked {len(order)} samples; sample {order[0][0]} first."


async def _ask_ranker(
    ctx: Context, ranker: LlmAgent, store: Store, scored: list[Sample]
) -> RoundRanking:
    """The critic's ranking of the scored samples, each photo after a line with its number."""
    parts: list[types.Part] = []
    for sample in scored:
        parts += [
            types.Part(text=f"Sample {sample.index}"),
            image_part(store.media_path(sample.image_path)),
        ]
    answer = await ctx.run_node(ranker, node_input=types.Content(role="user", parts=parts))
    if answer is None:
        raise RankingUnusable(EMPTY_ANSWER)
    return RoundRanking.model_validate(answer)


def _ranked(ranking: RoundRanking, indexes: list[int]) -> list[tuple[int, str]]:
    """Each scored sample's number with its reason, best first.

    Numbers the round does not have, and repeats, are dropped, and a missing reason is left
    blank. Raises when the ranking leaves out a scored sample.
    """
    ranked: dict[int, str] = {}
    for index, reason in zip_longest(ranking.order, ranking.reasons, fillvalue=""):
        if index in indexes and index not in ranked:
            ranked[index] = str(reason).strip()
    if len(ranked) < len(indexes):
        raise RankingUnusable(RANKING_INCOMPLETE)
    return list(ranked.items())


def _ranking_problem(error: Exception) -> str:
    """Why the ranking could not be used, in plain words."""
    if isinstance(error, RankingUnusable):
        return str(error)
    # Only the type: the text of an unexpected error may hold a request address.
    logger.warning("The critic could not rank a round: %s", type(error).__name__)
    return RANKING_FAILED


def _recommend(samples: list[Sample], best_first: list[Sample]) -> None:
    """Recommend the first of `best_first` with no hard flag, and no other sample of the round."""
    pick = next(
        (s for s in best_first if s.review is not None and not s.review.has_hard_flag), None
    )
    for sample in samples:
        sample.recommended = sample is pick


# --------------------------------------------------------------- revise_prompt


def _revise_prompt_workflow(deps: Deps, run: Run, session: StudioSession) -> Workflow:
    """load_feedback → rewrite_prompt → save_draft."""
    store, kit = deps.store, deps.kit
    recorder = StepRecorder(store, run.id)
    prompt_writer = build_prompt_writer(deps.llm)

    async def load_feedback(ctx: Context) -> str:
        async with recorder.step("load_feedback") as info:
            current = current_version(store, session)
            feedback = _feedback(store, session, current)
            session_refs = designer_references(store, session.id)
            designer = style_references(session_refs)
            products = product_references(session_refs)
            context: dict[str, Any] = {
                "task": "revise",
                "brief": session.brief,
                "brand": brand_block(kit),
                "backdrops": _backdrops(kit),
                "allowed_layouts": _layouts_for(session, kit),
                "concept": session.concept.model_dump(mode="json") if session.concept else None,
                "current": current.model_dump(mode="json"),
                "designer_edited": current.author == "designer",
                **feedback,
            }
            # Only a session with references carries them, so one without reads as before (v5).
            if designer:
                context["designer_references"] = designer_reference_context(designer)
            # v6 Part B: the revised photo prompt still describes only the scene around these.
            if products:
                context["product_photos"] = product_photo_context(products)
            ctx.state["prompt_writer_context"] = context
            ctx.state["prompt_images"] = [
                *product_photo_images(store, products),
                *designer_reference_images(store, designer),
            ]
            ctx.state["banned"] = _banned(feedback)
            comment = "a comment" if feedback["round_comment"] else "no comment"
            reactions = len(feedback["reactions"])
            info.note = f"{reactions} reactions and {comment} on round {session.rounds}."
            info.note += designer_references_sent(len(designer))
            info.note += products_sent(len(products))
            # The prompt writer receives the brief as its message, with the references after it.
            return session.brief

    async def rewrite_prompt(
        ctx: Context, node_input: str, banned: list[str], prompt_images: list[dict[str, str]]
    ) -> Event:
        async with recorder.step("rewrite_prompt") as info:
            info.provider = describe_llm(deps.llm)
            message = with_images(node_input, prompt_images)
            draft = await ask_prompt_writer(ctx, prompt_writer, message, info)
            kept = [term for term in banned if mentions(draft.photo_prompt, term)]
            if kept:
                # The prompt still holds words the feedback asked to remove: ask once more,
                # naming them. A term kept again is flagged on the saved version.
                earlier = info.attempts
                ctx.state["retry_note"] = _still_contains(kept)
                draft = await ask_prompt_writer(ctx, prompt_writer, message, info)
                info.attempts += earlier
            info.note = draft.reason_prompt
            data = draft.model_dump(mode="json")
            return Event(output=data, state={"prompt_draft": data})

    return Workflow(
        name="revise_prompt",
        edges=[
            (
                "START",
                load_feedback,
                node(rewrite_prompt, name="rewrite_prompt", rerun_on_resume=True),
                _save_draft_step(store, session, recorder, author="agent_after_feedback"),
            )
        ],
    )


def _feedback(store: Store, session: StudioSession, current: PromptVersion) -> dict[str, Any]:
    """The latest round's comment with who wrote it, the designer or the critic, its reacted
    samples in full, one line per earlier round, and where the current layout puts the words.
    """
    latest = session.rounds
    samples = store.list_samples(session.id)
    # Feedback comes oldest first, so a round's newest comment is the one kept.
    newest = {item.round: item for item in store.list_round_feedback(session.id)}
    comments = {number: item.text.strip() for number, item in newest.items()}
    reacted = [s for s in samples if s.round == latest and s.reaction != "none"]
    earlier = [
        _round_line(number, [s for s in samples if s.round == number], comments.get(number, ""))
        for number in range(1, latest)
    ]
    return {
        "round_comment": comments.get(latest, ""),
        "round_comment_author": newest[latest].author if latest in newest else "designer",
        "reactions": [_reaction(sample) for sample in reacted],
        "earlier_rounds": earlier,
        "layout_hint": current.layout,
        "text_position": "bottom",
    }


def _reaction(sample: Sample) -> dict[str, Any]:
    """A reacted sample as the prompt writer reads it."""
    review = sample.review
    return {
        "reaction": sample.reaction,
        "verdict": review.verdict if review else "",
        "flags": list(review.flags) if review else [],
        "comment": sample.comment,
    }


def _round_line(number: int, samples: list[Sample], comment: str) -> str:
    disliked = sum(1 for sample in samples if sample.reaction == "disliked")
    return f"Round {number}: {len(samples)} samples, {disliked} disliked. {comment}".strip()


def _banned(feedback: dict[str, Any]) -> list[str]:
    """The terms the feedback asked to remove: each phrase after a negation in the round's
    comment and in the comments on disliked samples, by the rule the stand-in follows too."""
    texts = [
        feedback["round_comment"],
        *(item["comment"] for item in feedback["reactions"] if item["reaction"] == "disliked"),
    ]
    return list(dict.fromkeys(term for text in texts for term in banned_terms(text)))


def _still_contains(terms: list[str]) -> str:
    """The retry note that names the banned terms the prompt kept."""
    quoted = [f'"{term}"' for term in terms]
    listed = quoted[0] if len(quoted) == 1 else f"{', '.join(quoted[:-1])} and {quoted[-1]}"
    return f"Your prompt still contains {listed}, which the feedback asked to remove."
