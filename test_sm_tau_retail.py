"""E2E tau-bench retail test with logfire tracing.

Phases:
  1. Baseline: run N retail tasks with no skillbook -> collect traces, rewards.
  2. Learn: feed traces through the new agentic SkillManager -> build skillbook.
  3. Replay: run the same N tasks with the trained skillbook -> new rewards.

The TauBenchRunner already emits rich logfire spans ("tau task run",
"tau task trace", "tau task outcome"). PydanticAI is auto-instrumented by
configure_logfire(), so every SkillManager tool call shows up in logfire
as a span.

Usage::

    uv run python test_sm_tau_retail.py

Env:
    LOGFIRE_TOKEN            — write token (instrumented runs)
    LOGFIRE_READ_TOKEN       — read token (used by the script to fetch
                               spans for verification after the run)
    AWS_BEARER_TOKEN_BEDROCK — Bedrock auth for haiku-4.5
    OPENAI_API_KEY           — for tau-bench's mandatory gpt-4.1 user sim
"""

from __future__ import annotations

import logging
import os
import sys
import time
from types import MappingProxyType
from typing import Any

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

# Tau2 hardcodes gpt-4.1 (via OpenAI) for the NL-assertion judge, the
# env interface, etc. Redirect to a Bedrock-hosted model before any
# tau2 modules import these constants so the Bedrock-only rule holds.
_BEDROCK_JUDGE = "bedrock/openai.gpt-oss-120b-1:0"
import tau2.config as _tau2_config  # noqa: E402
import tau2.evaluator.evaluator_nl_assertions as _nl_mod  # noqa: E402

for _mod in (_tau2_config, _nl_mod):
    for _attr in (
        "DEFAULT_LLM_AGENT",
        "DEFAULT_LLM_USER",
        "DEFAULT_LLM_NL_ASSERTIONS",
        "DEFAULT_LLM_ENV_INTERFACE",
    ):
        if hasattr(_mod, _attr):
            setattr(_mod, _attr, _BEDROCK_JUDGE)

# Retail tasks' reward_basis includes NL_ASSERTION. tau2.run.run_task's
# default evaluation_type is ALL (no NL eval), which raises. Force
# ALL_WITH_NL_ASSERTIONS so the judge actually runs and a numeric reward
# is produced.
import tau2.run as _tau2_run  # noqa: E402
from tau2.evaluator.evaluator import EvaluationType as _EvalType  # noqa: E402

_original_run_task = _tau2_run.run_task


def _run_task_with_nl(*args, **kwargs):
    kwargs.setdefault("evaluation_type", _EvalType.ALL_WITH_NL_ASSERTIONS)
    return _original_run_task(*args, **kwargs)


_tau2_run.run_task = _run_task_with_nl

# Configure logfire BEFORE any pydantic_ai agents are built so
# instrumentation attaches cleanly.
from ace.observability import configure_logfire

_LOGFIRE_OK = configure_logfire()

import logfire  # noqa: E402

RUN_TAG = f"sm_retail_e2e_{int(time.time())}"
logfire.info("test_sm_tau_retail.start", run_tag=RUN_TAG)

# Emit a root span so we can query logfire for everything this run emitted.
_root_span = logfire.span("sm_retail_e2e_run", run_tag=RUN_TAG)
_root_span.__enter__()

from ace.core.context import ACEStepContext  # noqa: E402
from ace.core.recursive_agent import AgenticConfig  # noqa: E402
from ace.core.skillbook import Skillbook  # noqa: E402
from ace.implementations.rr.config import RecursiveConfig  # noqa: E402
from ace.implementations.skill_manager import SkillManager  # noqa: E402
from ace.steps.rr_step import RRStep  # noqa: E402
from ace.steps.update import UpdateStep  # noqa: E402
from ace_eval.e2e.benchmarks.tau_bench import TauBenchRunner  # noqa: E402

AGENT_MODEL = "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"
SM_MODEL = "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"
# Bedrock-only: user sim uses gpt-oss-120b on Bedrock instead of the
# tau-bench-canonical OpenAI gpt-4.1. Expect slightly lower rewards per
# tau-bench's guidance; acceptable for validating the SM loop.
USER_MODEL = "bedrock/openai.gpt-oss-120b-1:0"
TASK_INDICES = (0, 1, 2)  # small slice for speed
MAX_STEPS = 50

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# quieten noisy loggers
for name in (
    "httpx",
    "LiteLLM",
    "litellm",
    "tau2",
    "ace.core.recursive_agent",
    "pipeline",
):
    logging.getLogger(name).setLevel(logging.WARNING)
log = logging.getLogger("sm.tau_retail")


