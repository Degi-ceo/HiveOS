# ADR 004 — `core` is the leaf layer (import DAG enforcement)

**Status:** Accepted  
**Date:** 2026-06-13  
**Deciders:** Kamil (owner), Hive (architect)

---

## Context

HiveOS has ten distinct layers: `core`, `llm`, `memory`, `context`, `tools`, `agents`, `gateway`, `autonomy`, `surfaces`, `observability`. In any sufficiently large Python codebase, import cycles are a persistent maintenance hazard: a convenience import in a "low-level" module creates a hidden dependency on a "high-level" one, which causes circular import errors, increases coupling, and makes testing harder (mocking requires the entire import graph).

The specific risk in HiveOS: `core` holds the approval gate, the self-modifier, the budgeter, and the spec_search loop. If `core` could import `llm`, a bug in the LLM adapter would break the approval gate. If `core` imported `agents`, a circular import would make standalone `core` tests impossible.

The architecture defines a directed acyclic graph (DAG):

```
core (leaf — imports nothing from HiveOS)
  ↑
llm / memory / context / tools / observability
  ↑
agents
  ↑
gateway / autonomy / surfaces
  ↑
runtime.py (composition root — imports everything)
```

The question is how to enforce this DAG so violations are caught before they reach production.

---

## Decision

**Enforce the import DAG with a two-level automated test in `tests/test_architecture.py`:**

1. **Runtime probe (subprocess):** A child process imports `hive.core.*` in isolation. If any higher-layer module is imported as a side effect, the probe fails.

2. **Static AST scan:** Every `.py` file in `src/hive/core/` is parsed with the `ast` module. Any `import` or `from ... import` statement that references a higher HiveOS layer (`hive.llm`, `hive.agents`, etc.) — even inside a function body or `if TYPE_CHECKING:` block — causes a test failure.

The static scan is stricter than the runtime probe: it catches function-local imports that wouldn't trigger at import time. This caught a real `core→llm` leak during development (a `from hive.llm.pricing import ...` inside a method body).

The consequence for cross-layer needs: **dependency injection, not imports.** If `core/spec_search.py` needs to call the LLM, it accepts a `router: Callable` argument. If `memory/keeper.py` needs to summarize, it accepts a `Summarizer` callable. The composition root (`runtime.py`) wires everything together — it imports every layer, constructs every object, and injects every dependency.

---

## Consequences

**Good:**
- `core` is testable in complete isolation: `pytest tests/test_core*.py` works with no mock adapter, no gateway, no memory backend.
- The composition root is the only file with a full import graph. Everything else has a bounded dependency set.
- Adding a new module to `core` comes with a hard constraint: no HiveOS imports allowed. This forces the author to think about injection points rather than reaching for a convenience import.
- The AST scan runs in < 1 second and catches violations instantly in CI.

**Bad / trade-offs:**
- Injection adds verbosity: constructors accumulate callable parameters. `SelfImprovement(modifier, gate, pending_store)` is longer than `from hive.core.approval import gate`.
- New contributors must understand the DAG before writing code in `core`. `CLAUDE.md` and `DEVELOPMENT.md` document this, and the test failure message names the violating import.
- The scan treats all `hive.*` imports equally — a future split of the package (e.g. `hive_core` as a separate installable) would need the scan updated.

---

## What it prevents

| Violation | Caught by |
|---|---|
| `core/spec_search.py` imports `hive.llm.router` at module level | Runtime probe |
| `core/approval.py` has `from hive.agents import ...` in a function | Static AST scan |
| `core/events.py` imports `hive.observability` for convenience | Both |
| A new `core/` module added without the author noticing the rule | Static AST scan (CI failure) |

---

## Alternatives considered

**Convention only (no automated check):** Relies on code review. Historically insufficient — the `core→llm` leak was found by the automated scan, not review.

**Runtime circular import detection:** `importlib` hooks can detect cycles, but not layering violations that aren't circular (e.g. `core→llm` doesn't cause a cycle if `llm` doesn't import `core`).

**Separate Python package per layer:** Maximum isolation, but enormous operational overhead for a single-developer project. The test-enforced DAG achieves the same correctness guarantee at zero deployment cost.

---

## See also

- [`tests/test_architecture.py`](../../tests/test_architecture.py) — the enforcing test
- [`runtime.py`](../../src/hive/runtime.py) — the composition root (imports all layers)
- [`docs/DEVELOPMENT.md`](../DEVELOPMENT.md) — architecture rules for contributors
- [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md#3-dependency-dag-enforced) — DAG section
