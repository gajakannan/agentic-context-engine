"""ExportSkillbookMarkdownStep — writes the skillbook as a human-readable markdown file."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from ..core.context import ACEStepContext
from ..core.skillbook import Skillbook


class ExportSkillbookMarkdownStep:
    """Export the skillbook as a markdown file after each learning cycle.

    Rewrites the file from scratch on every invocation so the markdown
    always reflects the current state of the skillbook.
    """

    requires: frozenset[str] = frozenset({"skillbook"})
    provides: frozenset[str] = frozenset()

    def __init__(self, path: str | Path, skillbook: Skillbook) -> None:
        self.path = Path(path)
        self.skillbook = skillbook

    def __call__(self, ctx: ACEStepContext) -> ACEStepContext:
        skills = self.skillbook.skills()
        if not skills:
            return ctx

        by_section: dict[str, list] = defaultdict(list)
        for skill in skills:
            by_section[skill.section].append(skill)

        lines: list[str] = ["# ACE Skillbook", ""]

        for section in sorted(by_section):
            lines.append(f"## {section}")
            lines.append("")
            for skill in by_section[section]:
                tags = (
                    f"helpful={skill.helpful_count}, "
                    f"harmful={skill.harmful_count}, "
                    f"neutral={skill.neutral_count}"
                )
                lines.append(f"### `{skill.id}`")
                lines.append("")
                lines.append(f"**Keywords:** {', '.join(skill.keywords)}")
                lines.append("")
                lines.append(f"**Issue:** {skill.issue}")
                lines.append("")
                if skill.insight:
                    lines.append(f"**Insight:** {skill.insight}")
                    lines.append("")
                if skill.occurrences:
                    lines.append(f"**Occurrences:** {len(skill.occurrences)}")
                    lines.append("")
                lines.append(f"*Tags: {tags}*")
                lines.append("")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n".join(lines))
        return ctx