def _run_phase(
    runner: TauBenchRunner,
    task_indices: tuple[int, ...],
    *,
    phase: str,
    skillbook_prompt: str | None,
) -> list[dict[str, Any]]:
    """Run each task, return list of outcome dicts."""
    log.info("phase=%s indices=%s skillbook=%s", phase, task_indices, bool(skillbook_prompt))
    results = []
    with logfire.span(
        "phase", phase=phase, run_tag=RUN_TAG, task_count=len(task_indices)
    ):
        for idx in task_indices:
            out = runner.run_task(
                idx,
                skillbook_prompt=skillbook_prompt,
                run_phase=phase,
                trial=0,
            )
            log.info(
                "  task %d: reward=%.2f outcome=%s wall=%.1fs",
                idx,
                out.reward,
                out.outcome.value,
                out.wall_clock_seconds or 0.0,
            )
            results.append(
                {
                    "task_index": idx,
                    "reward": out.reward,
                    "outcome": out.outcome.value,
                    "trace": dict(out.trace) if out.trace else None,
                    "wall": out.wall_clock_seconds or 0.0,
                    "error": out.error,
                }
            )
    return results


def _feed_trace_to_pipeline(
    trace: dict[str, Any],
    *,
    reflect_step: RRStep,
    update_step: UpdateStep,
    skillbook: Skillbook,
) -> tuple[Any, Any]:
    """Run RRStep → UpdateStep on a single trace."""
    from ace.core.context import SkillbookView

    ctx = ACEStepContext(
        sample=None,
        skillbook=SkillbookView(skillbook),
        trace=MappingProxyType(trace),
        injected_skill_ids=(),
    )
    ctx1 = reflect_step(ctx)
    ctx2 = update_step(ctx1)
    return ctx1.reflections, ctx2.skill_manager_output


def _skill_summary(skillbook: Skillbook) -> str:
    lines = [f"{len(skillbook.skills())} skills total:"]
    for s in skillbook.skills():
        counters = f"u={s.used_count},+{s.helpful_count},-{s.harmful_count},={s.neutral_count}"
        snippet = s.content[:100] + ("…" if len(s.content) > 100 else "")
        lines.append(f"  [{s.id}] ({counters}) {snippet}")
    return "\n".join(lines)


def _fetch_logfire_spans(run_tag: str) -> dict[str, Any] | None:
    """Pull back spans for this run via the Logfire read API.

    Returns a dict with counts and a few representative span names, or
    None if read-token not configured / HTTP fails.
    """
    token = os.environ.get("LOGFIRE_READ_TOKEN")
    if not token:
        log.warning("LOGFIRE_READ_TOKEN not set; skipping verification")
        return None

    import httpx

    try:
        # Give logfire a few seconds to flush
        logfire.force_flush()
    except Exception:
        pass

    # Logfire read API: GET /v1/query with ?sql=... and Bearer auth.
    # We tag only our top-level spans with run_tag. Broader view: recent
    # spans that likely belong to this run — SM tool names + pydantic-ai
    # chat/agent-run spans — so we can see whether SM tools actually fired.
    url = "https://logfire-us.pydantic.dev/v1/query"
    q = (
        "SELECT span_name, attributes "
        "FROM records "
        f"WHERE (attributes->>'run_tag' = '{run_tag}' "
        "   OR span_name IN ('rr.session','add_skill','update_skill','remove_skill',"
        "                    'tag_skill','search_skills','read_skill','execute_code',"
        "                    'agent run','chat','tau task run','tau task outcome')) "
        "  AND start_timestamp > now() - INTERVAL '30 minutes' "
        "ORDER BY start_timestamp DESC LIMIT 500"
    )
    r = None
    for method in ("GET", "POST"):
        try:
            if method == "GET":
                r = httpx.get(
                    url,
                    params={"sql": q},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=30.0,
                )
            else:
                r = httpx.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/sql",
                    },
                    content=q,
                    timeout=30.0,
                )
            if r.status_code == 200:
                break
        except Exception as e:
            log.warning("logfire query (%s) crashed: %s", method, e)
            continue

    if r is None or r.status_code != 200:
        log.warning(
            "logfire query failed: status=%s body=%s",
            r.status_code if r is not None else "none",
            (r.text[:400] if r is not None else ""),
        )
        return None

    try:
        data = r.json()
    except Exception:
        log.warning("logfire response parse failed; body=%s", r.text[:400])
        return None

    # Logfire returns column-oriented arrow-like payloads: {columns: [{name,values},...]}
    name_counter: dict[str, int] = {}
    total = 0
    if isinstance(data, dict) and isinstance(data.get("columns"), list):
        span_col = next(
            (c for c in data["columns"] if c.get("name") == "span_name"), None
        )
        if span_col:
            for name in span_col.get("values", []):
                if isinstance(name, str):
                    name_counter[name] = name_counter.get(name, 0) + 1
                    total += 1
    elif isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                name = row.get("span_name") or row.get("name")
                if isinstance(name, str):
                    name_counter[name] = name_counter.get(name, 0) + 1
                    total += 1

    return {"total": total, "span_counts": name_counter}


