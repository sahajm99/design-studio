"""The analyse workflow: one reference image in, one style card out.

Its only step is the analyst agent. A reply that does not fit the style card is
retried once by ADK; a second bad reply raises a plain ValueError, and the caller
records the failure.
"""

from __future__ import annotations

from google.adk import Runner, Workflow
from google.adk.models.base_llm import BaseLlm
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import RetryConfig, node
from google.genai import types
from pydantic import ValidationError

from studio.contracts import StyleCard, new_id
from studio.workflows.agents import build_analyst

_APP_NAME = "studio"
_USER_ID = "studio"
_ATTEMPTS = 2
_RETRY = RetryConfig(max_attempts=_ATTEMPTS, initial_delay=0.5, jitter=0.0)

STYLE_CARD_INVALID = (
    f"The model's answer did not match the style card after {_ATTEMPTS} attempts."
)


async def analyse_reference(
    llm: BaseLlm, image: bytes, mime_type: str, *, label: str = ""
) -> StyleCard:
    """Describe one reference image. Raises when the model gives no usable style card."""
    analyst = node(build_analyst(llm, label), retry_config=_RETRY)
    workflow = Workflow(name="analyse_reference", edges=[("START", analyst)])
    sessions = InMemorySessionService()
    runner = Runner(node=workflow, app_name=_APP_NAME, session_service=sessions)
    session_id = new_id()
    await sessions.create_session(
        app_name=_APP_NAME, user_id=_USER_ID, session_id=session_id, state={}
    )

    # The agent is the first step, so this message reaches it whole, image included.
    message = types.Content(
        role="user",
        parts=[
            types.Part.from_bytes(data=image, mime_type=mime_type),
            types.Part(text="Describe this reference."),
        ],
    )
    try:
        async for _ in runner.run_async(
            user_id=_USER_ID, session_id=session_id, new_message=message
        ):
            pass
    except ValidationError as error:
        # pydantic's own text runs to many lines and quotes the model's raw reply.
        raise ValueError(STYLE_CARD_INVALID) from error

    session = await sessions.get_session(
        app_name=_APP_NAME, user_id=_USER_ID, session_id=session_id
    )
    card = session.state.get("style_card") if session else None
    if card is None:
        raise ValueError("The model did not describe this image.")
    return StyleCard.model_validate(card)
