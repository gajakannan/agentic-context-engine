# Update Operations

The SkillManager communicates changes to the skillbook through **update operations**. Each operation is a structured instruction to modify the skillbook in a specific way.

## Operation Types

| Type | Description | Required Fields |
|------|-------------|----------------|
| `ADD` | Create a new skill | `section`, `issue`, `keywords` (`insight` required for `context`) |
| `UPDATE` | Modify an existing skill | `skill_id`, `issue` |
| `TAG` | Record whether a skill helped, harmed, or was neutral | `skill_id`, `metadata.delta` |
| `REMOVE` | Soft-remove a skill from the active skillbook | `skill_id` |

## Examples

### ADD

Adds a new strategy learned from experience:

```json
{
  "type": "ADD",
  "section": "context",
  "keywords": ["math", "decomposition"],
  "issue": "Complex arithmetic questions are easier to solve when the work is decomposed into smaller verified steps.",
  "insight": "Break complex problems into smaller steps before computing."
}
```

### UPDATE

Refines an existing strategy:

```json
{
  "type": "UPDATE",
  "skill_id": "context-00001",
  "section": "context",
  "keywords": ["math", "verification"],
  "issue": "Complex arithmetic questions are easier to solve when the work is decomposed into smaller verified steps.",
  "insight": "Break complex problems into smaller steps and verify each step before proceeding."
}
```

### TAG

Records whether a skill helped, harmed, or had no clear effect:

```json
{
  "type": "TAG",
  "section": "context",
  "skill_id": "context-00001",
  "metadata": {"delta": 1}
}
```

### REMOVE

Prunes a strategy that is consistently harmful:

```json
{
  "type": "REMOVE",
  "section": "context",
  "skill_id": "context-00003",
  "reason": "The guidance is stale and now causes wrong tool choices."
}
```

## Update Batches

The SkillManager emits operations as an `UpdateBatch` — one or more operations applied atomically:

```python
from ace import UpdateOperation, UpdateBatch

batch = UpdateBatch(operations=[
    UpdateOperation(
        type="ADD",
        section="context",
        keywords=["debugging", "logging"],
        issue="Input shape mismatches are hard to diagnose without request-level visibility.",
        insight="Log the incoming payload before the failing transformation.",
    ),
    UpdateOperation(type="REMOVE", section="context", skill_id="context-00003"),
])

skillbook.apply_update(batch)
```

In batch reflection mode, `ADD` and `UPDATE` operations may also include
`reflection_index` to indicate which reflection in the input tuple primarily
produced the operation.

When an operation is synthesized from multiple reflections, it may instead use
`reflection_indices` to list all contributing reflections. This lets downstream
provenance attach multiple trace sources to one learned skill.

## Skill Tagging

Skill effectiveness is recorded through `TAG` operations. The SkillManager decides
whether each injected skill helped, harmed, or had no material effect, and encodes
that as `metadata.delta`:

- `1` → increment `helpful_count`
- `-1` → increment `harmful_count`
- `0` → increment `neutral_count`

## How Updates Flow

```
Agent cites or injects skill_ids --> Reflector analyzes outcome --> SkillManager emits ADD/UPDATE/TAG/REMOVE
```

1. The **Agent** cites skill IDs it used in its reasoning
2. The **Reflector** produces the analysis that the SkillManager learns from
3. The **SkillManager** uses that analysis to ADD, UPDATE, TAG, or REMOVE skills

## What to Read Next

- [The Skillbook](skillbook.md) — where operations are applied
- [Three Roles](roles.md) — which role emits which operations
