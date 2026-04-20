"""SkillManager tool registrars and dependency container.

The agentic SkillManager operates on the real :class:`Skillbook` via
atomic mutation tools (ADD / UPDATE / REMOVE / TAG) and read-only
inspection tools (search / read). Tools apply changes directly — there
is no staging. Each mutation appends an ``UpdateOperation`` to
``deps.operations`` so the caller can recover an audit trail after the
run.

Generic tools (``execute_code``, ``recurse``) are provided by
:mod:`ace.core.recursive_agent`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, List, Literal, Optional

from pydantic_ai import RunContext

from ace.core.recursive_agent import AgenticDeps
from ace.core.skillbook import Skillbook, UpdateOperation

if TYPE_CHECKING:
    from pydantic_ai import Agent as PydanticAgent


# ------------------------------------------------------------------
# Dependency container
# ------------------------------------------------------------------


@dataclass
class SMDeps(AgenticDeps):
    """Dependencies injected into SkillManager tool calls via ``RunContext``.

    ``skillbook`` is the real :class:`Skillbook` — tools mutate it
    directly. ``operations`` is the audit trail appended by each
    mutation tool; the caller reads it after the run to build
    ``SkillManagerOutput``.
    """

    skillbook: Optional[Skillbook] = None
    operations: List[UpdateOperation] = field(default_factory=list)


# ------------------------------------------------------------------
# Mutation tools
# ------------------------------------------------------------------


def register_add_skill(agent: "PydanticAgent[SMDeps, Any]") -> None:
    """Register ``add_skill`` — ADD a new skill to the skillbook."""

    @agent.tool
    def add_skill(
        ctx: RunContext[SMDeps],
        section: str,
        content: str,
        justification: str = "",
        evidence: str = "",
    ) -> dict[str, Any]:
        """Add a new skill to the skillbook.

        Args:
            section: Target section (e.g. ``"general"``).
            content: The skill content, in imperative voice.
            justification: Why this skill is worth keeping.
            evidence: Concrete trace evidence motivating the add.

        Returns:
            The new skill's ID plus confirmation.
        """
        sb = ctx.deps.skillbook
        if sb is None:
            return {"error": "skillbook unavailable"}
        skill = sb.add_skill(
            section=section,
            content=content,
            justification=justification or None,
            evidence=evidence or None,
        )
        ctx.deps.operations.append(
            UpdateOperation(
                type="ADD",
                section=section,
                content=content,
                skill_id=skill.id,
                justification=justification or None,
                evidence=evidence or None,
            )
        )
        return {"ok": True, "skill_id": skill.id}


def register_update_skill(agent: "PydanticAgent[SMDeps, Any]") -> None:
    """Register ``update_skill`` — UPDATE an existing skill's content."""

    @agent.tool
    def update_skill(
        ctx: RunContext[SMDeps],
        skill_id: str,
        content: Optional[str] = None,
        justification: Optional[str] = None,
        evidence: Optional[str] = None,
    ) -> dict[str, Any]:
        """Update an existing skill's content, justification, or evidence.

        Preserve enumerated items on content updates — do not strip
        lists or criteria.
        """
        sb = ctx.deps.skillbook
        if sb is None:
            return {"error": "skillbook unavailable"}
        skill = sb.update_skill(
            skill_id,
            content=content,
            justification=justification,
            evidence=evidence,
        )
        if skill is None:
            return {"error": f"skill not found: {skill_id}"}
        ctx.deps.operations.append(
            UpdateOperation(
                type="UPDATE",
                section=skill.section,
                skill_id=skill_id,
                content=content,
                justification=justification,
                evidence=evidence,
            )
        )
        return {"ok": True, "skill_id": skill_id}


