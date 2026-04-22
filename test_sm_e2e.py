"""Challenging end-to-end test for the agentic SkillManager.

Runs the full ACE pipeline (Agent → Evaluate → Reflect → Update) over a
math-word-problem benchmark for multiple epochs. Measures whether the
skillbook actually improves the Agent's accuracy and whether the SM keeps
the skillbook hygienic across many mutations.

Usage::

    uv run python test_sm_e2e.py
    LIVE_E2E_MODEL=bedrock/us.anthropic.claude-sonnet-4-6 uv run python test_sm_e2e.py
    LIVE_E2E_EPOCHS=3 uv run python test_sm_e2e.py

Default: haiku 4.5 on Bedrock, 2 epochs, 15 samples. ~60–90 LLM calls.
"""

from __future__ import annotations

import os
import statistics
import sys
import time
from collections import Counter
from typing import Any

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

from ace import (
    ACE,
    Agent,
    Reflector,
    Sample,
    SimpleEnvironment,
    SkillManager,
    Skillbook,
)
from ace.core.recursive_agent import AgenticConfig
from ace.deduplication.detector import SimilarityDetector
from ace.protocols.deduplication import DeduplicationConfig

MODEL = os.environ.get(
    "LIVE_E2E_MODEL", "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"
)
EPOCHS = int(os.environ.get("LIVE_E2E_EPOCHS", "2"))

# Math-word-problem benchmark. Mix of one-step, multi-step, and subtle
# traps (percentage base, unit conversion, order of operations). Small
# enough to run in a few minutes; large enough that single-sample noise
# doesn't dominate the signal.
# Adversarial benchmark — hand-picked problems that frontier small models
# frequently miss (classic traps, subtle word-problem wording, counting that
# looks easy but rewards systematic care). Ground truths are concise so
# SimpleEnvironment's substring match is robust.
SAMPLES: list[Sample] = [
    # Classic "Boy-born-on-Tuesday" conditional-probability twist (without
    # the day-of-week qualifier; the twist is recognising the 1/3 answer).
    Sample(
        question=(
            "A family has two children. You learn that at least one of them is "
            "a boy. What is the probability that both children are boys? "
            "Assume boys and girls are equally likely and independent. "
            "Answer as a fraction in lowest terms."
        ),
        ground_truth="1/3",
    ),
    # Monty Hall — frequently missed.
    Sample(
        question=(
            "In the Monty Hall problem with three doors (one car, two goats), "
            "you pick a door, the host opens a different door revealing a goat, "
            "and offers to let you switch. What is the probability of winning "
            "the car if you always switch? Answer as a fraction."
        ),
        ground_truth="2/3",
    ),
    # Birthday-ish
    Sample(
        question=(
            "In a group of 23 people, what is the probability that at least two "
            "share a birthday (ignoring leap years)? Round to 2 decimal places."
        ),
        ground_truth="0.51",
    ),
    # Percentage-base trap
    Sample(
        question=(
            "A store raises a $50 item by 20%, then lowers the new price by 20%. "
            "What is the final price in dollars?"
        ),
        ground_truth="48",
    ),
    # Reverse-percentage — the trap is confusing discount base.
    Sample(
        question=(
            "An item is sold for $63 after a 30% discount. What was the original "
            "price in dollars?"
        ),
        ground_truth="90",
    ),
    # Compound interest with trap.
    Sample(
        question=(
            "You invest $1000 at 10% annual interest, compounded annually, for "
            "3 years. What is the final value in dollars? Round to the nearest "
            "whole dollar."
        ),
        ground_truth="1331",
    ),
    # Rate word problem — classic "work together" trap.
    Sample(
        question=(
            "Alice can paint a room in 6 hours. Bob can paint the same room in "
            "4 hours. How many hours would it take them working together? "
            "Answer as a fraction in lowest terms."
        ),
        ground_truth="12/5",
    ),
    # Age problem — multi-step algebra with a slightly awkward wording.
    Sample(
        question=(
            "Mary is twice as old as her brother. In 10 years, she will be 1.5 "
            "times his age. How old is Mary now?"
        ),
        ground_truth="20",
    ),
    # Counting / combinatorics trap.
    Sample(
        question=(
            "How many different ways can the letters of the word 'MISSISSIPPI' "
            "be arranged?"
        ),
        ground_truth="34650",
    ),
    # Combinatorics — distinct-handshakes.
    Sample(
        question=(
            "Ten people at a party each shake hands exactly once with every "
            "other person. How many handshakes occur in total?"
        ),
        ground_truth="45",
    ),
    # Number-theory trap: trailing zeros in 100!
    Sample(
        question="How many trailing zeros are in 100 factorial (100!)?",
        ground_truth="24",
    ),
    # Classic rate problem — mixture.
    Sample(
        question=(
            "How many liters of pure water must be added to 30 liters of a 40% "
            "salt solution to dilute it to a 25% salt solution?"
        ),
        ground_truth="18",
    ),
    # Rate-distance-time with unit trap.
    Sample(
        question=(
            "A train travels 150 kilometers in 1 hour 15 minutes. What is its "
            "speed in kilometers per hour?"
        ),
        ground_truth="120",
    ),
    # Averages trap.
    Sample(
        question=(
            "A student's average on four tests is 85. What score on a fifth "
            "test would raise her average to 87?"
        ),
        ground_truth="95",
    ),
    # Geometry — area of annulus.
    Sample(
        question=(
            "A circular ring (annulus) has an outer radius of 10 and an inner "
            "radius of 6. What is its area? Express in terms of pi."
        ),
        ground_truth="64pi",
    ),
    # Logic/wording trap — classic "how many X does each sibling have".
    Sample(
        question=(
            "If a brother has as many sisters as brothers, and each of his "
            "sisters has twice as many brothers as sisters, how many boys and "
            "girls are in the family? Give the total number of children."
        ),
        ground_truth="7",
    ),
    # Rate problem with ratio twist.
    Sample(
        question=(
            "If it takes 5 machines 5 minutes to make 5 widgets, how long would "
            "it take 100 machines to make 100 widgets? Answer in minutes."
        ),
        ground_truth="5",
    ),
    # Lily-pad doubling trap.
    Sample(
        question=(
            "A lily pad doubles in size every day. It takes 48 days to cover a "
            "pond. On what day did it cover half the pond?"
        ),
        ground_truth="47",
    ),
    # Bat-and-ball cost — CRT classic.
    Sample(
        question=(
            "A bat and a ball cost $1.10 in total. The bat costs $1.00 more "
            "than the ball. How much does the ball cost in cents?"
        ),
        ground_truth="5",
    ),
    # Geometric — Pythagorean with subtle unit.
    Sample(
        question=(
            "A 13-foot ladder leans against a wall. The bottom is 5 feet from "
            "the wall. How high up the wall does the ladder reach, in feet?"
        ),
        ground_truth="12",
    ),
]


