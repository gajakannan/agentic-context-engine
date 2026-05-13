"""Agentic SkillManager — mutates the skillbook directly via tool calls.

The SkillManager runs as a :class:`RecursiveAgent` with atomic mutation
tools (``add_skill``, ``update_skill``, ``remove_skill``, ``tag_skill``)
and read-only inspection tools (``search_skills``, ``read_skill``). Each
tool applies its effect to the real :class:`Skillbook` immediately.

The ``SkillManagerOutput`` returned by :meth:`SkillManager.update_skills`
is a post-hoc **audit log**: the ``reasoning`` comes from the agent's
structured output, and ``operations`` is the sequence of mutations the
tools recorded during the run. ``UpdateStep`` is the sole invocation
point; there is no downstream ``ApplyStep`` — the skillbook has already
been mutated when ``update_skills`` returns.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.models import Model as PydanticModel
from pydantic_ai.settings import ModelSettings

from ..core.insight_source import InsightSource
from ..core.outputs import SkillManagerOutput
from ..core.recursive_agent import AgenticConfig, BudgetExhausted, RecursiveAgent
from ..core.skillbook import Skillbook, UpdateBatch
from .prompts import SKILL_MANAGER_PROMPT, SKILL_MANAGER_SYSTEM
from .sm_tools import (
    SMDeps,
    register_add_skill,
    register_remove_skill,
    register_sm_read_skill,
    register_sm_search_skills,
    register_tag_skill,
    register_update_skill,
)

logger = logging.getLogger(__name__)


class SkillManagerReport(BaseModel):
    """Structured output the SkillManager emits when it finishes.

    Only ``reasoning`` is produced by the LLM. The audit trail of
    executed mutations is collected by the tools on ``SMDeps.operations``
    and spliced into the final :class:`SkillManagerOutput` by
    :meth:`SkillManager.update_skills`.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    reasoning: str = Field(..., description="Summary of the actions you took and why.")


class SkillManager(RecursiveAgent):
    """Transforms reflections into skillbook mutations via atomic tool calls.

    Subclass of :class:`RecursiveAgent` — inherits compaction, recursion,
    and budget management.

    The SkillManager is the third ACE role. Tools mutate the real
    :class:`Skillbook` directly: there is no staging, and no
    ``ApplyStep`` follows ``UpdateStep``. The returned
    :class:`SkillManagerOutput` is an audit log of what the tools
    already executed.

    Args:
        model: Model identifier string (LiteLLM / PydanticAI) or a
            pre-built ``Model`` instance.
        config: ``AgenticConfig`` — controls request/token budget,
            compaction, recursion depth. ``max_requests=1`` approximates
            the old one-shot behavior (a single tool turn).
        prompt_template: User prompt template (defaults to
            :data:`SKILL_MANAGER_PROMPT`).
        system_prompt: System prompt (defaults to
            :data:`SKILL_MANAGER_SYSTEM`).
        model_settings: Optional PydanticAI ``ModelSettings``.

    Example::

        sm = SkillManager("gpt-4o-mini", config=AgenticConfig(max_requests=20))
        output = sm.update_skills(
            reflections=(reflection_output,),
            skillbook=skillbook,         # real Skillbook, not a view
            question_context="Math problem solving",
            progress="5/10 correct",
            injected_skill_ids=ctx.injected_skill_ids,
        )
        # skillbook has already been mutated; output is the audit log
    """

    def __init__(
        self,
        model: str | PydanticModel,
        *,
        config: Optional[AgenticConfig] = None,
        prompt_template: str = SKILL_MANAGER_PROMPT,
        system_prompt: str = SKILL_MANAGER_SYSTEM,
        model_settings: ModelSettings | None = None,
    ) -> None:
        self._prompt_template = prompt_template

        if model_settings is None:
            from pydantic_ai.models.bedrock import BedrockModelSettings

            model_settings = BedrockModelSettings(
                bedrock_cache_instructions=True,
                bedrock_cache_tool_definitions=True,
                bedrock_cache_messages=True,
            )

        super().__init__(
            model,
            output_type=SkillManagerReport,
            system_prompt=system_prompt,
            config=config or AgenticConfig(),
            model_settings=model_settings,
            tools=[
                register_sm_search_skills,
                register_sm_read_skill,
                register_add_skill,
                register_update_skill,
                register_remove_skill,
                register_tag_skill,
            ],
            tool_names_to_compact=(
                "search_skills",
                "read_skill",
            ),
            span_label="sm",
        )

    def update_skills(
        self,
        *,
        reflections: tuple,
        skillbook: Skillbook,
        question_context: str,
        progress: str,
        source: InsightSource | None = None,
        injected_skill_ids: tuple[str, ...] = (),
        **kwargs: Any,
    ) -> SkillManagerOutput:
        """Run the agent; tools mutate ``skillbook`` as they fire.

        This method signature matches :class:`SkillManagerLike`.

        Args:
            reflections: Tuple of Reflector analyses (1-tuple for single,
                N-tuple for batch).
            skillbook: Real :class:`Skillbook` — mutated in place by the
                agent's tools.
            question_context: Description of the task domain.
            progress: Current progress summary (e.g. ``"5/10 correct"``).
            source: Base provenance record for the current learning trace.
                If ``None``, mutations are recorded without provenance.
            injected_skill_ids: Skills rendered into the Agent's prompt
                this run — the tagging scope surfaced to the agent.
            **kwargs: Accepted for protocol compatibility but not
                forwarded.

        Returns:
            :class:`SkillManagerOutput` audit log. The mutations are
            already applied; the caller does NOT need to call
            ``skillbook.apply_update()``.
        """
        reflections_data = [
            {
                "reasoning": r.reasoning,
                "error_identification": r.error_identification,
                "root_cause_analysis": r.root_cause_analysis,
                "correct_approach": r.correct_approach,
                "key_insight": r.key_insight,
            }
            for r in reflections
        ]

        prompt = self._prompt_template.format(
            progress=progress,
            stats=json.dumps(skillbook.stats()),
            injected_skill_ids=(
                json.dumps(list(injected_skill_ids)) if injected_skill_ids else "[]"
            ),
            reflections=json.dumps(reflections_data, ensure_ascii=False, indent=2),
            question_context=question_context,
        )

        deps = SMDeps(
            config=self.config,
            depth=0,
            max_depth=self.config.max_depth,
            skillbook=skillbook,
            current_source=source,
        )

        from pydantic_ai.messages import CachePoint

        prompt_payload: Any = [prompt, CachePoint(ttl="5m")]

        try:
            report, metadata = self.run(deps=deps, prompt=prompt_payload)
            reasoning = report.reasoning if report is not None else ""
            raw = {
                **metadata,
                "sm_trace": {
                    "total_iterations": deps.iteration,
                    "compactions": metadata.get("compactions", 0),
                },
            }
        except BudgetExhausted as exc:
            logger.warning(
                "SkillManager budget exhausted after %d compactions; returning partial audit",
                exc.compaction_count,
            )
            reasoning = "SkillManager budget exhausted before completing."
            raw = {
                "timeout": True,
                "sm_trace": {
                    "total_iterations": deps.iteration,
                    "compactions": exc.compaction_count,
                },
            }
        except Exception as e:
            logger.error("SkillManager failed: %s", e, exc_info=True)
            reasoning = f"SkillManager failed: {e}"
            raw = {"error": str(e)}

        return SkillManagerOutput(
            update=UpdateBatch(reasoning=reasoning, operations=list(deps.operations)),
            raw=raw,
        )
