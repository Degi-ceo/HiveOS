# ADR 001 — SQLite-first state storage

**Status:** Accepted  
**Date:** 2026-06-13  
**Deciders:** Kamil (owner), Hive (architect)

---

## Context

HiveOS needs durable state for six distinct stores: conversation history, episodic/knowledge memory, skill usage tracking, the task board, cron schedules, and the audit log. The system runs on a single Hetzner VPS with one active user (Kamil) and one autonomous agent (Hive), with occasional burst autonomy (heartbeat dispatching ≤`HIVE_MAX_AGENTS` concurrent subagents).

The options considered were:

| Option | Notes |
|---|---|
| SQLite (WAL mode) | Zero-config, file-per-store, survives restart, Python stdlib |
| PostgreSQL | Full SQL, network-capable, requires a server process |
| Redis | Fast in-memory, optional persistence, separate server process |
| Redis Streams | Good for event queues, but adds a new technology boundary |
| JSON sidecars | Simple, but no transactionality, no FTS, messy concurrent writes |

---

## Decision

**Use SQLite in WAL mode as the exclusive durable state store.** Each subsystem that needs persistence owns its own table(s) in a shared `state.db` file (path from `HiveConfig.state_db`), except the audit log (which gets its own `audit.sqlite` to allow independent rotation).

The shared `state.db` contains: `sessions`+`messages`+`messages_fts`, `episodic`+`knowledge`+`knowledge_fts`, `skill_usage`, `hive_tasks`, `hive_cron`, `hive_commitments`.

Each store self-initializes its DDL via `CREATE TABLE IF NOT EXISTS` + `CREATE VIRTUAL TABLE IF NOT EXISTS` on first use. `core/doctor.py` verifies the DB is openable but does not duplicate DDL (avoids the schema-drift bug fixed in PR #14).

When Mnemosyne is installed it manages its own schema in `MNEMOSYNE_HOME` — the shared `state.db` continues to hold the fallback `LocalMemoryProvider` tables, which become dormant.

---

## Consequences

**Good:**
- Zero deployment footprint: no server processes, no ports, no credentials for the DB itself.
- WAL mode allows concurrent readers + one writer, sufficient for Hive's single-writer model.
- FTS5 gives full-text search on messages and knowledge without an external search index.
- SQLite files are trivially backed up (`cp state.db backup/`) and inspected (`sqlite3`).
- `sqlite3` is Python stdlib — no mandatory dependency.
- `TaskBoard.recent_failures()`, `CommitmentBook`, `CronScheduler`, and `SessionStore` all benefit from SQL queries without custom serialization.

**Bad / trade-offs:**
- Single-writer: concurrent writes from the heartbeat + gateway are serialized. Acceptable at current scale; would require sharding or Postgres at multi-user scale.
- No network replication: the DB is local to the VPS. Acceptable (backup via `rsync`; Mnemosyne has its own remote-memory story via `MNEMOSYNE_MCP_URL`).
- SQLite cannot do `LISTEN`/`NOTIFY`: the EventBus uses in-process callbacks instead, which is fine for the single-process model.

---

## Alternatives considered

**PostgreSQL:** Adds a server process, credentials, and connection pooling. Justified at multi-user scale but pure overhead for a single-user personal agent.

**Redis:** Great for ephemeral task queues, but `TaskBoard` needs SQL queries (filtering by state, ordering by timestamp, joins). Persistence config is non-trivial. Two storage technologies for one system.

**JSON sidecars:** Used pre-P0. Caused concurrent-write corruption and had no query capability. Removed in PR #2.

---

## See also

- [`autonomy/tasks.py`](../../src/hive/autonomy/tasks.py) — `TaskBoard` (SQLite-backed)
- [`core/doctor.py`](../../src/hive/core/doctor.py) — DB health checks
- [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md#5-data-model-sqlite-first-no-json-sidecars-for-runtime-state) — data model section