def _accuracy(results: list[Any]) -> tuple[float, int, int]:
    correct = 0
    total = 0
    for r in results:
        ctx = getattr(r, "output", None)
        if ctx is None or ctx.agent_output is None or ctx.sample is None:
            continue
        gt = (ctx.sample.ground_truth or "").strip().lower()
        ans = (ctx.agent_output.final_answer or "").strip().lower()
        if not gt:
            continue
        if gt in ans:
            correct += 1
        total += 1
    return (correct / total if total else 0.0, correct, total)


def _operation_histogram(skillbook: Skillbook) -> Counter:
    """Count operations implied by current skillbook state.

    We infer from the skills that exist: an ADD was performed for each
    active skill. UPDATEs and TAGs aren't visible from the final state
    alone — we track them separately via SM outputs."""
    return Counter({"skills_end": len(skillbook.skills())})


def _counter_stats(skillbook: Skillbook) -> dict[str, float]:
    skills = skillbook.skills()
    if not skills:
        return {}
    return {
        "skills_total": len(skills),
        "used_sum": sum(s.used_count for s in skills),
        "helpful_sum": sum(s.helpful_count for s in skills),
        "harmful_sum": sum(s.harmful_count for s in skills),
        "neutral_sum": sum(s.neutral_count for s in skills),
        "used_mean": statistics.mean(s.used_count for s in skills),
        "helpful_mean": statistics.mean(s.helpful_count for s in skills),
        "harmful_mean": statistics.mean(s.harmful_count for s in skills),
    }


def _near_duplicate_pairs(skillbook: Skillbook, threshold: float = 0.85) -> int:
    """Count pairs of active skills with cosine similarity >= threshold."""
    detector = SimilarityDetector(DeduplicationConfig())
    detector.ensure_embeddings(skillbook)
    skills = [s for s in skillbook.skills() if s.embedding is not None]
    pairs = 0
    for i in range(len(skills)):
        for j in range(i + 1, len(skills)):
            sim = detector.cosine_similarity(skills[i].embedding, skills[j].embedding)
            if sim >= threshold:
                pairs += 1
    return pairs


def _print_header(msg: str) -> None:
    bar = "=" * 72
    print(f"\n{bar}\n{msg}\n{bar}")


def _print_skills(skillbook: Skillbook, limit: int = 40) -> None:
    for s in skillbook.skills()[:limit]:
        counters = f"(u={s.used_count},+{s.helpful_count},-{s.harmful_count},={s.neutral_count})"
        content = s.content[:90] + ("…" if len(s.content) > 90 else "")
        print(f"  [{s.id}] {counters} {content}")


