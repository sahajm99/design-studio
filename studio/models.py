"""The language-model gateway.

Every agent instruction embeds one context block (`wrap_context`), which the
fake model and the real model both read the same way (`read_context`), so
demo mode and live runs exercise the same agent code. `FakeLlm` is a
deterministic stand-in used in demo mode and in every test; `get_llm` and
`describe_llm` choose between it and ADK's real Gemini model.
"""

from __future__ import annotations

import hashlib
import json
import re
import string
from collections.abc import AsyncGenerator
from datetime import date
from typing import Any, get_args

from google.adk.models import Gemini
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from studio.config import Settings
from studio.contracts import (
    STYLE_FIELDS,
    Background,
    CalmArea,
    ColourCount,
    CommentScope,
    CreativeBrief,
    CriticFlag,
    Direction,
    DirectionFormat,
    DirectionSet,
    EditorAnswer,
    EditorEdit,
    FinalReview,
    LayoutId,
    LayoutKind,
    Mode,
    PromptDraft,
    RoundJudgement,
    RoundRanking,
    SampleReview,
    ScoutReport,
    SearchQueries,
    StyleCard,
    Subject,
    SubjectX,
    SubjectY,
    TextAmount,
    now,
)

ROLE_ANALYST = "analyst"
ROLE_PROMPT_WRITER = "prompt_writer"
ROLE_CRITIC = "critic"
ROLE_CRITIC_RANK = "critic_rank"
ROLE_JUDGE = "judge"
ROLE_FINAL_CHECK = "final_check"
ROLE_SCOUT = "scout"  # v4: task "queries" answers SearchQueries, task "report" a ScoutReport
ROLE_DIRECTIONS = "directions"  # v4: the direction writer, answering a DirectionSet
ROLE_EDITOR = "editor"  # v5: the editor's Ask mode, answering an EditorAnswer

_UNKNOWN_ROLE_TEXT = json.dumps({"error": "FakeLlm received no context"})

_MOOD_WORDS = ["calm", "precise", "quiet", "bold", "warm", "sleek", "sculptural", "spare"]

# The Literal type behind each STYLE_FIELDS entry, in the same order, so a
# field's digest byte picks from the same option list contracts.py declares.
_FIELD_OPTIONS: dict[str, tuple[str, ...]] = dict(
    zip(
        STYLE_FIELDS,
        (
            get_args(Background),
            get_args(LayoutKind),
            get_args(TextAmount),
            get_args(Subject),
            get_args(ColourCount),
        ),
    )
)

_RETRY_OPTIONS = types.HttpRetryOptions(
    attempts=3,
    initial_delay=2.0,
    max_delay=30.0,
    http_status_codes=[429, 500, 502, 503, 504],
)


# --------------------------------------------------------------- the context block


def wrap_context(role: str, context: dict[str, Any]) -> str:
    """Embed a role and a JSON context in an agent instruction."""
    return f"ROLE: {role}\n<context>\n{json.dumps(context, ensure_ascii=False)}\n</context>"


_ROLE_RE = re.compile(r"^ROLE:\s*(.+?)\s*$", re.MULTILINE)
_CONTEXT_RE = re.compile(r"<context>(.*?)</context>", re.DOTALL)


def read_context(text: str) -> tuple[str | None, dict[str, Any]]:
    """Recover the role and context `wrap_context` embedded, anywhere in `text`.

    Returns (None, {}) when the ROLE line or the context tags are missing, or
    when the text between the tags does not parse as a JSON object.
    """
    role_match = _ROLE_RE.search(text)
    context_match = _CONTEXT_RE.search(text)
    if not role_match or not context_match:
        return None, {}
    try:
        context = json.loads(context_match.group(1).strip())
    except json.JSONDecodeError:
        return None, {}
    if not isinstance(context, dict):
        return None, {}
    return role_match.group(1), context


# --------------------------------------------------------------------- FakeLlm


