"""Tests for the language-model gateway: the context block and FakeLlm.

No network, no key: everything here runs against FakeLlm or constructs a
Gemini object without calling it.
"""

from __future__ import annotations

import hashlib
import json
from typing import get_args

from google.adk.models import Gemini
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from studio.config import Settings
from studio.contracts import (
    STYLE_FIELDS,
    Background,
    ColourCount,
    LayoutKind,
    StyleCard,
    Subject,
    TextAmount,
)
from studio.models import (
    ROLE_ANALYST,
    FakeLlm,
    describe_llm,
    get_llm,
    read_context,
    wrap_context,
)

# The order here matches STYLE_FIELDS, independently of studio.models, so the
# determinism test is a real cross-check and not a restatement of the code.
_FIELD_LITERALS = (Background, LayoutKind, TextAmount, Subject, ColourCount)
_MOOD_WORDS = ["calm", "precise", "quiet", "bold", "warm", "sleek", "sculptural", "spare"]


async def _collect_text(llm: FakeLlm, llm_request: LlmRequest) -> str:
    responses = [response async for response in llm.generate_content_async(llm_request)]
    assert len(responses) == 1
    parts = responses[0].content.parts
    assert len(parts) == 1
    return parts[0].text


def _request(instruction: str, parts: list[types.Part]) -> LlmRequest:
    return LlmRequest(
        model="fake",
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(system_instruction=instruction),
    )


def _analyst_image_request(image_bytes: bytes) -> LlmRequest:
    instruction = wrap_context(ROLE_ANALYST, {})
    blob = types.Blob(data=image_bytes, mime_type="image/png")
    return _request(instruction, [types.Part(inline_data=blob)])


def _analyst_text_request(texts: list[str]) -> LlmRequest:
    instruction = wrap_context(ROLE_ANALYST, {})
    return _request(instruction, [types.Part(text=text) for text in texts])


def _expected_style_card_values(digest: bytes) -> dict[str, str]:
    values = {}
    for i, (field, literal) in enumerate(zip(STYLE_FIELDS, _FIELD_LITERALS)):
        options = get_args(literal)
        values[field] = options[digest[i] % len(options)]
    return values


def _expected_mood(digest: bytes) -> list[str]:
    mood: list[str] = []
    for byte in (digest[5], digest[6], digest[7]):
        index = byte % len(_MOOD_WORDS)
        while _MOOD_WORDS[index] in mood:
            index = (index + 1) % len(_MOOD_WORDS)
        mood.append(_MOOD_WORDS[index])
    return mood


# --------------------------------------------------------------- the context block


def test_context_round_trip() -> None:
    context = {
        "brief": "Café special {today} — 50% off!",
        "note": "emoji 🎉 and braces {like this}",
        "count": 3,
    }
    block = wrap_context(ROLE_ANALYST, context)
    text = f"some preamble text\n{block}\nand some trailing notes"

    role, round_tripped = read_context(text)

    assert role == ROLE_ANALYST
    assert round_tripped == context
    # non-ASCII text is not escaped in the wrapped block
    assert "Café" in block
    assert "🎉" in block


def test_read_context_without_block() -> None:
    role, context = read_context("plain instruction text with no ROLE line or tags")
    assert role is None
    assert context == {}

    role, context = read_context("ROLE: analyst\nno context tags here at all")
    assert role is None
    assert context == {}

    role, context = read_context("ROLE: analyst\n<context>\nnot valid json\n</context>")
    assert role is None
    assert context == {}


# ------------------------------------------------------------------------ analyst


async def test_fake_analyst_is_deterministic_and_valid() -> None:
    llm = FakeLlm()
    image_bytes = b"a reference photo of a dark minimal product shot"

    text_a = await _collect_text(llm, _analyst_image_request(image_bytes))
    text_b = await _collect_text(llm, _analyst_image_request(image_bytes))
    assert text_a == text_b

    digest = hashlib.sha256(image_bytes).digest()
    card = StyleCard.model_validate(json.loads(text_a))
    for field, value in _expected_style_card_values(digest).items():
        assert getattr(card, field) == value
    assert card.mood == _expected_mood(digest)
    assert len(set(card.mood)) == 3
    assert card.technique == "Demo analysis: no model looked at this image."

    other_text = await _collect_text(
        llm, _analyst_image_request(b"a completely different bright busy photo")
    )
    assert other_text != text_a

    # No image: the digest comes from the joined text parts instead.
    texts = ["first part of a description ", "second part of it"]
    text_only = await _collect_text(llm, _analyst_text_request(texts))
    text_digest = hashlib.sha256("".join(texts).encode("utf-8")).digest()
    text_card = StyleCard.model_validate(json.loads(text_only))
    for field, value in _expected_style_card_values(text_digest).items():
        assert getattr(text_card, field) == value


async def test_fake_unknown_role_returns_error_json() -> None:
    llm = FakeLlm()

    unknown_role = _request(
        wrap_context("photographer", {"brief": "whatever"}), [types.Part(text="whatever")]
    )
    text = await _collect_text(llm, unknown_role)
    assert json.loads(text) == {"error": "FakeLlm received no context"}

    no_instruction_at_all = LlmRequest(
        model="fake", contents=[types.Content(role="user", parts=[types.Part(text="whatever")])]
    )
    text = await _collect_text(llm, no_instruction_at_all)
    assert json.loads(text) == {"error": "FakeLlm received no context"}


# -------------------------------------------------------------------- model choice


def test_get_llm_in_demo_mode_is_fake(settings: Settings) -> None:
    llm = get_llm(settings)
    assert isinstance(llm, FakeLlm)
    assert describe_llm(llm) == "fake"


def test_get_llm_with_a_key_is_gemini(settings: Settings, monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-test-key-not-real")
    keyed_settings = settings.model_copy(update={"google_api_key": "dummy-test-key-not-real"})

    llm = get_llm(keyed_settings)

    assert isinstance(llm, Gemini)
    assert not isinstance(llm, FakeLlm)
    assert describe_llm(llm) == "gemini:gemini-3.5-flash-lite"
