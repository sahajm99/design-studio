"""Which layouts fit a photo, and how each composition reads in words.

Rules, not a model, choose the layouts. The critic already says where the
photo's subject sits and which areas are plain enough to hold words; these rules
turn that into an ordered shortlist of compositions for the designer to pick
from. Nothing here names a brand.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from studio.contracts import BrandKit, Composition, DesignSpec, LayoutTemplate, SampleReview

# Every template, in the order the studio offers them.
TEMPLATES: tuple[LayoutTemplate, ...] = (
    "hero",
    "full_bleed",
    "split",
    "corner",
    "caption_strip",
    "type_only",
)

# The templates that show a photo, so they cannot be used without one. The editor's
# custom layout is always drawn on a photo; the rules below never offer it.
NEEDS_PHOTO: frozenset[str] = frozenset(
    {"hero", "full_bleed", "split", "corner", "caption_strip", "custom"}
)

# Each template's own starting point. The Composition model's field defaults
# are generic, so some templates start elsewhere: the split puts its photo on
# top with the logo and the words on the panel's left edge, and the corner and
# the caption strip keep their logo on the bottom row.
_DEFAULTS: dict[str, Composition] = {
    "hero": Composition(template="hero"),
    "full_bleed": Composition(
        template="full_bleed", logo_position="top_centre", text_position="bottom", text_align="centre"
    ),
    "split": Composition(template="split", photo_side="top", logo_position="top_left", text_align="left"),
    "corner": Composition(template="corner", logo_position="bottom_left", text_align="left"),
    "caption_strip": Composition(template="caption_strip", logo_position="bottom_left", text_align="left"),
    "type_only": Composition(template="type_only"),
}

# Words go opposite the subject. TextAlign has no "right", so a subject on the
# left gets centred words, the nearest the contract allows.
_ALIGN_AWAY_FROM: dict[str, str] = {"right": "left", "left": "centre", "centre": "centre"}


def default_composition(template: LayoutTemplate) -> Composition:
    """The template with its own default parameters."""
    return _DEFAULTS[template].model_copy()


def composition_for(spec: DesignSpec) -> Composition:
    """The composition a spec renders with.

    The spec's layout names the template. Its composition supplies the
    parameters when it was made for that template; otherwise, as when a
    revision changed the layout, the template's defaults apply.
    """
    composition = spec.composition
    if composition is not None and composition.template == spec.layout:
        return composition
    return default_composition(spec.layout)


def allowed_templates(kit: BrandKit) -> list[LayoutTemplate]:
    """The templates the brand allows, in the kit's order.

    A kit without a `layouts` list allows all six. Unknown names are dropped; a
    list that names no known template counts as absent, so a typo in brand.yaml
    cannot leave the studio with nothing to render.
    """
    if kit.layouts is None:
        return list(TEMPLATES)
    allowed: list[LayoutTemplate] = []
    for name in kit.layouts:
        key = name.strip().lower().replace("-", "_").replace(" ", "_")
        for template in TEMPLATES:
            if key == template and template not in allowed:
                allowed.append(template)
    return allowed or list(TEMPLATES)


def shortlist(
    review: SampleReview | None,
    hint: LayoutTemplate,
    allowed: list[LayoutTemplate],
    *,
    has_photo: bool = True,
) -> list[Composition]:
    """The compositions that fit the photo, best first, with no duplicates.

    The first three are the main picks; the rest are offered under "More
    layouts". Compositions the kit does not allow, and (without a photo) those
    that need one, are left out. Adding stops once every usable template has a
    place, so a template appears in more than one variant only while others are
    still missing.
    """
    usable = {
        template
        for template in TEMPLATES
        if template in allowed and (has_photo or template not in NEEDS_PHOTO)
    }
    picks: list[Composition] = []
    seen: set[tuple[str, ...]] = set()

    def add(composition: Composition) -> bool:
        """Place the composition if it is new and usable. True once every usable template is placed."""
        key = _key(composition)
        if composition.template in usable and key not in seen:
            seen.add(key)
            picks.append(composition)
        return {pick.template for pick in picks} >= usable

    if not usable:
        return picks
    for composition in _rule_picks(review, hint):
        if add(composition):
            return picks
    # The last rule's second half: every allowed template still without a place, with its defaults.
    for template in TEMPLATES:
        if template in usable and all(pick.template != template for pick in picks):
            if add(default_composition(template)):
                break
    return picks


def _rule_picks(review: SampleReview | None, hint: LayoutTemplate) -> Iterator[Composition]:
    """The rules' suggestions in order, up to the caption strip."""
    # 1. The prompt writer's hint, with its template's defaults.
    if hint in _DEFAULTS:
        yield default_composition(hint)

    if review is not None and not review.has_hard_flag and review.calm_areas:
        align = _ALIGN_AWAY_FROM[review.subject_x]
        side = "left" if align == "left" else "centre"
        # 2. A calm bottom: words along it, the logo at the top on their side.
        if "bottom" in review.calm_areas:
            yield _with(
                "full_bleed", text_position="bottom", text_align=align, logo_position=f"top_{side}"
            )
        # 3. A calm top: words along it, the logo at the bottom on their side.
        if "top" in review.calm_areas:
            yield _with(
                "full_bleed", text_position="top", text_align=align, logo_position=f"bottom_{side}"
            )
        # 4. A calm side with the subject on the other: the photo keeps the
        # subject's side and the panel takes the calm one.
        subject = review.subject_x
        if subject != "centre" and ("left" if subject == "right" else "right") in review.calm_areas:
            yield _with("split", photo_side=subject)
            if subject == "right" and review.subject_y == "bottom":
                yield default_composition("corner")
    else:
        # 5. No calm area, no review or a hard flag: the words stay on solid
        # colour. The split's defaults put its photo on top.
        yield default_composition("hero")
        yield default_composition("split")

    # 6. The caption strip with its logo on the side away from the subject.
    logo_side = "right" if review is not None and review.subject_x == "left" else "left"
    yield _with("caption_strip", logo_position=f"bottom_{logo_side}")


