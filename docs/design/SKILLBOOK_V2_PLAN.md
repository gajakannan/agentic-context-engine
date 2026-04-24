# Skillbook v2 — Design & Execution Plan

Status: approved. This document is the frozen design for the skillbook refactor.

---

## Vision

- **Issues are the primary object.** A skill's core content is a prose description of the problem it addresses, with scope expressed inline.
- **Insights are mandatory for context skills, optional for harness.** Context skills must carry the imperative action the agent should follow — that's the whole point. Harness skills may be pure issue catalogs (a problem in the runtime environment, no agent-side workaround available yet); if a harness workaround does exist, it goes in `insight`.
- **Fine-grained categories stay structured.** The old specific category / topic labels are preserved as `keywords`; they do not get collapsed into free text and they are not replaced by the binary `section`.
- **Scope is emergent.** New issues start narrow; the SkillManager widens the scope text recursively as it sees the same issue recur across domains/traces. No separate scope field — widening = rewriting the `issue` prose.
- **Skillbook search can go hybrid.** BM25 + dense, fused via RRF. Flat structure retained.
- **Dashboard-first mindset.** Schema + provenance must support issue dashboards, occurrence heatmaps, effectiveness KPIs.

---

## Final `Skill` schema

```python
@dataclass
class Skill:
    id: str
    section: Literal["context", "harness"]   # pipeline-facing split only
    keywords: List[str]                      # fine-grained category/topic labels (required, normalized)
    issue: str                                # prose problem + scope inline — required
    insight: Optional[str]                    # imperative action — required for context, optional for harness
    occurrences: List[InsightSource]          # append-only audit chain; auto-appended on every mutation
    active: bool = True
    used_count: int = 0
    helpful_count: int = 0
    harmful_count: int = 0
    neutral_count: int = 0
    embedding: Optional[List[float]] = None   # stored in sidecar .npz, not JSON
    created_at: str
    updated_at: str
```

**Dropped fields:** `content`, `justification`, `evidence`.
**Rename:** `sources` → `occurrences`.
**Section semantics:** `section` is no longer the old free-form category field. It is only the binary split `context|harness`. Fine-grained categorization now lives in `keywords`.
**Invariants enforced in `Skillbook.add_skill` / `update_skill`:**
- `section ∈ {"context", "harness"}` — reject otherwise.
- `keywords` required, non-empty, normalized by stripping / lowercasing / de-duping while preserving order.
- `issue` always required, non-empty.
- `insight` required + non-empty when `section="context"`; may be `None` or empty when `section="harness"`.
- Any mutation invalidates `embedding` (set to `None` so it recomputes on next retrieval).

---

## Storage — split embeddings

- `skillbook.json` — diffable. `Skill` entries never carry `embedding`.
- `skillbook.embeddings.npz` — `numpy.savez_compressed`, keyed by `skill_id`, float32. **Cache only.** Can be deleted and recomputed lazily.
- `save_to_file(path)` writes both.
- `load_from_file(path)` loads JSON; loads `.npz` if present (silent no-op otherwise).
- Schema version check on load: JSON must contain `"schema_version": "2"`. Missing/mismatched → `raise ValueError("Skillbook format v2 required — regenerate")`. Hard break confirmed.

---

## Tool surface — atomic, no micro-tools

Full signatures. `issue` required on every mutation; `keywords` required on add and optional on update (omit to keep current); `insight` required when `section="context"`, optional when `section="harness"`:

```python
add_skill(section, issue, keywords, insight=None) -> {ok, skill_id}   # insight required iff section="context"
update_skill(skill_id, issue, keywords=None, insight=None) -> {ok}     # omit keywords / insight to keep current values
tag_skill(skill_id, delta) -> {ok}                           # delta ∈ {-1, 0, 1}
remove_skill(skill_id, reason) -> {ok}                       # SOFT default — sets active=False, keeps history
search_skills(query, top_k=5, section=None, keywords=None) -> [...]
read_skill(skill_id) -> {id, section, keywords, issue, insight, counters, active, occurrences}
```

**`add_skill` / `update_skill` auto-append an `InsightSource`** from the current trace. Every mutation is recorded in `occurrences`. `tag_skill` auto-appends an observation entry too.

No `widen_scope` / `codify_insight` / `add_occurrence` micro-tools — all mutations go through the atomic `update_skill` to prevent partial/stale states.

---

## Provenance wiring (closes current gap)

**Problem today:** `Skill.sources` is always `[]` because SM tools never thread `insight_source=`. Grepped to confirm.

**Fix:**

