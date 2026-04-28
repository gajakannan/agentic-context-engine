"""RR-specific tool registrars and dependency container.

Generic tools (execute_code, recurse) are provided by
:mod:`ace.core.recursive_agent`. This module adds RR-specific
tools and the RR dependency container.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional, Union

from pydantic_ai import ModelRetry, RunContext

from ace.core.context import SkillbookView
from ace.core.recursive_agent import AgenticDeps
from ace.core.skillbook import Skillbook

if TYPE_CHECKING:
    from pydantic_ai import Agent as PydanticAgent

from .config import RecursiveConfig

# ------------------------------------------------------------------
# Dependency container
# ------------------------------------------------------------------


@dataclass
class RRDeps(AgenticDeps):
    """Dependencies injected into RR tool calls via ``RunContext``.

    Extends :class:`AgenticDeps` with RR-specific trace and skillbook fields.
    ``sandbox`` is inherited from :class:`AgenticDeps``.

    ``skillbook`` (optional) is the real :class:`Skillbook` — provided so the
    read-only ``search_skillbook`` and ``read_skill`` tools can inspect
    strategies without the agent having to scan serialized text.
    """

    trace_data: dict[str, Any] = field(default_factory=dict)
    skillbook_text: str = ""
    skillbook: Optional[Union[Skillbook, SkillbookView]] = None
    thoughts: list[dict[str, Any]] = field(default_factory=list)


# ------------------------------------------------------------------
# RR-specific tool registrars
# ------------------------------------------------------------------


def register_output_validator(agent: "PydanticAgent[RRDeps, Any]") -> None:
    """Register the standard output validator on any RR agent."""

    @agent.output_validator
    def validate_output(ctx: RunContext[RRDeps], output: Any) -> Any:
        """Ensure the agent explored data before concluding."""
        if ctx.deps.iteration < 1:
            raise ModelRetry(
                "You haven't explored the data enough. "
                "Use execute_code first, "
                "then provide your final ReflectorOutput."
            )
        return output


def register_read_skill(agent: "PydanticAgent[RRDeps, Any]") -> None:
    """Register the ``read_skill`` read-only tool.

    Returns the full skill payload (including counters) for a given ID,
    or a ``not found`` message. No sandbox, no mutation.
    """

    @agent.tool
    def read_skill(ctx: RunContext[RRDeps], skill_id: str) -> dict[str, Any]:
        """Look up a skill by ID."""
        sb = ctx.deps.skillbook
        if sb is None:
            return {"error": "skillbook unavailable"}
        skill = sb.get_skill(skill_id)
        if skill is None:
            return {"error": f"skill not found: {skill_id}"}
        return {
            "id": skill.id,
            "section": skill.section,
            "keywords": list(skill.keywords),
            "issue": skill.issue,
            "insight": skill.insight,
            "active": skill.active,
            "used_count": skill.used_count,
            "helpful_count": skill.helpful_count,
            "harmful_count": skill.harmful_count,
            "neutral_count": skill.neutral_count,
            "occurrences": [source.to_dict() for source in skill.occurrences],
        }


def register_think(agent: "PydanticAgent[RRDeps, Any]") -> None:
    """Register the ``think`` narration channel.

    ``think`` is the home for the model's running narration during a
    tool-use turn — what it just confirmed, what it is checking next, brief
    observations. This keeps prose out of ``execute_code`` stdout, where
    Python should only print compact structured evidence. The final
    conclusion still belongs in ``ReflectorOutput`` (the only sink that
    propagates to the SkillManager); ``think`` notes are surfaced in
    ``output.raw["thoughts"]`` for inspection only.
    """

    @agent.tool
    def think(
        ctx: RunContext[RRDeps],
        thought: str,
        evidence_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Narrate your working state during the run.

        Use this for mid-run prose: "checking the constraint window next",
        "the mismatch is confirmed", "the decisive message is at index 12".
        Use it freely — it is the right home for everything you would
        naturally say while working. The final conclusion still goes in
        ``ReflectorOutput``; reusable data still lives in sandbox variables
        via ``execute_code``.
        """
        normalized = thought.strip()
        if not normalized:
            raise ModelRetry("Thought must be non-empty.")

        refs = [ref.strip() for ref in (evidence_refs or []) if ref.strip()]
        entry = {
            "thought": normalized,
            "evidence_refs": refs,
        }
        ctx.deps.thoughts.append(entry)
        return {
            "ok": True,
            "thought_count": len(ctx.deps.thoughts),
        }


def register_search_skillbook(agent: "PydanticAgent[RRDeps, Any]") -> None:
    """Register the ``search_skillbook`` read-only tool.

    Returns the top-k skills most relevant to the query via embedding
    similarity. Falls back to the first k active skills if embeddings are
    unavailable.
    """

    @agent.tool
    def search_skillbook(
        ctx: RunContext[RRDeps], query: str, top_k: int = 5
    ) -> list[dict[str, Any]]:
        """Search for skills matching a natural-language query."""
        sb = ctx.deps.skillbook
        if sb is None:
            return [{"error": "skillbook unavailable"}]

        from ace.implementations.skill_rendering import retrieve_top_k

        results = retrieve_top_k(sb, query, top_k=top_k)
        return [
            {
                "id": s.id,
                "section": s.section,
                "keywords": list(s.keywords),
                "issue": s.issue,
                "insight": s.insight,
                "active": s.active,
                "used_count": s.used_count,
                "helpful_count": s.helpful_count,
                "harmful_count": s.harmful_count,
                "neutral_count": s.neutral_count,
            }
            for s in results
        ]
