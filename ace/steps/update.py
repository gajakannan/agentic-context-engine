"""UpdateStep — runs the SkillManager, which mutates the skillbook in place."""

from __future__ import annotations

from ..core.context import ACEStepContext
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

    def __init__(
        self, skill_manager: SkillManagerLike, skillbook: Skillbook
    ) -> None:
        self.skill_manager = skill_manager
        self.skillbook = skillbook

    def __call__(self, ctx: ACEStepContext) -> ACEStepContext:
        progress = f"Epoch {ctx.epoch}/{ctx.total_epochs}"
        if ctx.total_steps is not None:
            progress += f", sample {ctx.step_index}/{ctx.total_steps}"

        question_context = ""
        if isinstance(ctx.trace, dict):
            q = ctx.trace.get("question", "")
            c = ctx.trace.get("context", "")
            question_context = f"{q}\n{c}".strip() if c else q

        output = self.skill_manager.update_skills(
            reflections=ctx.reflections,
            skillbook=self.skillbook,
            question_context=question_context,
            progress=progress,
            injected_skill_ids=ctx.injected_skill_ids,
        )
        return ctx.replace(skill_manager_output=output.update)