1. `UpdateStep.__call__` builds an `InsightSource` from `ctx.trace` (`trace_uid`, `source_system`, `trace_id`, `sample_question`) + `ctx.epoch` + `reflections[0].error_identification` + `reflections[0].key_insight`.
2. `SkillManager.update_skills(..., source: InsightSource)` — new required kwarg.
3. `SMDeps.current_source: InsightSource` — available to every tool.
4. `add_skill` / `update_skill` / `tag_skill` tools derive per-op `InsightSource` (copying identity, setting `operation_type`, appending op-specific `error_identification` / `learning_text`), and pass `insight_source=` through to the underlying `Skillbook` method.

Result: `skill.occurrences` populates naturally. Dashboard has data.

---

## Embedding input formula

```python
parts = [issue]
if insight is not None:
    parts.append(insight)
if keywords:
    parts.append(f"Keywords: {', '.join(keywords)}")
embedding_input = "\n\n".join(parts)
```

So search / dedup consumers can match on problem text, action text, and structured category labels. Invalidate on any mutation of `issue`, `insight`, or `keywords`.

---

## Prompt rendering

`Skillbook.as_prompt()` remains a compatibility / helper surface. It should render only skills where `active=True`, grouped by section, using the new `issue` / `insight` fields:

```
## context
- [context-00007]
  Keywords: airline, booking_api, cabin_class
  Issue: In tau-airline's update_reservation_flights API, cabin class is a single param applied to all legs/passengers — no per-leg or per-passenger differentiation.
  Insight: Before offering per-passenger or per-leg upgrades, immediately tell the user that cabin class is all-or-nothing, then present only all-or-nothing options.

## harness
- [harness-00003]
  Keywords: tau2, rate_limit, retries
  Issue: tau2 runner retries Bedrock 429 with 60s exponential backoff, blocking the whole pipeline. Observed in airline + retail runs.
```

This phase does **not** decide rollout retrieval or prompt-injection policy. `as_prompt()` is kept as a generic rendering helper for debugging, exports, and backward-compatible callers.

---

## SM prompt rewrite (`ace/implementations/prompts.py`)

- Declare the two-section taxonomy and the insight-required-iff-context invariant.
- Declare the distinction between binary `section` (`context|harness`) and fine-grained `keywords`.
- Require `issue` on every ADD/UPDATE; require `insight` only when `section="context"`.
- Require non-empty `keywords` on ADD. Guide: 1-5 short stable labels such as domain, subsystem, API family, or behavior category.
- Guide: write `issue` as problem + applicability inline (start narrow — single domain / single API / single endpoint).
- **Recursive widening rule:** if `search_skills` returns a semantically overlapping issue from another domain, call `update_skill` with a broader `issue` that covers both contexts, rather than creating a new skill.
- When broadening or merging a skill, update `keywords` too: keep useful existing labels, add genuinely new ones, and drop stale labels that no longer fit.
- **Duplicate-avoidance:** always `search_skills` before `add_skill`.
- Soft-delete semantics: `remove_skill` for skills that are harmful or outdated; audit chain is preserved and `active=False` skills are excluded from normal active-skill views.

---

## Skillbook search / retrieval (tooling only)

File: `ace/implementations/skill_rendering.py`.

`retrieve_top_k(skillbook, query, *, top_k=5, section=None, keywords=None)`:
1. Optional `section` pre-filter (`skillbook._sections` already indexed).
2. Optional `keywords` filter / boost against `skill.keywords`.
3. BM25 rank over `issue + insight + keywords` text (lexical).
4. Dense cosine rank over embeddings. Query embedding failure → `raise` (already done).
5. Reciprocal Rank Fusion (k=60). Return top-k.

Add dep: `rank-bm25` (MIT, ~50 LOC wrapping). No infra.

This section applies to `search_skills` / inspection flows only. Agent-side retrieval and prompt-injection policy are explicitly deferred.

---

## Files to touch

Core (CLAUDE.md-gated — user pre-approved):
- `ace/core/skillbook.py` — `Skill` rewrite, `UpdateOperation` rewrite (drop content/justification/evidence fields, add keywords/issue/insight), `add_skill`/`update_skill`/`remove_skill` (default `soft=True`), `to_dict`/`from_dict` with `schema_version="2"` check, sidecar save/load, `as_prompt()` field rendering update, `to_llm_dict`, `_apply_operation`.
- `ace/core/insight_source.py` — no change (fields already sufficient).

