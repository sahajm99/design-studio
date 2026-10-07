"""The agents' instructions: each carries the context block both models read."""

from __future__ import annotations

from types import MappingProxyType, SimpleNamespace
from typing import Any

from google.adk.agents import LlmAgent

from studio.contracts import StyleCard
from studio.models import ROLE_ANALYST, FakeLlm, wrap_context
from studio.workflows.agents import ANALYST_PROMPT, build_analyst


def instruction_text(agent: LlmAgent, state: dict[str, Any]) -> str:
    """The agent's instruction as ADK builds it, from a read-only view of the session state."""
    instruction = agent.instruction
    assert callable(instruction)
    text = instruction(SimpleNamespace(state=MappingProxyType(state)))
    assert isinstance(text, str)
    return text


def test_analyst_instruction_holds_the_label() -> None:
    label = 'Spring {launch} "quoted"'
    agent = build_analyst(FakeLlm(), label=label)

    text = instruction_text(agent, {})

    assert text.startswith(ANALYST_PROMPT)
    assert text.endswith(wrap_context(ROLE_ANALYST, {"label": label}))
    assert (agent.name, agent.output_key, agent.output_schema) == (
        "analyst",
        "style_card",
        StyleCard,
    )