def main() -> int:
    _print_header(f"E2E SkillManager test — model={MODEL}  epochs={EPOCHS}  N={len(SAMPLES)}")

    skillbook = Skillbook()
    ace = ACE.from_roles(
        agent=Agent(MODEL),
        reflector=Reflector(MODEL),
        skill_manager=SkillManager(
            MODEL, config=AgenticConfig(max_requests=12)
        ),
        environment=SimpleEnvironment(),
        skillbook=skillbook,
    )

    per_epoch_accuracy: list[float] = []
    per_epoch_skill_count: list[int] = []
    per_epoch_stats: list[dict[str, float]] = []
    wall_times: list[float] = []

    for epoch in range(1, EPOCHS + 1):
        _print_header(f"Epoch {epoch}/{EPOCHS}")
        t0 = time.time()
        results = ace.run(SAMPLES, epochs=1)
        wall = time.time() - t0
        wall_times.append(wall)

        acc, correct, total = _accuracy(results)
        per_epoch_accuracy.append(acc)
        per_epoch_skill_count.append(len(skillbook.skills()))
        per_epoch_stats.append(_counter_stats(skillbook))

        print(f"  accuracy:      {acc:.2%}  ({correct}/{total})")
        print(f"  skillbook:     {len(skillbook.skills())} skills")
        print(f"  wall:          {wall:.1f}s")
        _print_skills(skillbook, limit=8)

    # Final diagnostics
    _print_header("Final skillbook")
    _print_skills(skillbook, limit=40)

    _print_header("Hygiene metrics")
    dup_pairs = _near_duplicate_pairs(skillbook)
    stats = _counter_stats(skillbook)
    print(f"  near-duplicate pairs (cos>=0.85): {dup_pairs}")
    print(f"  counter stats: {stats}")

    _print_header("Summary")
    print(f"  model: {MODEL}")
    print(f"  samples: {len(SAMPLES)}  epochs: {EPOCHS}")
    for i, (acc, count, wall) in enumerate(
        zip(per_epoch_accuracy, per_epoch_skill_count, wall_times), 1
    ):
        print(f"  epoch {i}: acc={acc:.2%}  skills={count}  wall={wall:.1f}s")

    # Pass criteria
    print()
    checks: list[tuple[str, bool, str]] = []

    # 1. No regression: final epoch accuracy >= first epoch (allow a small slack for noise)
    if len(per_epoch_accuracy) >= 2:
        delta = per_epoch_accuracy[-1] - per_epoch_accuracy[0]
        noise_slack = 1.0 / len(SAMPLES)  # 1-sample worth of noise
        no_regression = delta >= -noise_slack
        checks.append(
            (
                "no accuracy regression vs. epoch 1",
                no_regression,
                f"Δ = {delta:+.2%} (slack ±{noise_slack:.2%})",
            )
        )

    # 2. Skillbook is bounded — shouldn't blow up past 2 skills per sample processed
    max_reasonable_skills = len(SAMPLES) * EPOCHS * 2
    bounded = len(skillbook.skills()) <= max_reasonable_skills
    checks.append(
        (
            "skillbook size bounded",
            bounded,
            f"{len(skillbook.skills())} skills / ceiling {max_reasonable_skills}",
        )
    )

    # 3. At least some skills were created — otherwise SM isn't working
    any_skills = len(skillbook.skills()) >= 1
    checks.append(
        (
            "SM created >=1 skill",
            any_skills,
            f"final skills = {len(skillbook.skills())}",
        )
    )

    # 4. Near-duplicate rate stays low
    dup_rate_ok = len(skillbook.skills()) == 0 or dup_pairs / max(len(skillbook.skills()), 1) < 0.3
    checks.append(
        (
            "near-duplicate rate <30% of skill count",
            dup_rate_ok,
            f"{dup_pairs} pairs / {len(skillbook.skills())} skills",
        )
    )

    # 5. Some skills got used (Agent step bumped used_count) — sanity on injection tracking
    if len(skillbook.skills()) >= 1:
        any_used = any(s.used_count > 0 for s in skillbook.skills())
        # used_count only ticks on skills that existed *before* the Agent runs,
        # so epoch-1 skills will only be used in epoch 2. Only assert when epochs>=2.
        if EPOCHS >= 2:
            checks.append(
                (
                    "injected_skill_ids bumped used_count",
                    any_used,
                    f"{sum(1 for s in skillbook.skills() if s.used_count > 0)} skills have used_count>0",
                )
            )

    all_pass = all(passed for _, passed, _ in checks)
    for name, passed, detail in checks:
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name}  — {detail}")

    print()
    if all_pass:
        print("All checks passed.")
        return 0
    else:
        print("One or more checks failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
