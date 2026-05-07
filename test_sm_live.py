"""Live SkillManager tests — exercise the agentic tool loop against a real model.

Each scenario builds a real ``Skillbook``, constructs a real ``ReflectorOutput``,
runs the agentic ``SkillManager``, then asserts what the tools mutated. Nothing
here uses mocks for the LLM.

Usage::

    OPENAI_API_KEY=... uv run python test_sm_live.py

Set ``LIVE_SM_MODEL`` to override the default model.
"""

from __future__ import annotations

import os
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

from ace.core.outputs import ReflectorOutput
from ace.core.recursive_agent import AgenticConfig
from ace.core.skillbook import Skillbook
from ace.implementations.skill_manager import SkillManager

MODEL = os.environ.get(
    "LIVE_SM_MODEL", "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"
)

# Bedrock model aliases the user asked us to exercise. Set LIVE_SM_MODELS=""
# to disable the cross-model matrix and use only LIVE_SM_MODEL.
DEFAULT_MODELS = [
    "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "bedrock/us.anthropic.claude-sonnet-4-6",
    "bedrock/openai.gpt-oss-120b-1:0",
    "bedrock/minimax.minimax-m2.5",
]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


@dataclass
class Result:
    name: str
    passed: bool
    detail: str
    ops: list[str]


def _fmt_op(op: Any) -> str:
    bits = [op.type]
    if op.skill_id:
        bits.append(op.skill_id)
    if op.content:
        snippet = op.content[:60] + ("…" if len(op.content) > 60 else "")
        bits.append(repr(snippet))
    if op.metadata:
        bits.append(str(op.metadata))
    return " ".join(bits)


def _run_case(
    name: str,
    *,
    skillbook_setup: Callable[[Skillbook], None],
    reflection: ReflectorOutput,
    injected_skill_ids: tuple[str, ...] = (),
    progress: str = "1/1",
    question_context: str = "",
    config: AgenticConfig | None = None,
    assertion: Callable[[Skillbook, list[Any]], str | None] = lambda sb, ops: None,
) -> Result:
    sb = Skillbook()
    skillbook_setup(sb)

    try:
        sm = SkillManager(MODEL, config=config or AgenticConfig(max_requests=15))
        output = sm.update_skills(
            reflections=(reflection,),
            skillbook=sb,
            question_context=question_context,
            progress=progress,
            injected_skill_ids=injected_skill_ids,
        )
    except Exception as e:
        return Result(
            name=name,
            passed=False,
            detail=f"SM crashed: {type(e).__name__}: {e}\n{traceback.format_exc()}",
            ops=[],
        )

    ops = list(output.update.operations)
    error = assertion(sb, ops)
    ops_fmt = [_fmt_op(op) for op in ops]
    reason_preview = (output.update.reasoning or "")[:180]
    detail = (
        f"{error} | reasoning={reason_preview!r}"
        if error
        else f"reasoning={reason_preview!r}"
    )
    return Result(
        name=name,
        passed=error is None,
        detail=detail,
        ops=ops_fmt,
    )


def _print_result(r: Result) -> None:
    mark = "PASS" if r.passed else "FAIL"
    print(f"[{mark}] {r.name}")
    for op in r.ops:
        print(f"       · {op}")
    # Always show detail on failure so we can spot patterns across models.
    if not r.passed or os.environ.get("LIVE_SM_VERBOSE"):
        print(f"       detail: {r.detail}")
    print()


# ----------------------------------------------------------------------
# Scenario builders
# ----------------------------------------------------------------------


def failure_reflection(*, error: str, insight: str, reasoning: str = "") -> ReflectorOutput:
    return ReflectorOutput(
        reasoning=reasoning or f"Agent got the wrong answer. {error}",
        error_identification=error,
        root_cause_analysis="Agent applied an incorrect method / missed a step.",
        correct_approach="Describe the correct method with a concrete example.",
        key_insight=insight,
    )


def success_reflection(*, insight: str, reasoning: str = "") -> ReflectorOutput:
    return ReflectorOutput(
        reasoning=reasoning or "Agent answered correctly using a clean approach.",
        error_identification="none",
        root_cause_analysis="Clean execution; chosen strategy fit the problem.",
        correct_approach="Strategy worked; record so future runs reuse it.",
        key_insight=insight,
    )


