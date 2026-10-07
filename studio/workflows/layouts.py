"""The layout runs: compose shows the chosen photo in the layouts that fit it, and finish
makes the post from the layout the designer picks.

Rules, not a model, choose the layouts: the critic has already said where the photo's
subject sits and which areas are calm, and `shortlist` turns that into compositions.
Compose renders each composition as a candidate. Finish copies the chosen candidate's
image as the post, so the post is exactly the thumbnail the designer picked. Both runs
take the words from the session's current prompt version, so words edited after the
samples were made still reach the post.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from google.adk import Event, Workflow

from studio.contracts import (
    Composition,
    DesignSpec,
    LayoutCandidate,
    Post,
    PromptVersion,
    Run,
    Sample,
    StudioSession,
    new_id,
)
from studio.render import CUSTOM_DOES_NOT_FIT, NEEDS_PHOTO, allowed_templates, describe
from studio.render import shortlist as fitting_layouts
from studio.store import Store
from studio.workflows.shared import (
    TEXT_DOES_NOT_FIT,
    Deps,
    current_version,
    existing_media,
    place_in_history,
    plural,
    run_workflow,
    saved_post,
    settle,
    update_session,
)
from studio.workflows.steps import StepRecorder, readable_error

_FIRST_LAYOUTS = 3
_RENDERS_AT_ONCE = 2

SAMPLE_NOT_IN_SESSION = "This photo belongs to another session."
SAMPLE_DELETED = "This photo was deleted."
PHOTO_FILE_MISSING = "This photo's image file is missing."
LAYOUT_NOT_IN_SESSION = "This layout belongs to another session."
LAYOUT_IMAGE_MISSING = "This layout's image is missing. Render the layouts again."
LAYOUT_PHOTO_MISSING = "The photo this layout was made from is no longer available."
LAYOUT_OLD_WORDS = "The layout was made with older words. Compose again."


# ------------------------------------------------------------------------ runs


async def run_compose(
    deps: Deps,
    run: Run,
    session: StudioSession,
    sample: Sample,
    compositions: list[Composition] | None = None,
) -> list[LayoutCandidate]:
    """Render the chosen sample's photo in layouts for the designer to pick from.

    Without `compositions`, the run renders the first three layouts the shortlist rules give
    for the photo, skipping any already rendered for this sample with the current words; with
    a list, exactly those, as for "More layouts" and "Preview". Returns the new candidates,
    or an empty list when the run failed.
    """

    async def work() -> list[LayoutCandidate]:
        workflow = _compose_workflow(deps, run, session, sample, compositions)
        state = await run_workflow(workflow, run.id, session.brief)
        return [LayoutCandidate.model_validate(data) for data in state["candidates"]]

    return await settle(deps.store, run, work, session) or []


async def run_finish(
    deps: Deps, run: Run, session: StudioSession, candidate: LayoutCandidate
) -> Post | None:
    """Make the post from the chosen layout candidate, and finish the session.

    Returns the post, or None when the run failed.
    """

    async def work() -> Post:
        workflow = _finish_workflow(deps, run, session, candidate)
        return saved_post(deps.store, await run_workflow(workflow, run.id, session.brief))

    return await settle(deps.store, run, work, session)


# --------------------------------------------------------------------- compose


def _compose_workflow(
    deps: Deps,
    run: Run,
    session: StudioSession,
    sample: Sample,
    compositions: list[Composition] | None,
) -> Workflow:
    """load_pick → shortlist → render_layouts → save_layouts."""
    store, kit = deps.store, deps.kit
    recorder = StepRecorder(store, run.id)

    async def load_pick() -> Event:
        async with recorder.step("load_pick") as info:
            photo_path = _usable_photo(store, session, sample)
            version = current_version(store, session)
            info.note = (
                f"Sample {sample.index} of round {sample.round}, "
                f"words from version {version.number}."
            )
            spec_data = _base_spec(version).model_dump(mode="json")
            return Event(
                output=spec_data,
                state={
                    "design_spec": spec_data,
                    "photo_path": photo_path,
                    "hint": version.layout,
                    "version_id": version.id,
                },
            )

    async def shortlist(hint: str, version_id: str) -> Event:
        async with recorder.step("shortlist") as info:
            if compositions is None:
                fitting = fitting_layouts(sample.review, hint, allowed_templates(kit))
                earlier = store.list_layout_candidates(session.id, sample.id)
                # Only layouts made with the current words count: older ones carry old words.
                rendered = [
                    candidate.composition
                    for candidate in earlier
                    if candidate.prompt_version_id == version_id
                ]
                chosen = [c for c in fitting[:_FIRST_LAYOUTS] if c not in rendered]
                info.note = f"{plural(len(fitting), 'layout')} fit; rendering {len(chosen)}."
            else:
                chosen = list(compositions)
                info.note = f"Rendering {plural(len(chosen), 'chosen layout')}."
            data = [composition.model_dump(mode="json") for composition in chosen]
            return Event(output=data, state={"chosen_layouts": data})

    async def render_layouts(
        design_spec: dict[str, Any],
        photo_path: str,
        version_id: str,
        chosen_layouts: list[dict[str, Any]],
    ) -> Event:
        async with recorder.step("render_layouts") as info:
            spec = DesignSpec.model_validate(design_spec)
            chosen = [Composition.model_validate(item) for item in chosen_layouts]
            photo = store.media_path(photo_path)
            candidates, problems = await _render_candidates(
                deps, session, sample, spec, photo, chosen, version_id
            )
            info.note = " ".join([f"Rendered {len(candidates)} of {len(chosen)}.", *problems])
            if problems and not candidates:
                raise ValueError(problems[0])
            data = [candidate.model_dump(mode="json") for candidate in candidates]
            return Event(output=data, state={"candidates": data})

    async def save_layouts(candidates: list[dict[str, Any]]) -> Event:
        async with recorder.step("save_layouts") as info:
            saved = [LayoutCandidate.model_validate(item) for item in candidates]
            for candidate in saved:
                store.save_layout_candidate(candidate)
            update_session(store, session, picked_sample_id=sample.id, status="composing")
            info.note = f"{plural(len(saved), 'layout')} ready."
            return Event(output={"layouts": len(saved)})

    return Workflow(
        name="compose",
        edges=[("START", load_pick, shortlist, render_layouts, save_layouts)],
    )


def _usable_photo(store: Store, session: StudioSession, sample: Sample) -> str:
    """The chosen sample's photo, relative to the data folder, once the sample is usable."""
    if sample.session_id != session.id:
        raise ValueError(SAMPLE_NOT_IN_SESSION)
    if sample.status == "deleted":
        raise ValueError(SAMPLE_DELETED)
    photo_path = existing_media(store, sample.image_path)
    if photo_path is None:
        raise ValueError(PHOTO_FILE_MISSING)
    return photo_path