def _with(template: LayoutTemplate, **changes: Any) -> Composition:
    """The template's defaults with a few parameters changed, checked like any composition."""
    return Composition.model_validate({**_DEFAULTS[template].model_dump(), **changes})


def _key(composition: Composition) -> tuple[str, ...]:
    """What makes two compositions look different: only the parameters their template uses."""
    template = composition.template
    if template == "custom":
        # The editor's arrangement is the whole look.
        return (template, composition.custom.model_dump_json() if composition.custom else "")
    column = composition.logo_position.split("_")[1]
    if template == "full_bleed":
        return (
            template,
            composition.logo_position,
            composition.text_position,
            composition.text_align,
            composition.scrim,
        )
    if template == "split":
        return (template, composition.photo_side, column, composition.text_align)
    if template == "corner":
        return (template, column)
    if template == "caption_strip":
        side = "right" if column == "right" else "left"
        return (template, side, composition.scrim)
    return (template,)


def describe(composition: Composition) -> str:
    """The composition in a few plain words, used as a candidate's caption."""
    template = composition.template
    row, column = composition.logo_position.split("_")
    if template == "hero":
        return "Hero"
    if template == "full_bleed":
        return (
            f"Full bleed, words {composition.text_position} {composition.text_align}, "
            f"logo {row} {column}"
        )
    if template == "split":
        parts = [f"Split, photo {composition.photo_side}"]
        if column != "left":
            parts.append("logo centred" if column == "centre" else "logo right")
        if composition.text_align == "centre":
            parts.append("words centred")
        return ", ".join(parts)
    if template == "corner":
        return "Corner" if column == "left" else f"Corner, logo bottom {column}"
    if template == "caption_strip":
        return f"Caption strip, logo {'right' if column == 'right' else 'left'}"
    if template == "custom":
        return "Custom layout"
    return "Words only"