class FakeLlm(BaseLlm):
    """A deterministic stand-in model, used in demo mode and in every test.

    It reads the role and context `wrap_context` embedded in the system
    instruction and answers with one JSON text part: a StyleCard for the
    analyst, a PromptDraft for the prompt writer, a SampleReview for the
    critic, a RoundRanking for the critic's ranking pass, a RoundJudgement
    for the judge, a FinalReview for the final check, SearchQueries or a
    ScoutReport for the scout (by its task), a DirectionSet for the direction
    writer, an EditorAnswer for the editor (from the request in its message),
    or an error for anything else.
    """

    model: str = "fake"

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        instruction = str(llm_request.config.system_instruction or "")
        role, context = read_context(instruction)

        if role == ROLE_ANALYST:
            digest = _digest_request(llm_request)
            text = json.dumps(_fake_style_card(digest).model_dump())
        elif role == ROLE_PROMPT_WRITER:
            text = json.dumps(_fake_prompt_draft(context).model_dump())
        elif role == ROLE_CRITIC:
            digest = _digest_request(llm_request)
            text = json.dumps(_fake_sample_review(digest).model_dump())
        elif role == ROLE_CRITIC_RANK:
            text = json.dumps(_fake_round_ranking(context).model_dump())
        elif role == ROLE_JUDGE:
            text = json.dumps(_fake_round_judgement(context).model_dump())
        elif role == ROLE_FINAL_CHECK:
            text = json.dumps(_fake_final_review(context).model_dump())
        elif role == ROLE_SCOUT:
            text = json.dumps(_fake_scout(context).model_dump())
        elif role == ROLE_DIRECTIONS:
            text = json.dumps(_fake_direction_set(context).model_dump())
        elif role == ROLE_EDITOR:
            answer = _fake_editor_answer(_request_text(llm_request), context)
            text = json.dumps(answer.model_dump())
        else:
            text = _UNKNOWN_ROLE_TEXT

        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


def _digest_request(llm_request: LlmRequest) -> bytes:
    """SHA-256 over the request's image bytes, or over its joined text when there is no image."""
    image = bytearray()
    texts: list[str] = []
    for content in llm_request.contents or []:
        for part in content.parts or []:
            blob = part.inline_data
            if blob is not None and blob.data:
                image.extend(blob.data)
            elif part.text:
                texts.append(part.text)
    source = bytes(image) if image else "".join(texts).encode("utf-8")
    return hashlib.sha256(source).digest()


def _request_text(llm_request: LlmRequest) -> str:
    """The first text of the newest message from the user, or "" when it has none.

    The editor's request comes first in its message, before the pictures. The newest message
    is read, so a request the run also opened with, or an answer asked for again, still
    gives the request.
    """
    for content in reversed(llm_request.contents or []):
        if content.role != "user":
            continue
        texts = [part.text for part in content.parts or [] if part.text]
        if texts:
            return texts[0]
    return ""


def _fake_style_card(digest: bytes) -> StyleCard:
    values = {
        field: _FIELD_OPTIONS[field][digest[i] % len(_FIELD_OPTIONS[field])]
        for i, field in enumerate(STYLE_FIELDS)
    }

    mood: list[str] = []
    for byte in (digest[5], digest[6], digest[7]):
        index = byte % len(_MOOD_WORDS)
        while _MOOD_WORDS[index] in mood:
            index = (index + 1) % len(_MOOD_WORDS)
        mood.append(_MOOD_WORDS[index])

    return StyleCard(**values, mood=mood, technique="Demo analysis: no model looked at this image.")


# -------------------------------------------------------------- prompt writer

_UNUSABLE_BRIEF_QUESTION = "Tell me who the post is for, what is on offer, and how it should feel."

_PHOTO_PROMPT_TEMPLATE = (
    "A single {subject}, photographed as a premium studio still life. Setting: a seamless, "
    "evenly lit {mode} backdrop close to the colour {hex}, with nothing else in frame. Light: "
    "soft and directional, with a gentle falloff into the backdrop. Lens: 85mm, straight on, "
    "the subject centred with generous empty space around it. Colour: restrained, two tones "
    "at most, the subject's own material doing the work. Mood: calm, precise and quiet. No "
    "text, no logos, no watermarks."
)

_SENTENCE_END_RE = re.compile(r"[.!?]")
# v3.1: a brief naming a person or a scene gets the full-bleed layout, so demo drafts vary.
# A word counts on its own or with a plural s, never inside another word.
_FULL_BLEED_WORDS_RE = re.compile(
    r"\b(?:person|people|patient|smile|scene|room|clinic|team)s?\b", re.IGNORECASE
)
_DEFAULT_ALLOWED_LAYOUTS = ("hero",)


def _fake_prompt_draft(context: dict[str, Any]) -> PromptDraft:
    task = context.get("task") or "draft"
    if task == "revise":
        return _revise_prompt(context)
    if task == "words":
        return _words_prompt(context)
    if task == "words_for_photo":
        return _words_for_photo_prompt(context)
    return _draft_prompt(context)


