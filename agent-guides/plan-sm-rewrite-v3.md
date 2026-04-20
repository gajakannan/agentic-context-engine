# ACE SkillManager Rewrite — Plan v3

Semi-temporary planning doc. Delete once PR 3 lands and is merged.

## PR 1 — Substrate

**`ace/core/context.py:95`** — add field to `ACEStepContext`:
- `injected_skill_ids: tuple[str, ...] = ()`

**`ace/core/skillbook.py:226`** — add counters to `Skill`:
- `used_count: int = 0`
- `helpful_count: int = 0`
- `harmful_count: int = 0`
- `neutral_count: int = 0`

**`ace/core/skillbook.py`** — add `Skillbook.tag_skill(skill_id, delta: Literal[+1, -1, 0])` method. Keep the `TAG` branch in `_apply_operation` as its implementation so serialized `UpdateOperation(TAG)` still works.

**`ace/core/skillbook.py:535`** — `as_prompt()` stays unchanged (no counter rendering).

**Agent step (`ace/implementations/agent.py:96`)** — after rendering the skillbook into the prompt, write `injected_skill_ids` onto the context and bump `used_count` on each. This is the only upstream counter touched.

**Remove citation plumbing:**
- `ace/core/outputs.py:52` — delete `SkillTag`.
- `ace/core/outputs.py:79` — delete `ReflectorOutput.skill_tags`.
- `ace/implementations/skill_manager.py:109-112` — remove the `skill_tags` consumer block.

**Docs:**
- `docs/design/ACE_ARCHITECTURE.md` — update Skill section (counters, injection-based attribution).
- `docs/design/ACE_DECISIONS.md` — add "Injection is ground truth; citation dropped."

## PR 2 — Reflector & RR prompts

**ReflectorOutput stays pure analysis** — `reasoning`, `error_identification`, `root_cause_analysis`, `correct_approach`, `key_insight`. No tagging fields, no `harmful_ids`/`helpful_ids`.

**Reflector prompt (`ace/implementations/prompts.py:419-432`)** — delete `REFLECTOR_SKILL_EVAL_SECTION` entirely.

**RR prompt (`ace/implementations/rr/prompts.py:115-136`)** — delete the `re.findall` citation recipe. No replacement. RR may still be told "inspect the skillbook (covered / contradicted / gap) and narrate what you find" as analysis guidance — never as a decision.

**Two read-only RR tools** (so RR can enrich its narrative without citations):
- `search_skillbook(query, top_k)` → wraps `retrieve_top_k`.
- `read_skill(id)` → `Skillbook.get_skill`. Return value includes counters.

**`ace/steps/rr_step.py`:**
- `_build_traces_dict` (line 267) — drop `skill_ids`; add `injected_skill_ids` from context.
- Line 399 — remove the `skill_tags=[]` remnant in the timeout fallback.

**RR batch input preserved** at the caller level — contract unchanged.

## PR 3 — Rewrite SkillManager (same class)

Rewrite `SkillManager` on top of `RecursiveAgent`. Same class name, same `SkillManagerLike` protocol, same `UpdateStep` wrapper — nothing upstream changes.

**Mutation tools** (operate on the real `Skillbook`, no staging):
- `add_skill(section, content, justification, evidence)` → `Skillbook.add_skill`.
- `update_skill(skill_id, content?, justification?, evidence?)` → `Skillbook.update_skill`.
- `remove_skill(skill_id, reason)` → `Skillbook.remove_skill`.
- `tag_skill(skill_id, delta: +1 | -1 | 0)` → `Skillbook.tag_skill`.

**Read-only tools:**
- `search_skills(query, top_k)` → returns skills with counters.
- `read_skill(id)` → returns skill with counters.

**Sandbox:**
- `sandbox_eval(code)` — reuse `register_execute_code`; opt-in per runner.

**Termination:**
- `finalize(reasoning)` → terminates the loop; returns `SkillManagerOutput` as an audit log of what was done, not a plan to be applied.

**Delete `ApplyStep`** (`ace/steps/apply.py:10`) and remove it from the ACE pipeline composition. `UpdateStep` is the sole SM invocation and the skillbook is already mutated when it returns. `UpdateStep.max_workers=1` stays.

**`SkillManagerOutput` shape** — `reasoning: str` + `operations: list[UpdateOperation]`. Operations become a post-hoc audit trail. Same dataclass, same serialization; semantic shift only.

**`UpdateBatch` / `apply_update` / `_apply_operation`** remain for offline reconstruction and tests, but the online path no longer flows through them.

**SM prompt (`ace/implementations/prompts.py:439+`):**
- Drop `<skill_effectiveness>` section (lines 505-513).
- Instruct the SM that it decides helpful/harmful/neutral from `injected_skill_ids` + outcome + reflection.
- Instruct the SM to REMOVE skills whose `harmful_count ≥ N` when it encounters them during investigation. Counters are surfaced via `read_skill` / `search_skills`, not the rendered skillbook.

**`AgenticConfig(max_requests=…)`** — `max_requests=1` degrades to a single-tool-call pass.

**Docs:**
- `docs/design/ACE_ARCHITECTURE.md` — SM section (tools, direct mutation, no ApplyStep).
- `docs/design/ACE_REFERENCE.md` — tool surface.
- `docs/design/ACE_DECISIONS.md` — "SM mutates directly; Reflector is analysis-only."

## Out of scope
- `RetrieveStep` / production top-k injection.
- Sandbox-gated commit gating (Voyager-style verify-before-ADD).
- Counter decay / windowing.
- Global "sweep all skills with harmful_count ≥ N" tool.

## Ship order
PR 1 → PR 2 → PR 3.