async def _render_candidates(
    deps: Deps,
    session: StudioSession,
    sample: Sample,
    spec: DesignSpec,
    photo: Path,
    compositions: list[Composition],
    version_id: str,
) -> tuple[list[LayoutCandidate], list[str]]:
    """Render the photo in each composition, two at a time, with the words of `version_id`.

    Indexes carry on from the sample's earlier candidates. A render that fails is left out,
    and a line saying why is returned in its place.
    """
    store = deps.store
    earlier = store.list_layout_candidates(session.id, sample.id)
    first = max((candidate.index for candidate in earlier), default=0) + 1
    limit = asyncio.Semaphore(_RENDERS_AT_ONCE)

    async def render(index: int, composition: Composition) -> LayoutCandidate:
        out_path = store.candidates_dir / session.id / f"{sample.id}-{index}.png"
        photo_path = photo if composition.template in NEEDS_PHOTO else None
        async with limit:
            report = await deps.renderer.render(
                _in_layout(spec, composition), deps.kit, photo_path, out_path
            )
        return LayoutCandidate(
            session_id=session.id,
            sample_id=sample.id,
            index=index,
            composition=composition,
            image_path=store.relative(out_path),
            render_report=report,
            prompt_version_id=version_id,
        )

    results = await asyncio.gather(
        *(render(first + offset, c) for offset, c in enumerate(compositions)),
        return_exceptions=True,
    )
    candidates = [result for result in results if isinstance(result, LayoutCandidate)]
    problems = [
        f"{describe(composition)} failed: {readable_error(result)}"
        for composition, result in zip(compositions, results)
        if isinstance(result, Exception)
    ]
    return candidates, problems