def _draft_prompt(context: dict[str, Any]) -> PromptDraft:
    brief = str(context.get("brief") or "")
    words = brief.split()
    if len(words) < 6:
        return PromptDraft(usable=False, question=_UNUSABLE_BRIEF_QUESTION)

    taste = context.get("taste") or {}
    background_counts = (taste.get("liked") or {}).get("background") or {}
    dark_count = int(background_counts.get("dark") or 0)
    light_count = int(background_counts.get("light") or 0)
    mode: Mode = "light" if light_count > dark_count else "dark"

    subject = " ".join(words[:6]).lower()
    hex_colour = (context.get("backdrops") or {}).get(mode, "")
    photo_prompt = _PHOTO_PROMPT_TEMPLATE.format(subject=subject, mode=mode, hex=hex_colour)

    headline = _six_word_headline(brief)
    if headline in (context.get("recent_headlines") or []):
        headline = f"{headline} Again."

    brand = context.get("brand") or {}
    hashtag = re.sub(r"[^A-Za-z0-9]", "", str(brand.get("name") or ""))

    concept = CreativeBrief(
        audience=str(brand.get("audience") or ""),
        idea=f"Show {subject} with nothing else in frame.",
        feeling="calm",
        offer=_first_sentence(brief),
        tone="quiet",
    )

    return PromptDraft(
        usable=True,
        concept=concept,
        photo_prompt=photo_prompt,
        layout=_draft_layout(brief, context),
        mode=mode,
        headline=headline,
        subline="A demo subline written without a model.",
        caption=f"{headline} This caption was written in demo mode.",
        hashtags=[f"#{hashtag}"] if hashtag else [],
        reason_prompt="Demo mode: the prompt follows a fixed template.",
        reason_words="Demo mode: the headline repeats the brief.",
        changes=["Demo mode: a new prompt from the template."],
        scope="words",
    )


def _draft_layout(brief: str, context: dict[str, Any]) -> LayoutId:
    """The layout the stand-in drafts (v3.1): full_bleed for a brief that names a person or a
    scene, when the kit allows it, else hero.

    `allowed_layouts` in the context lists the layouts the kit allows; when it is missing,
    only hero is.
    """
    allowed = context.get("allowed_layouts") or _DEFAULT_ALLOWED_LAYOUTS
    if "full_bleed" in allowed and _FULL_BLEED_WORDS_RE.search(brief):
        return "full_bleed"
    return "hero"


def _words_for_photo_prompt(context: dict[str, Any]) -> PromptDraft:
    """Task 'words_for_photo': the words as the draft task writes them; the photo is kept.

    Used when a session starts from an archived photo: there is no new photo to
    describe, so `photo_prompt` only records that the existing photo stays as it is.
    """
    draft = _draft_prompt(context)
    if not draft.usable:
        return draft
    return draft.model_copy(
        update={
            "photo_prompt": "The chosen photo, kept as it is.",
            "changes": ["Demo mode: words written for an existing photo."],
        }
    )


def _six_word_headline(brief: str) -> str:
    joined = " ".join(brief.split()[:6])
    capitalised = joined[0].upper() + joined[1:] if joined else joined
    core = capitalised.rstrip(string.punctuation + " ")
    return f"{core}."


def _first_sentence(text: str) -> str:
    stripped = text.strip()
    match = _SENTENCE_END_RE.search(stripped)
    return stripped[: match.end()] if match else stripped


_BANNED_TRIGGER_RE = re.compile(r"\b(?:no|not|without|instead of|remove|never)\b", re.IGNORECASE)
_PHRASE_END_RE = re.compile(r"[.,!?;:]")
# A word that starts the next cue ends the phrase; that cue is matched on its own.
_BANNED_CUE_WORDS = frozenset({"no", "not", "without", "instead", "remove", "never"})
_BANNED_LEADING_WORDS = frozenset({"the", "a", "any"})
_QUOTE_CHARS = "\"'“”‘’`"
_EXTRA_SPACES_RE = re.compile(r" {2,}")


def banned_terms(text: str) -> list[str]:
    """The words that follow a negation or removal cue in `text`.

    A cue is `no`, `not`, `without`, `instead of`, `remove` or `never`. The phrase after it
    ends at `and`, at the next cue, or at a comma, full stop or other punctuation mark; `or`
    splits it into one term per alternative, so "no chrome or silver" gives `chrome` and
    `silver`. Each term loses its quotes and a leading `the`, `a` or `any`, keeps at most
    three words, and is lower-cased. Used by the stand-in's revise task and shared with the
    real reviser's banned-term guard, so both apply exactly the same rule.
    """
    terms: list[str] = []
    for match in _BANNED_TRIGGER_RE.finditer(text):
        rest = text[match.end() :]
        end = _PHRASE_END_RE.search(rest)
        phrase: list[str] = []
        phrases = [phrase]
        for raw in (rest[: end.start()] if end else rest).split():
            word = raw.strip(_QUOTE_CHARS).lower()
            if word == "and" or word in _BANNED_CUE_WORDS:
                break
            if word == "or":
                phrase = []
                phrases.append(phrase)
            elif word:
                phrase.append(word)
        for words in phrases:
            if words and words[0] in _BANNED_LEADING_WORDS:
                words = words[1:]
            if words:
                terms.append(" ".join(words[:3]))
    return terms


