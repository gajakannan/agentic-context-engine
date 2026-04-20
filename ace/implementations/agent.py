"""Agent — produces answers using the current skillbook of strategies.

Uses PydanticAI for structured output validation with automatic retry
and error feedback.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Union

from pydantic_ai import Agent as PydanticAgent
from pydantic_ai.settings import ModelSettings

from ..core.context import SkillbookView
from ..core.outputs import AgentOutput
from ..core.skillbook import Skillbook
from ..providers.pydantic_ai import resolve_model
from .helpers import format_optional
from .prompts import AGENT_PROMPT

logger = logging.getLogger(__name__)


class Agent:
    """Produces answers using the current skillbook of strategies.

    The Agent is one of three core ACE roles. It takes a question and
    uses the accumulated strategies in the skillbook to produce reasoned
    answers.

    Args:
        model: Model identifier string. Supports any LiteLLM model
            (e.g. ``"gpt-4o-mini"``, ``"openrouter/anthropic/claude-3.5-sonnet"``)
            or a PydanticAI-native identifier (e.g. ``"openai:gpt-4o"``).
        prompt_template: Custom prompt template (defaults to
            :data:`AGENT_PROMPT`).
        max_retries: Maximum retries for structured output validation.
            PydanticAI feeds validation errors back to the LLM on retry.
        model_settings: Optional PydanticAI ``ModelSettings`` for
            temperature, max_tokens, etc.

    Example::

        agent = Agent("gpt-4o-mini")
        output = agent.generate(
            question="What is the capital of France?",
            context="Answer concisely",
            skillbook=skillbook,
        )
        print(output.final_answer)  # "Paris"
    """

    def __init__(
        self,
        model: str,
        *,
        prompt_template: str = AGENT_PROMPT,
        max_retries: int = 3,
        model_settings: ModelSettings | None = None,
    ) -> None:
        self._prompt_template = prompt_template
        self._agent = PydanticAgent(
            resolve_model(model),
            output_type=AgentOutput,
            retries=max_retries,
            model_settings=model_settings,
            defer_model_check=True,
        )

    def generate(
        self,
        *,
        question: str,
        context: Optional[str],
        skillbook: Union[SkillbookView, Skillbook],
        reflection: Optional[str] = None,
        **kwargs: Any,
    ) -> AgentOutput:
        """Generate an answer using skillbook strategies.

        This method signature matches :class:`AgentLike`.

        Args:
            question: The question to answer.
            context: Additional context or requirements.
            skillbook: Current skillbook (needs ``as_prompt``).
            reflection: Optional reflection from a previous attempt.
            **kwargs: Accepted for protocol compatibility but not forwarded.

        Returns:
            :class:`AgentOutput` with reasoning, final_answer, and
            cited skill_ids.
        """
        prompt = self._prompt_template.format(
            skillbook=skillbook.as_prompt() or "(empty skillbook)",
            reflection=format_optional(reflection),
            question=question,
            context=format_optional(context),
        )

        result = self._agent.run_sync(prompt)
        output = result.output
        output.raw = _extract_usage(result)
        return output


def _extract_usage(result: Any) -> dict[str, Any]:
    """Extract usage metadata from a PydanticAI run result."""
    usage = result.usage()
    return {
        "usage": {
            "prompt_tokens": usage.input_tokens or 0,
            "completion_tokens": usage.output_tokens or 0,
            "total_tokens": usage.total_tokens or 0,
        },
    }