Integration:
- `ace/deduplication/detector.py` — embedding input changed to `issue + insight + keywords`. Line 178 needs update (`s.content` → new formula). Invalidation hook on update (skill.embedding = None).
- `ace/deduplication/prompts.py` — referencing `skill_a.content` (lines 54, 56, 113, 114) → update to `issue` + `keywords` context.
- `ace/deduplication/operations.py` — referencing `.content` writes (lines 113, 153) → update to `.issue` / `.insight`.
- `ace/implementations/sm_tools.py` — rewrite all tool signatures (`add_skill`, `update_skill`, `tag_skill`, `remove_skill`, `search_skills`, `read_skill`). Thread `ctx.deps.current_source` into every mutation.
- `ace/implementations/skill_manager.py` — accept `source: InsightSource` on `update_skills`, store on `SMDeps.current_source`.
- `ace/implementations/prompts.py` — full SM prompt rewrite.
- `ace/implementations/rr/tools.py` — `read_skill` return dict (line 89) → new fields. `search_skillbook` return (line 122) → new fields.
- `ace/implementations/skill_rendering.py` — `render_skills_xml` (line 47) → new fields + hybrid BM25+RRF + `section` / `keywords` support.
- `ace/implementations/helpers.py` — line 59 renders `skill.content` → swap to issue/insight.
- `ace/steps/update.py` — build `InsightSource` from `ctx.trace` and pass to `SM.update_skills(source=...)`.
- `ace/steps/export_markdown.py` — lines 44, 46-47, 49-50 reference old fields → update.

Tests / examples:
- `tests/` — fixtures will break on load (different field names); update.
- `examples/` — skim for any `.content` / `.justification` / `.evidence` reads.

New dep: `rank-bm25`.

---

## Smoke test

Run after changes land:

```bash
uv run ace-eval e2e \
  --benchmark tau-bench-airline \
  --traces results/e2e/run_784b73163157/collection \
  --agent-model bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0 \
  --reflector-model bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0 \
  --skill-manager-model bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0 \
  --user-model bedrock/openai.gpt-oss-120b-1:0 \
  --reflector-type rr \
  --num-trials 1 --max-num-steps 50 --max-workers 1 \
  --no-benchmark --logfire --verbose
```

Verify:
- No crashes.
- `skillbook.json` conforms to v2 shape.
- `skillbook.embeddings.npz` created.
- Every skill has `len(occurrences) >= 1`.
- Every skill has `len(keywords) >= 1`.
- Context skills have non-null `insight`; harness skills may or may not.
- Logfire shows `sm.session` → `add_skill` / `update_skill` with atomic signatures.

---

## Out of scope

- Dashboard UI — data shape is sufficient; build later.
- Agent-side retrieval / prompt injection policy — defer to a later plan; this document does not decide whether skills are fetched by a pre-step, tool calls, or some other rollout path.
- Cross-encoder reranker — not worth it below ~2000 skills.
- Multi-vector embeddings (content + use-case split) — not needed; single concat works.
- Hierarchical taxonomy — explicitly rejected. Structured flat `keywords` are sufficient.
- Query expansion / HyDE — defer until retrieval misses observed in production.
- SQLite migration — JSON+sidecar is right for <5K skills.

---

## Open decisions

- **`UpdateOperation` audit-log fields:** drop `content/justification/evidence`, add `issue/insight`. Keep structure identical otherwise.
- **Section validation:** add a module-level constant `VALID_SECTIONS = frozenset({"context", "harness"})` and validate in `add_skill` + `update_skill` (via `section=` lookup from existing skill).
- **Keyword normalization:** store `keywords` as short lowercase identifiers; de-dupe while preserving order.
- **`_generate_id` prefix:** stays as `section.split()[0].lower()` → yields `context-00001` / `harness-00001` naturally.
- **Hard purge:** expose `Skillbook.purge(skill_id)` as a module-level method NOT wired to any SM tool. Human-operator / CLI only.

---

## Already done in this branch

- [x] `ace/implementations/skill_rendering.py:96-101` — `retrieve_top_k` raises on embedding failure (no silent fallback).
- [x] `ace/core/recursive_agent.py` — `span_label` threaded through `run_agent_with_compaction` and `RecursiveAgent`; SkillManager emits `sm.session` spans distinct from RR's `rr.session`.
- [x] `ace/implementations/rr/config.py` — `cache_prompts` / `cache_ttl` added to `RecursiveConfig` (was previously an `AttributeError`).
- [x] `ace-eval/src/ace_eval/e2e/training.py` — `_train_sequential` surfaces `SampleResult.error` instead of silently swallowing.
- [x] `ace/implementations/skill_manager.py` — passes `span_label="sm"` to superclass.