def _remove_term(prompt: str, term: str) -> str:
    """Every whole-word, case-insensitive occurrence of `term`, removed from `prompt`."""
    pattern = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
    return pattern.sub("", prompt)


def _tidy_spaces(text: str) -> str:
    return _EXTRA_SPACES_RE.sub(" ", text).strip()


def _revise_prompt(context: dict[str, Any]) -> PromptDraft:
    """Task 'revise': keep the current words, amend the photo prompt for the feedback."""
    current = context.get("current") or {}
    photo_prompt = str(current.get("photo_prompt") or "")

    round_comment = str(context.get("round_comment") or "").strip()

    disliked_comments: list[str] = []
    liked_comments: list[str] = []
    for reaction in context.get("reactions") or []:
        if not isinstance(reaction, dict):
            continue
        comment = str(reaction.get("comment") or "").strip()
        if not comment:
            continue
        if reaction.get("reaction") == "disliked":
            disliked_comments.append(comment)
        elif reaction.get("reaction") == "liked":
            liked_comments.append(comment)

    # v2.1: strip whatever the feedback asked to remove, from the base prompt only,
    # so the "Changed after feedback"/"Avoiding" sentences below still quote it in full.
    terms = list(
        dict.fromkeys(
            banned_terms(round_comment)
            + [term for comment in disliked_comments for term in banned_terms(comment)]
        )
    )
    changes: list[str] = []
    for term in terms:
        photo_prompt = _remove_term(photo_prompt, term)
        changes.append(f'Removed "{term}".')
    if terms:
        photo_prompt = _tidy_spaces(photo_prompt)

    if round_comment:
        photo_prompt = f"{photo_prompt} Changed after feedback: {round_comment}."

    for comment in disliked_comments:
        photo_prompt = f"{photo_prompt} Avoiding: {comment}."

    for comment in liked_comments:
        changes.append(f"Kept the liked sample's look; applied: {comment}")
        photo_prompt = f"{photo_prompt} Also: {comment}."

    return PromptDraft(
        usable=True,
        photo_prompt=photo_prompt,
        layout=current.get("layout") or "hero",
        mode=current.get("mode") or "dark",
        headline=str(current.get("headline") or ""),
        subline=str(current.get("subline") or ""),
        caption=str(current.get("caption") or ""),
        hashtags=list(current.get("hashtags") or []),
        reason_prompt="Kept the subject and setting; changed what the feedback asked.",
        reason_words=str(current.get("reason_words") or ""),
        changes=changes,
    )


_PHOTO_SCOPE_WORDS = ("photo", "image", "picture", "backdrop", "background")
_WORDS_SCOPE_WORDS = ("headline", "caption", "subline", "words", "hashtag")


def _comment_scope(comment: str) -> CommentScope:
    """What a post-page comment is asking to change: the photo, the words, or both."""
    lowered = comment.lower()
    if any(word in lowered for word in _PHOTO_SCOPE_WORDS):
        if any(word in lowered for word in _WORDS_SCOPE_WORDS):
            return "both"
        return "photo"
    return "words"


def _words_prompt(context: dict[str, Any]) -> PromptDraft:
    """Task 'words': the v1 art director's light/dark/shorter rules, on the words only."""
    current = context.get("current") or {}
    comment = str(context.get("comment") or "")
    lowered = comment.lower()

    mode = current.get("mode") or "dark"
    if "light" in lowered:
        mode = "light"
    if "dark" in lowered:
        mode = "dark"

    headline = str(current.get("headline") or "")
    if "shorter" in lowered:
        shortened = " ".join(headline.split()[:4]).rstrip(string.punctuation)
        headline = f"{shortened}."

    return PromptDraft(
        usable=True,
        photo_prompt=str(current.get("photo_prompt") or ""),
        layout=current.get("layout") or "hero",
        mode=mode,
        headline=headline,
        subline=str(current.get("subline") or ""),
        caption=str(current.get("caption") or ""),
        hashtags=list(current.get("hashtags") or []),
        reason_prompt=str(current.get("reason_prompt") or ""),
        reason_words=f"Revised after the comment: {comment}",
        scope=_comment_scope(comment),
    )


# -------------------------------------------------------------------- critic


_SUBJECT_X_OPTIONS: tuple[SubjectX, ...] = ("left", "centre", "right")
_SUBJECT_Y_OPTIONS: tuple[SubjectY, ...] = ("top", "middle", "bottom")
_CALM_AREA_OPTIONS: tuple[CalmArea, ...] = ("bottom", "top", "left", "right")


