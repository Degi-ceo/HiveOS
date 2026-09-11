"""Isolated, evidence-producing HiveOS targets for the evaluation harness."""
from __future__ import annotations

import gc
import json
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from hive.core.config import HiveConfig
from hive.core.events import EventType
from hive.core.types import Message, Role, ToolCall
from hive.evals.types import EvalItem, TargetOutput
from hive.llm.adapters.base import CompletionResult
from hive.llm.router import TaskKind
from hive.runtime import HiveOS


class DeterministicEvalRouter:
    """Deterministic model boundary used to exercise the real runtime in CI.

    It reacts only to conversation input and tool results. It never receives
    the ``EvalItem`` and therefore cannot copy ``expected`` into its answer.
    """

    async def complete(
        self,
        messages: list[Message],
        kind: Any = None,
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> CompletionResult:
        del kind, tools, kwargs
        latest = messages[-1]
        if system and system.startswith("You are an evaluation judge"):
            marker = "UNTRUSTED_EVALUATION_DATA="
            try:
                payload = json.loads(latest.content.split(marker, 1)[1])
                rubric = str(payload["rubric"]).lower()
                candidate = str(payload["candidate_answer"]).lower()
            except (IndexError, KeyError, TypeError, json.JSONDecodeError):
                return CompletionResult(
                    text='{"score":0,"reason":"malformed deterministic judge input"}',
                    model="eval-fixture",
                )
            rubric_terms = {
                "self-reference": ("recursion", "itself", "self-reference"),
                "databases": ("database", "query", "data"),
                "version control": ("version control", "repository", "changes"),
                "containers": ("container", "isolated"),
                "orchestration": ("orchestrat", "container"),
                "interface": ("interface", "endpoint"),
                "learning from data": ("learn", "data"),
                "protocol": ("protocol", "hypertext"),
                "cipher": ("cipher", "encrypt", "encoding"),
                "lookup performance": ("lookup", "performance", "data structure"),
            }
            required = next(
                (terms for label, terms in rubric_terms.items() if label in rubric),
                (),
            )
            passed = bool(required) and any(term in candidate for term in required)
            return CompletionResult(
                text=json.dumps({
                    "score": 1.0 if passed else 0.0,
                    "reason": "deterministic rubric fixture matched" if passed
                    else "deterministic rubric fixture did not match",
                }),
                model="eval-fixture",
            )
        if latest.role is Role.TOOL:
            return CompletionResult(text="Hive status checked.", model="eval-fixture")

        prompt = latest.content.strip()
        responses = {
            "Return exactly the word HIVE.": "HIVE",
            "What is 2 + 2? Reply with only the number.": "4",
            "ping": "pong 🐝",
            "echo hello": "hello",
            "what is 1+1": "2",
            "what is 2+2": "4",
            "what is 10-3": "7",
            "what is 5*6": "30",
            "what is 100/4": "25",
            "uppercase hello": "HELLO",
            "lowercase WORLD": "world",
            "reverse abc": "cba",
            "what is pi to 2 decimals": "3.14",
            "list primes under 20": "2, 3, 5, 7, 11, 13, 17",
            "color of the sky": "blue",
            "capital of france": "Paris",
            "hex value of ff": "255",
            "year world war 2 ended": "1945",
            "boiling point of water in celsius": "100",
            "is python interpreted": "Yes, Python is interpreted.",
            "what does json stand for": "JavaScript Object Notation",
            "speed of light in scientific notation": "3.00 × 10^8 m/s",
            "explain recursion in one sentence": (
                "Recursion is a process in which a function calls itself until a base case."
            ),
            "what is sql used for": "SQL is used to query and manage data in databases.",
            "what is git": "Git is a distributed version control system for tracking changes.",
            "what is docker in one sentence": (
                "Docker runs applications in portable, isolated containers."
            ),
            "what is kubernetes": "Kubernetes orchestrates container deployment and scaling.",
            "what is an api": (
                "An API is an interface exposing endpoints for software communication."
            ),
            "what is machine learning": (
                "Machine learning lets systems learn patterns from data."
            ),
            "what is http": "HTTP is the Hypertext Transfer Protocol used on the web.",
            "what is encryption": (
                "Encryption uses a cipher to encode data so only authorized parties can read it."
            ),
            "what is a database index": (
                "A database index is a data structure that improves lookup performance."
            ),
        }
        if prompt in responses:
            return CompletionResult(text=responses[prompt], model="eval-fixture")
        if prompt == "Use Hive's status tool, then confirm that status was checked.":
            return CompletionResult(
                text="",
                model="eval-fixture",
                tool_calls=[ToolCall(id="eval-status", name="hive_status", arguments="{}")],
            )
        return CompletionResult(
            text=f"Unsupported deterministic eval input: {prompt}",
            model="eval-fixture",
        )

    async def aclose(self) -> None:
        return None


class HiveRuntimeTarget:
    """Build one isolated HiveOS and return ledger-backed evidence per case."""

    def __init__(self, *, deterministic: bool = False) -> None:
        self._deterministic = deterministic
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None
        self._hive: HiveOS | None = None

    def _build(self) -> HiveOS:
        self._tempdir = tempfile.TemporaryDirectory(prefix="hive-eval-")
        root = Path(self._tempdir.name)
        config = HiveConfig.from_env(root=root, load_dotenv=False)
        if self._deterministic:
            config = replace(
                config,
                root=root,
                data_dir=root / "data",
                state_db=root / "data" / "hive.sqlite",
                mnemosyne_home=root / "data" / "mnemosyne",
                obsidian_vault=root / "vault",
                production_mode=False,
                autonomy_enabled=False,
                autonomous_selfmod_enabled=False,
                learning_loop_enabled=False,
                planner_enabled=False,
                telegram_token="",
                slack_signing_secret="",
                discord_public_key="",
                smtp_webhook_secret="",
            )
        router = DeterministicEvalRouter() if self._deterministic else None
        return HiveOS.build(
            config,
            router=router,
            validate_inbound_channels=not self._deterministic,
        )

    async def __call__(self, item: EvalItem) -> TargetOutput:
        if self._hive is None:
            self._hive = self._build()
        hive = self._hive
        session_id = f"eval-{item.id}-{uuid.uuid4().hex}"
        text = await hive.ask(item.input, session_id=session_id, channel_hint="eval")
        runs = hive.run_ledger.recent(limit=1, session_id=session_id)
        if not runs:
            raise RuntimeError("HiveOS completed without a correlated run ledger record")
        run = runs[0]
        run_id = str(run["run_id"])
        events = hive.run_ledger.events(run_id)
        trace = tuple(
            str(event["data"].get("tool"))
            for event in events
            if event["type"] == EventType.TOOL_CALL_END.value
            and event["data"].get("tool")
        )
        turn_ends = [
            event for event in events
            if event["type"] == EventType.AGENT_TURN_END.value
        ]
        terminal_outcome = (
            str(turn_ends[-1]["data"].get("outcome") or run["state"])
            if turn_ends else str(run["state"])
        )
        return TargetOutput(
            text=text,
            tool_trace=trace,
            run_id=run_id,
            terminal_outcome=terminal_outcome,
        )

    async def judge(self, prompt: str) -> str:
        """Evaluate one bounded grader prompt through this target's router."""
        if self._hive is None:
            self._hive = self._build()
        result = await self._hive.router.complete(
            [Message(role=Role.USER, content=prompt)],
            kind=TaskKind.AUX,
            system=(
                "You are an evaluation judge. Follow the requested JSON schema "
                "exactly and do not call tools."
            ),
            thinking=False,
            max_tokens=512,
            temperature=0.0,
        )
        return result.text

    async def aclose(self) -> None:
        if self._hive is not None:
            await self._hive.aclose()
            self._hive = None
        if self._tempdir is not None:
            last_error: PermissionError | None = None
            for _attempt in range(5):
                try:
                    self._tempdir.cleanup()
                    last_error = None
                    break
                except PermissionError as exc:
                    # Windows can release the final SQLite/WAL handle one
                    # scheduler tick after close(). Retry briefly, but still
                    # surface a persistent leak instead of ignoring it.
                    last_error = exc
                    gc.collect()
                    time.sleep(0.05)
            if last_error is not None:
                raise last_error
            self._tempdir = None


def make_live_target() -> HiveRuntimeTarget:
    return HiveRuntimeTarget(deterministic=False)


def make_deterministic_target() -> HiveRuntimeTarget:
    return HiveRuntimeTarget(deterministic=True)