def register_remove_skill(agent: "PydanticAgent[SMDeps, Any]") -> None:
    """Register ``remove_skill`` — REMOVE a skill from the skillbook."""

    @agent.tool
    def remove_skill(
        ctx: RunContext[SMDeps],
        skill_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Remove a skill. Use when a skill is harmful, duplicated, or vague.

        Args:
            skill_id: ID of the skill to remove.
            reason: One-sentence justification recorded in the audit log.
        """
        sb = ctx.deps.skillbook
        if sb is None:
            return {"error": "skillbook unavailable"}
        skill = sb.get_skill(skill_id)
        if skill is None:
            return {"error": f"skill not found: {skill_id}"}
        section = skill.section
        sb.remove_skill(skill_id)
        ctx.deps.operations.append(
            UpdateOperation(
                type="REMOVE",
                section=section,
                skill_id=skill_id,
                justification=reason,
            )
        )
        return {"ok": True, "skill_id": skill_id}


def register_tag_skill(agent: "PydanticAgent[SMDeps, Any]") -> None:
    """Register ``tag_skill`` — record an effectiveness observation."""

    @agent.tool
    def tag_skill(
        ctx: RunContext[SMDeps],
        skill_id: str,
        delta: Literal[1, -1, 0],
    ) -> dict[str, Any]:
        """Bump a skill's effectiveness counter.

        ``+1`` → helpful_count, ``-1`` → harmful_count, ``0`` → neutral_count.
        Use against each injected skill based on whether it helped, harmed,
        or had no material effect on the outcome.
        """
        sb = ctx.deps.skillbook
        if sb is None:
            return {"error": "skillbook unavailable"}
        skill = sb.tag_skill(skill_id, delta)
        if skill is None:
            return {"error": f"skill not found: {skill_id}"}
        ctx.deps.operations.append(
            UpdateOperation(
                type="TAG",
                section=skill.section,
                skill_id=skill_id,
                metadata={"delta": int(delta)},
            )
        )
        return {
            "ok": True,
            "skill_id": skill_id,
            "helpful_count": skill.helpful_count,
            "harmful_count": skill.harmful_count,
            "neutral_count": skill.neutral_count,
        }


# ------------------------------------------------------------------
# Read-only tools
# ------------------------------------------------------------------


def register_sm_read_skill(agent: "PydanticAgent[SMDeps, Any]") -> None:
    """Register ``read_skill`` — return the full skill payload by ID."""

    @agent.tool
    def read_skill(ctx: RunContext[SMDeps], skill_id: str) -> dict[str, Any]:
        """Look up a skill by ID. Returns content, section, and counters."""
        sb = ctx.deps.skillbook
        if sb is None:
            return {"error": "skillbook unavailable"}
        skill = sb.get_skill(skill_id)
        if skill is None:
            return {"error": f"skill not found: {skill_id}"}
        return {
            "id": skill.id,
            "section": skill.section,
            "content": skill.content,
            "status": skill.status,
            "used_count": skill.used_count,
            "helpful_count": skill.helpful_count,
            "harmful_count": skill.harmful_count,
            "neutral_count": skill.neutral_count,
            "justification": skill.justification,
            "evidence": skill.evidence,
        }


def register_sm_search_skills(agent: "PydanticAgent[SMDeps, Any]") -> None:
    """Register ``search_skills`` — top-k skills by embedding similarity."""

    @agent.tool
    def search_skills(
        ctx: RunContext[SMDeps],
        query: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Retrieve skills most relevant to a natural-language query.

        Use this before ADD to check for near-duplicates, and to discover
        which existing skills a reflection might be refining.
        """
        sb = ctx.deps.skillbook
        if sb is None:
            return [{"error": "skillbook unavailable"}]
        from ace.implementations.skill_rendering import retrieve_top_k

        results = retrieve_top_k(sb, query, top_k=top_k)
        return [
            {
                "id": s.id,
                "section": s.section,
                "content": s.content,
                "used_count": s.used_count,
                "helpful_count": s.helpful_count,
                "harmful_count": s.harmful_count,
                "neutral_count": s.neutral_count,
            }
            for s in results
        ]