# ----------------------------------------------------------------------
# Cases
# ----------------------------------------------------------------------


def case_1_empty_sb_failure() -> Result:
    """Empty skillbook + failure → expect at least one ADD."""
    refl = failure_reflection(
        error="Computed 15*24 as 310 instead of 360 via distributive property.",
        insight="When decomposing a product like a*(b+c), verify a*b and a*c before summing.",
        reasoning=(
            "Agent attempted 15*24 using distributive: 15*(20+4). Wrote 15*20=310. "
            "Should be 300. Off-by-ten arithmetic slip."
        ),
    )
    return _run_case(
        "case_1_empty_sb_failure",
        skillbook_setup=lambda sb: None,
        reflection=refl,
        progress="1/10 correct",
        question_context="Mental arithmetic",
        assertion=lambda sb, ops: (
            None
            if any(op.type == "ADD" for op in ops) and len(sb.skills()) >= 1
            else f"expected >=1 ADD and >=1 skill in book; got ops={[op.type for op in ops]}, skills={len(sb.skills())}"
        ),
    )


def case_2_empty_sb_success() -> Result:
    """Empty skillbook + success with a specific insight → expect ADD."""
    refl = success_reflection(
        insight=(
            "For factual 'capital of X' questions, answer with the capital only, "
            "no extra prose."
        ),
        reasoning=(
            "Agent answered 'Paris' to 'Capital of France?'. Clean factual lookup, "
            "no reasoning noise. Reusable pattern for factual single-entity questions."
        ),
    )
    return _run_case(
        "case_2_empty_sb_success",
        skillbook_setup=lambda sb: None,
        reflection=refl,
        progress="8/10 correct",
        question_context="Factual trivia",
        assertion=lambda sb, ops: (
            None
            if any(op.type == "ADD" for op in ops)
            else f"expected an ADD; got {[op.type for op in ops]}"
        ),
    )


def case_3_tag_helpful_for_injected() -> Result:
    """Injected skill + success → expect a tag_skill(+1) on the injected skill."""
    injected_id = None

    def _setup(sb: Skillbook) -> None:
        nonlocal injected_id
        s = sb.add_skill(
            section="math",
            content="Use distributive property for mental multiplication: a*(b+c) = a*b + a*c.",
        )
        injected_id = s.id

    refl = success_reflection(
        insight="Distributive decomposition works well for 2-digit multiplications.",
        reasoning=(
            f"Agent used the injected skill [{'INJECTED_ID'}] to solve 25*14 = "
            "25*(10+4) = 250+100 = 350. Correct answer. Strategy was directly useful."
        ),
    )

    # We can't know injected_id until setup; patch inside the case runner
    sb = Skillbook()
    _setup(sb)
    assert injected_id is not None
    refl = ReflectorOutput(
        reasoning=(
            f"Agent used the injected skill {injected_id} to solve 25*14 = "
            "25*(10+4) = 250+100 = 350. Correct answer. Strategy was directly useful."
        ),
        error_identification="none",
        root_cause_analysis="Skill directly contributed to correct decomposition.",
        correct_approach="Reuse the distributive strategy for similar 2-digit tasks.",
        key_insight="Distributive decomposition works well for 2-digit multiplications.",
    )

    try:
        sm = SkillManager(MODEL, config=AgenticConfig(max_requests=15))
        output = sm.update_skills(
            reflections=(refl,),
            skillbook=sb,
            question_context="Mental arithmetic",
            progress="9/10 correct",
            injected_skill_ids=(injected_id,),
        )
    except Exception as e:
        return Result(
            "case_3_tag_helpful_for_injected",
            False,
            f"crash: {e}",
            [],
        )

    ops = list(output.update.operations)
    tagged_helpful = any(
        op.type == "TAG"
        and op.skill_id == injected_id
        and op.metadata.get("delta", 0) >= 1
        for op in ops
    )
    skill = sb.get_skill(injected_id)
    counter_ok = skill is not None and skill.helpful_count >= 1
    passed = tagged_helpful and counter_ok
    return Result(
        "case_3_tag_helpful_for_injected",
        passed,
        f"tagged_helpful={tagged_helpful}, helpful_count={skill.helpful_count if skill else 'MISSING'}",
        [_fmt_op(op) for op in ops],
    )


