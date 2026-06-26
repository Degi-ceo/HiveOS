"""
learning — SPRINT_6 P-F self-improvement loop (trace → evolve → eval).

Submodules:
- ``storage``   — SQLite helpers for ``learning_traces`` + ``learning_loops``
- ``tracer``    — observes tool-call outcomes into ``learning_traces``
- ``evolver``   — wraps ``self_mod.propose`` (added in P-F step 2)
- ``evaluator`` — runs evals + pytest on candidate worktree (added in P-F step 2)
- ``loop``      — orchestrator that wires everything (added in P-F step 3)

Public entry points live on ``HiveOS.learning_loop()`` and are gated by
``config.learning_loop_enabled`` (default off).
"""
from __future__ import annotations

from hive.core.learning.evaluator import (  # noqa: F401
    EvalScore,
    Evaluator,
    Verdict,
)
from hive.core.learning.evolver import (  # noqa: F401
    Evolver,
    Proposal,
)
from hive.core.learning.loop import LearningLoop, LoopConfig  # noqa: F401
from hive.core.learning.storage import (  # noqa: F401
    count_by_verdict,
    ensure_schema,
    insert_loop,
    insert_trace,
    query_loops,
    query_traces,
)
from hive.core.learning.tracer import (  # noqa: F401
    OUTCOME_DENIED,
    OUTCOME_ERROR,
    OUTCOME_OK,
    Tracer,
)

__all__ = [
    "Tracer",
    "OUTCOME_OK",
    "OUTCOME_ERROR",
    "OUTCOME_DENIED",
    "Evolver",
    "Proposal",
    "Evaluator",
    "EvalScore",
    "Verdict",
    "LearningLoop",
    "LoopConfig",
    "ensure_schema",
    "insert_trace",
    "insert_loop",
    "query_traces",
    "query_loops",
    "count_by_verdict",
]