def _calm_areas(byte: int) -> list[CalmArea]:
    """The areas plain enough for words, from one digest byte (v2.1).

    0 -> bottom, 1 -> top, 2 -> left, 3 -> right, 4 -> none.
    """
    index = byte % 5
    return [_CALM_AREA_OPTIONS[index]] if index < len(_CALM_AREA_OPTIONS) else []


def _fake_sample_review(digest: bytes) -> SampleReview:
    flags: list[CriticFlag] = ["busy"] if digest[3] % 5 == 0 else []
    return SampleReview(
        on_brief=2 + digest[0] % 4,
        brand_fit=2 + digest[1] % 4,
        craft=2 + digest[2] % 4,
        flags=flags,
        verdict="Demo review: no model looked at this image.",
        suggested_change="Demo mode: nothing to suggest.",
        subject_x=_SUBJECT_X_OPTIONS[digest[4] % 3],
        subject_y=_SUBJECT_Y_OPTIONS[digest[5] % 3],
        calm_areas=_calm_areas(digest[6]),
    )


# ---------------------------------------------------------------- critic ranking (v2.1)


def _fake_round_ranking(context: dict[str, Any]) -> RoundRanking:
    """The stand-in's ranking: the given sample indexes, ascending, with a fixed reason each."""
    order = sorted(int(index) for index in context.get("sample_indexes") or [])
    reasons = [f"Demo ranking: sample {index}." for index in order]
    return RoundRanking(
        order=order,
        reasons=reasons,
        note="Demo ranking: no model compared these samples.",
    )


# ----------------------------------------------------------- judge and final check (v3)

_DEMO_JUDGE_CHANGES = [
    "Demo change: softer, more even light.",
    "Demo change: a plainer backdrop with more empty space.",
]


def _fake_round_judgement(context: dict[str, Any]) -> RoundJudgement:
    """The stand-in's judgement of a round, from the top sample's score and flag.

    Good enough when the top sample has no hard flag and its overall score reaches the stop
    score. A round with no scored sample, or one that is not good enough, gets the same two
    fixed changes.
    """
    top_overall = context.get("top_overall")
    stop_score = context.get("stop_score") or 0
    if top_overall is None:
        return RoundJudgement(
            good_enough=False,
            reason="Demo judgement: no sample was scored.",
            changes=list(_DEMO_JUDGE_CHANGES),
        )
    good_enough = not context.get("top_has_hard_flag") and top_overall >= stop_score
    return RoundJudgement(
        good_enough=good_enough,
        reason=f"Demo judgement: the top sample scores {top_overall} against {stop_score}.",
        changes=[] if good_enough else list(_DEMO_JUDGE_CHANGES),
    )


def _fake_final_review(context: dict[str, Any]) -> FinalReview:
    """The stand-in's final check: it picks the first post, which ships with a fixed score
    and flaw.

    `post_count` in the context (1 when missing) is how many posts were shown (v3.1); each
    gets the same fixed reason.
    """
    post_count = int(context.get("post_count") or 1)
    return FinalReview(
        ship=True,
        score=4,
        biggest_flaw="Demo mode: no model looked at the post.",
        fix_hint="Demo mode: nothing to fix.",
        pick=1,
        reasons=["Demo mode: no model compared the posts."] * post_count,
    )


# ------------------------------------------------------- scout and directions (v4)

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_FALLBACK_FIELD = "organisations"
_REPORT_LEADS = ("Formats and series.", "Angles and hooks.", "Cautions.")


def _fake_scout(context: dict[str, Any]) -> SearchQueries | ScoutReport:
    """The scout's two tasks: `queries` (the default) and `report`."""
    if context.get("task") == "report":
        return _fake_scout_report(context)
    return _fake_search_queries(context)


def _fake_search_queries(context: dict[str, Any]) -> SearchQueries:
    """Two searches from the brand's field, the brief's first six words and this month."""
    field = _context_field(context)
    subject = " ".join(str(context.get("brief") or "").split()[:6])
    month = _context_month(context)
    return SearchQueries(
        queries=[
            f"how {field} present {subject}".strip(),
            f"{field} social media post ideas {month}",
        ]
    )


def _context_field(context: dict[str, Any]) -> str:
    """The brand's field: `field` in the context, or in its `research` or `brand` block;
    `organisations` when none is given."""
    for source in (context, context.get("research"), context.get("brand")):
        if isinstance(source, dict) and str(source.get("field") or "").strip():
            return str(source["field"]).strip()
    return _FALLBACK_FIELD


def _context_month(context: dict[str, Any]) -> str:
    """The month of `today` in the context (an ISO date), else this UTC month, in English."""
    try:
        month = date.fromisoformat(str(context.get("today") or "")[:10]).month
    except ValueError:
        month = now().month
    return _MONTHS[month - 1]