def case_4_tag_harmful_for_injected() -> Result:
    """Injected skill misled the agent → expect tag_skill(-1) or REMOVE, and likely an UPDATE/ADD with a corrected rule."""
    sb = Skillbook()
    bad = sb.add_skill(
        section="math",
        content="Always add numbers left-to-right without regrouping to save time.",
    )
    refl = ReflectorOutput(
        reasoning=(
            f"Agent followed skill {bad.id} (left-to-right without regrouping) and got 48+37=75 "
            "instead of 85. The skill ignored carry. Skill directly caused the error."
        ),
        error_identification="Ignored carry when adding units column (8+7=15).",
        root_cause_analysis=(
            f"Injected skill {bad.id} instructed left-to-right without regrouping, which "
            "drops carries. Incorrect strategy."
        ),
        correct_approach=(
            "Add columns right-to-left and carry over into the next column when the sum "
            "exceeds 9."
        ),
        key_insight="Multi-digit addition requires carrying over units-column overflow.",
    )

    try:
        sm = SkillManager(MODEL, config=AgenticConfig(max_requests=15))
        output = sm.update_skills(
            reflections=(refl,),
            skillbook=sb,
            question_context="Mental arithmetic",
            progress="3/10 correct",
            injected_skill_ids=(bad.id,),
        )
    except Exception as e:
        return Result(
            "case_4_tag_harmful_for_injected",
            False,
            f"crash: {e}",
            [],
        )

    ops = list(output.update.operations)
    tagged_harmful = any(
        op.type == "TAG" and op.skill_id == bad.id and op.metadata.get("delta", 0) <= -1
        for op in ops
    )
    removed = any(op.type == "REMOVE" and op.skill_id == bad.id for op in ops)
    updated = any(op.type == "UPDATE" and op.skill_id == bad.id for op in ops)
    skill = sb.get_skill(bad.id)
    harmful_count = skill.harmful_count if skill is not None else -1
    passed = tagged_harmful or removed or updated
    detail = (
        f"tagged_harmful={tagged_harmful}, removed={removed}, updated={updated}, "
        f"harmful_count={harmful_count}"
    )
    return Result(
        "case_4_tag_harmful_for_injected",
        passed,
        detail,
        [_fmt_op(op) for op in ops],
    )


def case_5_dedup_before_add() -> Result:
    """Near-duplicate skill already exists → SM should UPDATE or tag, not ADD a paraphrase."""
    sb = Skillbook()
    existing = sb.add_skill(
        section="math",
        content="Use distributive property when multiplying two-digit numbers mentally.",
    )
    # Reflection pattern is nearly the same as the existing skill
    refl = failure_reflection(
        error="Agent didn't decompose 18*25 and made a multi-step arithmetic error.",
        insight=(
            "Decompose two-digit multiplications with distributive property rather than "
            "computing directly: avoids long arithmetic mistakes."
        ),
        reasoning=(
            "Pattern: agents repeatedly make errors computing 2-digit multiplications "
            "directly. Decomposition prevents this."
        ),
    )
    try:
        sm = SkillManager(MODEL, config=AgenticConfig(max_requests=15))
        output = sm.update_skills(
            reflections=(refl,),
            skillbook=sb,
            question_context="Mental arithmetic",
            progress="4/10 correct",
            injected_skill_ids=(),
        )
    except Exception as e:
        return Result("case_5_dedup_before_add", False, f"crash: {e}", [])

    ops = list(output.update.operations)
    added = [op for op in ops if op.type == "ADD"]
    updated_existing = any(
        op.type == "UPDATE" and op.skill_id == existing.id for op in ops
    )
    # Pass if either (a) no ADD was created (dedup worked), or (b) the existing skill was UPDATEd
    passed = (len(added) == 0) or updated_existing
    detail = (
        f"added={len(added)}, updated_existing={updated_existing}, "
        f"final_skills={len(sb.skills())}"
    )
    return Result(
        "case_5_dedup_before_add",
        passed,
        detail,
        [_fmt_op(op) for op in ops],
    )


