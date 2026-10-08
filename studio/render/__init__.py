"""Turns a design spec and a brand kit into the finished post image, shortlists the layouts that fit a photo, keeps the editor's custom layout within the brand's guardrails, and lists the faces a post may be set in."""

from studio.render.compose import (
    NEEDS_PHOTO,
    TEMPLATES,
    allowed_templates,
    composition_for,
    default_composition,
    describe,
    shortlist,
)
from studio.render.custom import (
    apply_edits,
    apply_guardrails,
    blocks_for_template,
    default_layout,
    edited_words,
)
from studio.render.faces import available_faces, face_named
from studio.render.renderer import CUSTOM_DOES_NOT_FIT, Renderer, build_html

__all__ = [
    "CUSTOM_DOES_NOT_FIT",
    "NEEDS_PHOTO",
    "TEMPLATES",
    "Renderer",
    "allowed_templates",
    "apply_edits",
    "apply_guardrails",
    "available_faces",
    "blocks_for_template",
    "build_html",
    "composition_for",
    "default_composition",
    "default_layout",
    "describe",
    "edited_words",
    "face_named",
    "shortlist",
]