def _fake_scout_report(context: dict[str, Any]) -> ScoutReport:
    """Three short paragraphs built from the numbered results' titles, citing every one.

    The results are `results` in the context (or `sources`), each with a `number` (its
    position when missing) and a `title`. Result i goes to paragraph i mod 3, so with
    fewer than three results a paragraph can be left with nothing to cite.
    """
    numbered = _numbered_results(context)
    if not numbered:
        return ScoutReport(text="Demo research: there were no results to read.")
    paragraphs: list[str] = []
    for index, lead in enumerate(_REPORT_LEADS):
        group = numbered[index::3]
        if group:
            cited = "; ".join(f'"{title}" [{number}]' for number, title in group)
            paragraphs.append(f"{lead} Demo research: no model read these results. {cited}.")
        else:
            paragraphs.append(f"{lead} Demo research: nothing more in these results.")
    return ScoutReport(
        text="\n\n".join(paragraphs),
        source_numbers=[number for number, _ in numbered],
    )


def _numbered_results(context: dict[str, Any]) -> list[tuple[int, str]]:
    """(number, title) for each result in the context, in order."""
    numbered: list[tuple[int, str]] = []
    for position, item in enumerate(context.get("results") or context.get("sources") or [], 1):
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("number") or position)
        except (TypeError, ValueError):
            number = position
        title = str(item.get("title") or item.get("url") or "Untitled")
        numbered.append((number, title))
    return numbered


_UNUSABLE_DIRECTIONS_QUESTION = (
    "Say what the post is about and who it is for, in a sentence or two."
)
_RECOMMENDED_REASON = "Demo mode: the first direction is recommended."
# One kind of picture per direction, so the three differ by construction.
_DIRECTION_FORMATS: tuple[DirectionFormat, ...] = ("object", "people", "place")
# "Other directions" (previous_directions given): the formats rotated, each title marked.
_OTHER_DIRECTION_FORMATS: tuple[DirectionFormat, ...] = ("people", "place", "object")
_OTHER_TITLE_PREFIX = "Another "
_DIRECTION_TEXTS: dict[str, dict[str, str]] = {
    "object": {
        "label": "Object",
        "angle": "Show {subject} as one object on its own.",
        "subject": "{subject}, as a single object",
        "framing": "Centred and straight on, with generous empty space.",
        "mood": "calm",
    },
    "people": {
        "label": "People",
        "angle": "Show the people behind {subject}.",
        "subject": "a person or a small team, for {subject}",
        "framing": "Waist up, at eye level, in soft window light.",
        "mood": "warm",
    },
    "place": {
        "label": "Place",
        "angle": "Show the setting for {subject}.",
        "subject": "the setting of {subject}",
        "framing": "Wide, from the doorway, with depth.",
        "mood": "quiet",
    },
}
# Every template but custom: a direction never asks for a custom layout.
_DIRECTION_LAYOUTS = tuple(layout for layout in get_args(LayoutId) if layout != "custom")


def _fake_direction_set(context: dict[str, Any]) -> DirectionSet:
    """Three directions from the brief's first six words and `allowed_layouts`, or the
    question when the brief has fewer than six words.

    Formats object, people and place; layouts the first three distinct allowed ones,
    repeated in turn when the kit allows fewer. The place direction needs a real photo.
    When `previous_directions` is given ("Other directions"), the formats rotate to people,
    place and object and each title starts with "Another ", so the new set shows the change.
    """
    words = str(context.get("brief") or "").split()
    if len(words) < 6:
        return DirectionSet(usable=False, question=_UNUSABLE_DIRECTIONS_QUESTION)

    subject = " ".join(words[:6]).lower()
    short = " ".join(words[:4]).rstrip(string.punctuation)
    layouts = _direction_layouts(context)
    other = bool(context.get("previous_directions"))
    formats = _OTHER_DIRECTION_FORMATS if other else _DIRECTION_FORMATS
    prefix = _OTHER_TITLE_PREFIX if other else ""
    directions = []
    for number, direction_format in enumerate(formats, start=1):
        texts = _DIRECTION_TEXTS[direction_format]
        directions.append(
            Direction(
                number=number,
                title=f"{prefix}{texts['label']}: {short}",
                format=direction_format,
                angle=texts["angle"].format(subject=subject),
                subject=texts["subject"].format(subject=subject),
                framing=texts["framing"],
                mood=texts["mood"],
                layout=layouts[number - 1],
                headline_idea=" ".join(words[:5]),
                facts=[],
                why=f"Demo direction {number}.",
                source_numbers=[1],
                needs_real_photo=direction_format == "place",
            )
        )
    return DirectionSet(
        usable=True,
        directions=directions,
        recommended=1,
        recommended_reason=_RECOMMENDED_REASON,
    )