def case_6_remove_harmful_threshold() -> Result:
    """Skill with harmful_count=2 + new harmful evidence → SM should REMOVE or tag again to push to 3."""
    sb = Skillbook()
    bad = sb.add_skill(
        section="api",
        content="Always send requests without retry logic to keep latency low.",
    )
    # Pre-seed harmful_count=2 to simulate prior observations
    bad.harmful_count = 2

    refl = ReflectorOutput(
        reasoning=(
            f"Skill {bad.id} caused a third transient-failure incident — request dropped on a "
            "flaky network path because retries were disabled."
        ),
        error_identification=(
            "Request dropped on transient network error; no retry, user saw a hard failure."
        ),
        root_cause_analysis=(
            f"Skill {bad.id} forbids retries even for transient errors — bad default for "
            "unreliable links."
        ),
        correct_approach=(
            "Retry transient errors (timeouts, 5xx, connection reset) with exponential "
            "backoff up to 3 attempts; only skip retry on idempotent-violating verbs."
        ),
        key_insight="Never disable retries unconditionally; distinguish transient vs terminal.",
    )

    try:
        sm = SkillManager(MODEL, config=AgenticConfig(max_requests=15))
        output = sm.update_skills(
            reflections=(refl,),
            skillbook=sb,
            question_context="API reliability",
            progress="ongoing",
            injected_skill_ids=(bad.id,),
        )
    except Exception as e:
        return Result("case_6_remove_harmful_threshold", False, f"crash: {e}", [])

    ops = list(output.update.operations)
    removed = any(op.type == "REMOVE" and op.skill_id == bad.id for op in ops)
    bumped_to_three = any(
        op.type == "TAG" and op.skill_id == bad.id and op.metadata.get("delta", 0) <= -1
        for op in ops
    )
    skill = sb.get_skill(bad.id)
    final_harmful = skill.harmful_count if skill is not None else "(removed)"
    passed = removed or bumped_to_three
    detail = f"removed={removed}, bumped_harmful={bumped_to_three}, final_harmful_count={final_harmful}"
    return Result(
        "case_6_remove_harmful_threshold",
        passed,
        detail,
        [_fmt_op(op) for op in ops],
    )


def case_7_max_requests_one_shot() -> Result:
    """max_requests=1 degrades to one-shot — still produces a valid audit even if no mutations."""
    refl = failure_reflection(
        error="Minor off-by-one in range loop.",
        insight="When iterating [a, b], confirm whether b is inclusive before coding the loop.",
    )

    try:
        sm = SkillManager(MODEL, config=AgenticConfig(max_requests=1))
        output = sm.update_skills(
            reflections=(refl,),
            skillbook=Skillbook(),
            question_context="Python coding",
            progress="1/1",
            injected_skill_ids=(),
        )
    except Exception as e:
        return Result("case_7_max_requests_one_shot", False, f"crash: {e}", [])

    # Either: SM produced ops (succeeded in one shot) or returned an empty audit (budget exhausted gracefully).
    detail = (
        f"reasoning={output.update.reasoning[:120]!r}, "
        f"ops={len(output.update.operations)}, timeout={output.raw.get('timeout', False)}"
    )
    return Result(
        "case_7_max_requests_one_shot",
        True,  # pass as long as it doesn't crash
        detail,
        [_fmt_op(op) for op in output.update.operations],
    )


def case_8_batch_reflections() -> Result:
    """Two reflections in one call — SM should process both."""
    sb = Skillbook()
    r1 = failure_reflection(
        error="Division by zero not guarded.",
        insight="Always guard against zero divisor before dividing.",
    )
    r2 = success_reflection(
        insight=(
            "Prefer collections.Counter over manual dict-increment for tallying "
            "frequencies — avoids KeyError paths."
        ),
    )
    try:
        sm = SkillManager(MODEL, config=AgenticConfig(max_requests=20))
        output = sm.update_skills(
            reflections=(r1, r2),
            skillbook=sb,
            question_context="Python coding basics",
            progress="5/10 correct",
            injected_skill_ids=(),
        )
    except Exception as e:
        return Result("case_8_batch_reflections", False, f"crash: {e}", [])

    ops = list(output.update.operations)
    added = [op for op in ops if op.type == "ADD"]
    passed = len(added) >= 1
    detail = f"adds={len(added)}, total_ops={len(ops)}, final_skills={len(sb.skills())}"
    return Result(
        "case_8_batch_reflections",
        passed,
        detail,
        [_fmt_op(op) for op in ops],
    )


