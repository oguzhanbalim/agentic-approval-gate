from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from approval_gate import Evidence, ExecutionContext, Gate, Registry


class FakeClock:
    """Starts at the real time, so gates in other processes agree until a test advances it."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: int) -> None:
        self.now += timedelta(**kwargs)


@dataclass(frozen=True)
class Refund:
    order_id: str
    amount_cents: int
    currency: Literal["USD", "EUR"] = "USD"

    def __post_init__(self) -> None:
        if self.amount_cents <= 0:
            raise ValueError("amount_cents must be positive")


class Harness:
    """A gate with a counting executor and a fake clock."""

    def __init__(self, path) -> None:
        self.path = path
        self.calls: list[tuple[Refund, ExecutionContext]] = []
        self.fail_with: Exception | None = None
        self.clock = FakeClock()
        self.registry = Registry()

        @self.registry.action("refund", Refund)
        def run(payload: Refund, context: ExecutionContext) -> dict:
            self.calls.append((payload, context))
            if self.fail_with:
                raise self.fail_with
            return {"refunded": payload.amount_cents}

        self.gate = Gate(path, self.registry, clock=self.clock)
        self.agent = self.gate.agent("bot")

    def propose(self, **overrides):
        args = {
            "payload": {"order_id": "ORD-1", "amount_cents": 100},
            "evidence": [Evidence("ticket://1", note="synthetic")],
            "reason": "synthetic test",
        }
        args.update(overrides)
        return self.agent.propose("refund", args.pop("payload"), **args)

    def approved(self, **overrides):
        proposal = self.propose(**overrides)
        return self.gate.approve(proposal.id, "reviewer")

    def raw(self) -> sqlite3.Connection:
        """A separate connection with the append-only triggers removed: the attacker's view."""
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute("DROP TRIGGER IF EXISTS events_no_update")
        conn.execute("DROP TRIGGER IF EXISTS events_no_delete")
        return conn


@pytest.fixture
def h(tmp_path) -> Harness:
    harness = Harness(tmp_path / "gate.db")
    yield harness
    harness.gate.close()