def _direction_layouts(context: dict[str, Any]) -> list[LayoutId]:
    """Three layouts: the first three distinct allowed ones, in turn when fewer are allowed.

    `allowed_layouts` missing (or naming nothing usable) means hero only, as for the draft.
    """
    allowed = [
        layout
        for layout in dict.fromkeys(context.get("allowed_layouts") or _DEFAULT_ALLOWED_LAYOUTS)
        if layout in _DIRECTION_LAYOUTS
    ] or list(_DEFAULT_ALLOWED_LAYOUTS)
    return [allowed[index % len(allowed)] for index in range(len(_DIRECTION_FORMATS))]


# ------------------------------------------------------------------ editor (v5)

# The stand-in's one question. The edit run asks it too, for an answer with nothing in it.
EDITOR_QUESTION = "Say which block to change and how."
_EDITOR_SUMMARY = "Demo mode: {edits}."
_EDITOR_SIZE_STEP = 8  # pixels, for "smaller" and "larger"
_EDITOR_MOVE_STEP = 5.0  # percent of the canvas, for "up", "down", "left" and "right"
# A text block's size when its context gives none: the renderer's headline size.
_EDITOR_FALLBACK_PX = 64
_EDITOR_TEXT_KINDS = frozenset({"headline", "subline", "text"})
_EDITOR_MAX_TEXT_CHARS = 200  # a text block's words, as Block allows
_EDITOR_SIZES: dict[str, int] = {"smaller": -_EDITOR_SIZE_STEP, "larger": _EDITOR_SIZE_STEP}
_EDITOR_MOVES: dict[str, tuple[float, float]] = {
    "up": (0.0, -_EDITOR_MOVE_STEP),
    "down": (0.0, _EDITOR_MOVE_STEP),
    "left": (-_EDITOR_MOVE_STEP, 0.0),
    "right": (_EDITOR_MOVE_STEP, 0.0),
}
# The kinds a request can name. The photo is among them so that a request about the photo,
# which no block is, gets the question rather than moving the headline.
_EDITOR_KIND_RE = re.compile(r"\b(headline|subline|logo|shade|image|text|photo)\b")
_EDITOR_BLOCK_RE = re.compile(r"\bblock (\d+)\b")
_EDITOR_LOGO_RE = re.compile(r"\b(?:change|replace) the logo\b")
_EDITOR_DELETE_SHADE_RE = re.compile(r"\bdelete the shade\b")
_EDITOR_ADD_TEXT_RE = re.compile(r"\badd text\b", re.IGNORECASE)
_EDITOR_WORDS_RE = re.compile(r"\bchange the (headline|subline) to\b", re.IGNORECASE)
# The editor's Send back and Bring forward, in words: "send the shade back".
_EDITOR_REORDERS: dict[str, re.Pattern[str]] = {
    "back": re.compile(r"\bsend\b.*\bback\b"),
    "forward": re.compile(r"\bbring\b.*\bforward\b"),
}