def _base_spec(version: PromptVersion) -> DesignSpec:
    """The post's words and mode from the prompt version; each layout sets its own template."""
    return DesignSpec(
        layout=version.layout,
        mode=version.mode,
        headline=version.headline,
        subline=version.subline,
        photo_prompt=version.photo_prompt,
        caption=version.caption,
        hashtags=list(version.hashtags),
        reason_layout=version.reason_prompt,
        reason_words=version.reason_words,
    )


def _in_layout(spec: DesignSpec, composition: Composition) -> DesignSpec:
    """The spec in the composition's template, with the composition's parameters."""
    return spec.model_copy(update={"layout": composition.template, "composition": composition})


# ---------------------------------------------------------------------- finish


def _finish_workflow(
    deps: Deps, run: Run, session: StudioSession, candidate: LayoutCandidate
) -> Workflow:
    """load_pick → save_post."""
    store, kit = deps.store, deps.kit
    recorder = StepRecorder(store, run.id)

    async def load_pick() -> Event:
        async with recorder.step("load_pick") as info:
            sample = _candidate_sample(store, session, candidate)
            version = current_version(store, session)
            if candidate.prompt_version_id != version.id:
                raise ValueError(LAYOUT_OLD_WORDS)
            composition = candidate.composition
            info.note = f"{describe(composition)}. Sample {sample.index} of round {sample.round}."
            spec_data = _in_layout(_base_spec(version), composition).model_dump(mode="json")
            return Event(
                output=spec_data,
                state={"design_spec": spec_data, "sample": sample.model_dump(mode="json")},
            )

    async def save_post(design_spec: dict[str, Any], sample: dict[str, Any]) -> Event:
        async with recorder.step("save_post"):
            spec = DesignSpec.model_validate(design_spec)
            picked = Sample.model_validate(sample)
            post_id = new_id()
            out_path = store.posts_dir / f"{post_id}.png"
            shutil.copyfile(store.media_path(candidate.image_path), out_path)
            previous = store.get_post(session.post_id) if session.post_id else None
            root_post_id, version = place_in_history(store, post_id, previous)
            report = candidate.render_report
            # A custom layout's words are placed by hand, so the advice points to the editor.
            custom = candidate.composition.template == "custom"
            does_not_fit = CUSTOM_DOES_NOT_FIT if custom else TEXT_DOES_NOT_FIT
            post = Post(
                id=post_id,
                root_post_id=root_post_id,
                version=version,
                run_id=run.id,
                brand_id=kit.id,
                brief=session.brief,
                image_path=store.relative(out_path),
                # Kept even for a words-only layout, so a later revision can bring the photo back.
                photo_path=existing_media(store, picked.image_path),
                photo_provider=picked.provider,
                caption=spec.caption,
                spec=spec,
                render_report=report,
                notes=[] if report.fits else [does_not_fit],
                session_id=session.id,
                sample_id=picked.id,
                direction_title=current_version(store, session).direction_title,
            )
            store.save_post(post)
            _record_pick(store, session, picked, candidate, post.id)
            return Event(
                output={"post_id": post.id, "version": post.version}, state={"post_id": post.id}
            )

    return Workflow(name="finish", edges=[("START", load_pick, save_post)])


def _candidate_sample(
    store: Store, session: StudioSession, candidate: LayoutCandidate
) -> Sample:
    """The sample the chosen layout was made from, once the layout is known to be usable."""
    if candidate.session_id != session.id:
        raise ValueError(LAYOUT_NOT_IN_SESSION)
    if existing_media(store, candidate.image_path) is None:
        raise ValueError(LAYOUT_IMAGE_MISSING)
    sample = store.get_sample(candidate.sample_id)
    if sample is None or sample.status == "deleted":
        raise ValueError(LAYOUT_PHOTO_MISSING)
    return sample


def _record_pick(
    store: Store,
    session: StudioSession,
    sample: Sample,
    candidate: LayoutCandidate,
    post_id: str,
) -> None:
    """Mark the sample picked and used in the post, reject its round's other candidates,
    and finish the session with the chosen layout."""
    picked = store.get_sample(sample.id) or sample
    picked.status = "picked"
    picked.used_in_post_id = post_id
    store.save_sample(picked)
    for other in store.list_samples(session.id):
        if other.round == picked.round and other.id != picked.id and other.status == "candidate":
            other.status = "rejected"
            store.save_sample(other)
    update_session(
        store,
        session,
        picked_sample_id=picked.id,
        picked_candidate_id=candidate.id,
        post_id=post_id,
        status="finished",
    )
