"""AgentStep — runs the Agent role to produce an answer."""

from __future__ import annotations

from ..core.context import ACEStepContext
from ..core.outputs import AgentOutput
from ..core.skillbook import Skillbook
from ..protocols import AgentLike


class AgentStep:
    """Execute the Agent role against the current sample and skillbook.

    Reads the skillbook via ``ctx.skillbook`` (a ``SkillbookView``).  Records
    the set of skills rendered into the agent prompt this run as
    ``ctx.injected_skill_ids`` and bumps ``used_count`` on each — this is the
    ground-truth attribution scope consumed by downstream steps (Reflector/RR,
    SkillManager).  No other upstream counter is touched.
    """

    requires = frozenset({"sample", "skillbook"})
    provides = frozenset({"agent_output", "injected_skill_ids"})

    def __init__(self, agent: AgentLike, skillbook: Skillbook) -> None:
        self.agent = agent
        self.skillbook = skillbook

    def __call__(self, ctx: ACEStepContext) -> ACEStepContext:
        injected_ids = tuple(s.id for s in self.skillbook.skills())

        agent_output: AgentOutput = self.agent.generate(
            question=ctx.sample.question,
            context=ctx.sample.context,
            skillbook=ctx.skillbook,
        )

        self.skillbook.mark_used(injected_ids)

        return ctx.replace(
            agent_output=agent_output,
            injected_skill_ids=injected_ids,
        )
