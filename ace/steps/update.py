"""UpdateStep — runs the SkillManager, which mutates the skillbook in place."""

from __future__ import annotations

from ..core.context import ACEStepContext
from ..core.insight_source import InsightSource, infer_trace_identity
from ..core.skillbook import Skillbook
from ..protocols import SkillManagerLike


class UpdateStep:
    """Run the agentic SkillManager against the current reflection.

    The SkillManager mutates the real :class:`Skillbook` directly through
    its tools (``add_skill`` / ``update_skill`` / ``remove_skill`` /
    ``tag_skill``). By the time this step returns the skillbook already
    reflects the changes — ``ctx.skill_manager_output`` is a post-hoc
    audit log, not a plan to apply.

    ``max_workers = 1`` because the SM reads the current skillbook state
    and mutates it; concurrent calls would see stale state and race on
    writes.
    """

    requires = frozenset({"reflections", "skillbook"})
    provides = frozenset({"skill_manager_output"})

    max_workers = 1

    def __init__(self, skill_manager: SkillManagerLike, skillbook: Skillbook) -> None:
        self.skill_manager = skill_manager
        self.skillbook = skillbook

    def __call__(self, ctx: ACEStepContext) -> ACEStepContext:
        progress = f"Epoch {ctx.epoch}/{ctx.total_epochs}"
        if ctx.total_steps is not None:
            progress += f", sample {ctx.step_index}/{ctx.total_steps}"

        sample = getattr(ctx, "sample", None)
        sample_question = getattr(sample, "question", "") or ""
        sample_context = getattr(sample, "context", "") or ""

        question_context = ""
        if isinstance(ctx.trace, dict):
            q = str(ctx.trace.get("question", "") or "")
            c = str(ctx.trace.get("context", "") or "")
            question_context = f"{q}\n{c}".strip() if c else q
        elif sample_question:
            question_context = (
                f"{sample_question}\n{sample_context}".strip()
                if sample_context
                else sample_question
            )

        identity = infer_trace_identity(
            trace=ctx.trace,
            sample=sample,
            metadata=ctx.metadata,
        )
        reflection = ctx.reflections[0]
        source = InsightSource(
            trace_uid=identity.trace_uid or "",
            source_system=identity.source_system,
            trace_id=identity.trace_id,
            display_name=identity.display_name,
            sample_question=sample_question or None,
            epoch=ctx.epoch,
            error_identification=reflection.error_identification or None,
            learning_text=reflection.key_insight or None,
        )

        output = self.skill_manager.update_skills(
            reflections=ctx.reflections,
            skillbook=self.skillbook,
            question_context=question_context,
            progress=progress,
            source=source,
            injected_skill_ids=ctx.injected_skill_ids,
        )
        return ctx.replace(skill_manager_output=output.update)
