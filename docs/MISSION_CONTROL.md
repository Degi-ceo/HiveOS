# Mission Control — Operator Manual

## Agents Kanban (SPRINT_6 P-G)

The **Agents** tab in Mission Control renders a live Kanban board with five
fixed columns — one per named sub-agent: `researcher`, `coder`, `reviewer`,
`memory-keeper`, `security-reviewer`.

### What you see

Each column is a vertical queue of cards. Cards represent A2A sub-agent
calls. Card fields:

| Field | Source |
|---|---|
| Status badge | `queued` / `running` / `done` / `failed` |
| Elapsed time | `now − started_at` (live, updates every 1s) |
| Task description | the delegated task string |
| Tool-call count | v1 leaves at 0 (orchestration-internal count, future work) |

### Drill-down to trace

Click a card whose `session_id` is set to open `/traces/{session_id}` in a
new tab. The parent orchestrator session must have been started with
`session_id=...` passed to `delegate_via_envelope` for the link to resolve.

### Event flow

```
delegate_to_specialist(name, task, session_id=...)
   └─→ delegate_via_envelope
         ├─→ a2a.call.started   (board.snapshot adds card)
         ├─→ AgentExecutor → AgentResult
         ├─→ a2a.call.completed | a2a.call.failed
         └─→ board.snapshot marks card done/failed
GET /agents/board          ← REST snapshot (refresh button or polling)
WS /ws/dashboard           ← live delta frames {type:"a2a.call.*"}
```

### Card pruning

Cards older than 1 hour (the BoardStore TTL) are removed from the snapshot.
For longer retention, change `BoardStore.ttl_seconds` and rebuild.

### Why no Playwright smoke yet

This repo does not yet declare `playwright` as a dependency. Visual smoke is
captured manually (Hive runs `hive serve` + opens `/app`) until that
dependency is added. The REST + WS smoke in `scripts/smokes/kanban.py`
covers the data path 100%.