def main() -> int:
    log.info("logfire=%s  run_tag=%s", _LOGFIRE_OK, RUN_TAG)
    log.info("agent=%s  sm=%s  user=%s", AGENT_MODEL, SM_MODEL, USER_MODEL)

    # --- Build roles & skillbook
    skillbook = Skillbook()
    # RR handles long multi-turn traces via execute_code; simple Reflector
    # collapses them into a single LLM turn and gives up.
    reflect_step = RRStep(
        SM_MODEL,
        config=RecursiveConfig(max_requests=15, max_tokens=200_000),
    )
    skill_manager = SkillManager(
        SM_MODEL,
        config=AgenticConfig(max_requests=15),
    )
    update_step = UpdateStep(skill_manager, skillbook)

    # --- Build tau runner
    runner = TauBenchRunner(
        domain="retail",
        agent_model=AGENT_MODEL,
        user_model=USER_MODEL,
        user_strategy="llm",
        max_num_steps=MAX_STEPS,
        seed=300,
    )
    log.info(
        "retail total_tasks=%s  picked=%s", runner.total_tasks, TASK_INDICES
    )

    # --- Phase 1: baseline
    baseline = _run_phase(runner, TASK_INDICES, phase="baseline", skillbook_prompt=None)
    baseline_reward = sum(r["reward"] for r in baseline) / len(baseline)
    log.info("baseline mean reward: %.2f", baseline_reward)

    # --- Phase 2: learn from baseline traces
    log.info("--- learning phase ---")
    for b in baseline:
        if b["trace"] is None:
            continue
        with logfire.span(
            "learn_from_trace",
            run_tag=RUN_TAG,
            task_index=b["task_index"],
            reward=b["reward"],
        ):
            reflections, sm_out = _feed_trace_to_pipeline(
                b["trace"],
                reflect_step=reflect_step,
                update_step=update_step,
                skillbook=skillbook,
            )
            log.info(
                "  task %d: reflection key_insight=%r  ops=%d",
                b["task_index"],
                (reflections[0].key_insight if reflections else "")[:120],
                len(sm_out.operations) if sm_out else 0,
            )

    log.info("--- skillbook after learning ---\n%s", _skill_summary(skillbook))

    # --- Phase 3: replay with trained skillbook
    sb_prompt = skillbook.as_prompt()
    log.info("skillbook prompt bytes: %d", len(sb_prompt))
    if not sb_prompt:
        log.warning("empty skillbook — skipping replay phase")
        replay_reward = None
        replay = []
    else:
        replay = _run_phase(
            runner, TASK_INDICES, phase="replay", skillbook_prompt=sb_prompt
        )
        replay_reward = sum(r["reward"] for r in replay) / len(replay)
        log.info("replay mean reward: %.2f", replay_reward)

    # --- Verify: pull back logfire spans
    log.info("--- verifying logfire spans ---")
    span_report = _fetch_logfire_spans(RUN_TAG)
    if span_report is None:
        log.info("no logfire verification performed")
    else:
        log.info("logfire spans retrieved: %s", span_report)

    # --- Summary
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(f"run_tag:        {RUN_TAG}")
    print(f"tasks:          {TASK_INDICES}")
    print(f"baseline reward: {baseline_reward:.2f}  ({[r['reward'] for r in baseline]})")
    if replay_reward is not None:
        print(f"replay reward:   {replay_reward:.2f}  ({[r['reward'] for r in replay]})")
        print(f"delta:           {replay_reward - baseline_reward:+.2f}")
    print(f"skills created:  {len(skillbook.skills())}")
    print(f"counters (sum):  used={sum(s.used_count for s in skillbook.skills())}  "
          f"helpful={sum(s.helpful_count for s in skillbook.skills())}  "
          f"harmful={sum(s.harmful_count for s in skillbook.skills())}  "
          f"neutral={sum(s.neutral_count for s in skillbook.skills())}")
    if span_report:
        print(f"logfire spans:   {span_report.get('total','?')} records")
        # top 10 span names
        counts = span_report.get("span_counts", {}) or {}
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:10]
        for n, c in top:
            print(f"   {c:4d}  {n}")

    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        try:
            _root_span.__exit__(None, None, None)
            logfire.force_flush()
        except Exception:
            pass
    sys.exit(rc)