def _fake_editor_answer(request: str, context: dict[str, Any]) -> EditorAnswer:
    """The stand-in's edits for a handful of verbs, so demo mode shows the Ask mode.

    "smaller" and "larger" step a text block's size by 8px, "up", "down", "left" and "right"
    move a block by 5 percent of the canvas, and "send … back" and "bring … forward" move it
    one place in the list: the block the request names by kind ("the logo"), or by number
    ("block 3", which may name a block that is not there), else the headline. "change the
    headline to" or "change the subline to" gives that block the words after "to"; "change the
    logo" or "replace the logo" puts the uploaded file on the logo block; "delete the shade"
    deletes the first shade; "add text" adds a text block with the words after it. No other
    verb is read in a block's new words. Anything else, a verb whose block is not on the
    canvas, or a logo change without a file, gets the question back. The summary names the
    operations.
    """
    layout = context.get("layout") or {}
    blocks = [block for block in layout.get("blocks") or [] if isinstance(block, dict)]
    retitling = _EDITOR_WORDS_RE.search(request)
    rest = request[: retitling.start()] if retitling else request
    adding = _EDITOR_ADD_TEXT_RE.search(rest)
    asked = (rest[: adding.start()] if adding else rest).lower()
    sizes = [step for word, step in _EDITOR_SIZES.items() if _says(asked, word)]
    moves = [step for word, step in _EDITOR_MOVES.items() if _says(asked, word)]
    turns = [way for way, pattern in _EDITOR_REORDERS.items() if pattern.search(asked)]
    edits: list[EditorEdit] = []
    if sizes or moves or turns:
        target = _editor_target(asked, blocks)
        if target is None or (sizes and target.get("kind") not in _EDITOR_TEXT_KINDS):
            return EditorAnswer(usable=False, question=EDITOR_QUESTION)
        number = target["number"]
        if sizes:
            size = int((target.get("style") or {}).get("size_px") or _EDITOR_FALLBACK_PX)
            edits.append(EditorEdit(op="set_style", block=number, size_px=size + sum(sizes)))
        if moves:
            box = target.get("box") or {}
            x = _moved(box, "x", sum(step[0] for step in moves))
            y = _moved(box, "y", sum(step[1] for step in moves))
            edits.append(EditorEdit(op="move", block=number, x=x, y=y))
        edits += [EditorEdit(op="reorder", block=number, direction=way) for way in turns]
    if _EDITOR_LOGO_RE.search(asked):
        logo = _first_of(blocks, "logo")
        upload_id = str(context.get("upload_id") or "")
        if logo is None or not upload_id:
            return EditorAnswer(usable=False, question=EDITOR_QUESTION)
        edits.append(EditorEdit(op="replace_image", block=logo["number"], upload_id=upload_id))
    if _EDITOR_DELETE_SHADE_RE.search(asked):
        shade = _first_of(blocks, "shade")
        if shade is None:
            return EditorAnswer(usable=False, question=EDITOR_QUESTION)
        edits.append(EditorEdit(op="delete", block=shade["number"]))
    if adding:
        words = rest[adding.end() :].strip()[:_EDITOR_MAX_TEXT_CHARS]
        if not words:
            return EditorAnswer(usable=False, question=EDITOR_QUESTION)
        edits.append(EditorEdit(op="add_text", text=words))
    if retitling:
        block = _first_of(blocks, retitling.group(1).lower())
        words = request[retitling.end() :].strip()[:_EDITOR_MAX_TEXT_CHARS]
        if block is None or not words:
            return EditorAnswer(usable=False, question=EDITOR_QUESTION)
        edits.append(EditorEdit(op="set_text", block=block["number"], text=words))
    if not edits:
        return EditorAnswer(usable=False, question=EDITOR_QUESTION)
    return EditorAnswer(usable=True, edits=edits, summary=_editor_summary(edits, blocks))


def _says(text: str, word: str) -> bool:
    """Whether the text holds the word on its own, so "up" is not read in "upload"."""
    return re.search(rf"\b{word}\b", text) is not None


def _editor_target(asked: str, blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The block a request names: by number, even one that is not there (then only its number
    is known), or by kind, the first block of that kind; the headline when it names neither.
    None when the kind it names is not on the canvas."""
    numbered = _EDITOR_BLOCK_RE.search(asked)
    if numbered:
        number = int(numbered.group(1))
        found = next((block for block in blocks if block.get("number") == number), None)
        return found if found is not None else {"number": number}
    named = _EDITOR_KIND_RE.search(asked)
    return _first_of(blocks, named.group(1) if named else "headline")


def _first_of(blocks: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    """The first block of the kind in the context's list, or None."""
    return next((block for block in blocks if block.get("kind") == kind), None)


def _moved(box: dict[str, Any], side: str, delta: float) -> float | None:
    """One side of a block's box moved by `delta`, or None, which keeps it, when the move does
    not go that way or the box does not give the side."""
    start = box.get(side)
    if not delta or not isinstance(start, (int, float)):
        return None
    return float(start) + delta


def _editor_summary(edits: list[EditorEdit], blocks: list[dict[str, Any]]) -> str:
    """The stand-in's one sentence: each operation, with the block it names by kind, or by
    number when the block is not on the canvas."""
    kinds = {block.get("number"): block.get("kind") for block in blocks}
    named: list[str] = []
    for edit in edits:
        if edit.block is None:
            named.append(edit.op)
        elif kinds.get(edit.block):
            named.append(f"{edit.op} on the {kinds[edit.block]}")
        else:
            named.append(f"{edit.op} on block {edit.block}")
    return _EDITOR_SUMMARY.format(edits=", ".join(named))


# --------------------------------------------------------------- model selection


def get_llm(settings: Settings) -> BaseLlm:
    """The model to run agents against: the fake in demo mode, else real Gemini.

    The real model's key is read by the SDK from the GOOGLE_API_KEY
    environment variable; it is never passed in or logged here.
    """
    if settings.demo_mode:
        return FakeLlm()
    return Gemini(model=settings.gemini_model, retry_options=_RETRY_OPTIONS)


def describe_llm(llm: BaseLlm) -> str:
    """A short label for the run page: 'fake', or 'gemini:{model}'."""
    if isinstance(llm, FakeLlm):
        return "fake"
    return f"gemini:{llm.model}"