def case_9_vague_reflection_no_op() -> Result:
    """Purely meta-commentary reflection → SM should produce no ADD (or degrade gracefully)."""
    refl = ReflectorOutput(
        reasoning="Agent should be more careful and think about things deeply.",
        error_identification="Not careful enough.",
        root_cause_analysis="Did not consider options.",
        correct_approach="Be careful. Remember to think about things.",
        key_insight="Consider carefully.",
    )
    try:
        sm = SkillManager(MODEL, config=AgenticConfig(max_requests=15))
        output = sm.update_skills(
            reflections=(refl,),
            skillbook=Skillbook(),
            question_context="General",
            progress="1/1",
            injected_skill_ids=(),
        )
    except Exception as e:
        return Result("case_9_vague_reflection_no_op", False, f"crash: {e}", [])

    ops = list(output.update.operations)
    adds = [op for op in ops if op.type == "ADD"]
    # Pass if the SM declined to add vague skills. If it did ADD, inspect whether content is sharp — allow one if it rewrote it.
    detail = f"adds={len(adds)}, total_ops={len(ops)}, reasoning={output.update.reasoning[:140]!r}"
    passed = len(adds) == 0
    return Result("case_9_vague_reflection_no_op", passed, detail, [_fmt_op(op) for op in ops])


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------


CASES: list[Callable[[], Result]] = [
    case_1_empty_sb_failure,
    case_2_empty_sb_success,
    case_3_tag_helpful_for_injected,
    case_4_tag_harmful_for_injected,
    case_5_dedup_before_add,
    case_6_remove_harmful_threshold,
    case_7_max_requests_one_shot,
    case_8_batch_reflections,
    case_9_vague_reflection_no_op,
]


def main() -> int:
    global MODEL
    models_env = os.environ.get("LIVE_SM_MODELS")
    if models_env is not None:
        models = [m.strip() for m in models_env.split(",") if m.strip()]
    elif os.environ.get("LIVE_SM_MODEL"):
        models = [MODEL]
    else:
        models = DEFAULT_MODELS

    matrix: dict[str, list[Result]] = {}

    for model_id in models:
        MODEL = model_id
        short = model_id.split("/")[-1]
        print(f"\n{'=' * 72}")
        print(f"Model: {short}")
        print("=" * 72 + "\n")
        results: list[Result] = []
        for case in CASES:
            try:
                r = case()
            except Exception as e:
                r = Result(
                    case.__name__,
                    False,
                    f"runner crash: {e}\n{traceback.format_exc()}",
                    [],
                )
            results.append(r)
            _print_result(r)
        matrix[short] = results

    # Summary matrix
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    case_names = [c.__name__ for c in CASES]
    model_names = list(matrix.keys())
    width = max((len(nm) for nm in case_names), default=10)
    print(f"{'case'.ljust(width)}  " + "  ".join(f"{m[:22]:<22}" for m in model_names))
    for i, case_name in enumerate(case_names):
        row = [case_name.ljust(width)]
        for m in model_names:
            r = matrix[m][i]
            row.append(("PASS" if r.passed else "FAIL").ljust(22))
        print("  ".join(row))

    # Totals
    print()
    for m in model_names:
        p = sum(1 for r in matrix[m] if r.passed)
        print(f"  {m[:50]:<50} {p}/{len(CASES)} passed")
    total_passed = sum(
        sum(1 for r in matrix[m] if r.passed) for m in model_names
    )
    total = len(CASES) * len(model_names)
    print(f"\nTotal: {total_passed}/{total} across {len(model_names)} model(s)")
    return 0 if total_passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
