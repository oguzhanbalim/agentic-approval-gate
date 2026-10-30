"""An approved action runs at most once: in sequence, under races, and across crashes."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from approval_gate import Gate, NotAllowed, Registry
from conftest import Harness, Refund


def test_second_execute_is_refused(h):
    proposal = h.approved()
    h.gate.execute(proposal.id)
    with pytest.raises(NotAllowed, match="executed, not approved"):
        h.gate.execute(proposal.id)
    assert len(h.calls) == 1


def test_execute_approved_runs_each_once_across_repeated_sweeps(h):
    h.approved(idempotency_key="a")
    h.approved(idempotency_key="b")
    assert len(h.gate.execute_approved()) == 2
    assert h.gate.execute_approved() == []
    assert len(h.calls) == 2


def test_threads_racing_on_one_proposal(tmp_path):
    db = tmp_path / "gate.db"
    harness = Harness(db)
    proposal = harness.approved()
    harness.gate.close()

    calls: list[str] = []
    outcomes: list[str] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        registry = Registry()

        @registry.action("refund", Refund)
        def run(payload, context):
            calls.append(context.proposal_id)
            return {}

        gate = Gate(db, registry)
        barrier.wait()
        try:
            outcomes.append(gate.execute(proposal.id).state)
        except NotAllowed:
            outcomes.append("refused")
        finally:
            gate.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == [proposal.id]
    assert sorted(outcomes) == ["executed"] + ["refused"] * 7


WORKER = textwrap.dedent(
    """
    import sys
    from dataclasses import dataclass
    from approval_gate import Gate, NotAllowed, Registry

    @dataclass(frozen=True)
    class Refund:
        order_id: str
        amount_cents: int
        currency: str = "USD"

    registry = Registry()

    @registry.action("refund", Refund)
    def run(payload, context):
        with open(sys.argv[3], "a") as handle:
            handle.write("ran\\n")
        return {}

    gate = Gate(sys.argv[1], registry)
    try:
        gate.execute(sys.argv[2])
    except NotAllowed:
        pass
    """
)


def test_processes_racing_on_one_proposal(tmp_path):
    db = tmp_path / "gate.db"
    ledger = tmp_path / "ledger.txt"
    harness = Harness(db)
    proposal = harness.approved()
    harness.gate.close()

    workers = [
        subprocess.Popen([sys.executable, "-c", WORKER, str(db), proposal.id, str(ledger)])
        for _ in range(6)
    ]
    assert [worker.wait(timeout=60) for worker in workers] == [0] * 6
    assert ledger.read_text() == "ran\n"


def test_unique_index_refuses_a_second_claim_even_if_the_code_is_bypassed(h):
    proposal = h.approved()
    with h.gate.log.transaction():
        h.gate.log.append(proposal.id, "execution_started", "gate", "t", {})
    with pytest.raises(sqlite3.IntegrityError), h.gate.log.transaction():
        h.gate.log.append(proposal.id, "execution_started", "gate", "t", {})


CRASHER = textwrap.dedent(
    """
    import os, sys
    from dataclasses import dataclass
    from approval_gate import Gate, Registry

    @dataclass(frozen=True)
    class Refund:
        order_id: str
        amount_cents: int
        currency: str = "USD"

    registry = Registry()

    @registry.action("refund", Refund)
    def run(payload, context):
        with open(sys.argv[3], "a") as handle:
            handle.write("ran\\n")
        os._exit(9)  # The side effect happened; the outcome was never recorded.

    Gate(sys.argv[1], registry).execute(sys.argv[2])
    """
)


@pytest.fixture
def crashed(tmp_path) -> tuple[Harness, str, Path]:
    db = tmp_path / "gate.db"
    ledger = tmp_path / "ledger.txt"
    harness = Harness(db)
    proposal = harness.approved()
    done = subprocess.run([sys.executable, "-c", CRASHER, str(db), proposal.id, str(ledger)])
    assert done.returncode == 9
    yield harness, proposal.id, ledger
    harness.gate.close()


def test_crash_mid_execution_leaves_it_in_doubt_and_never_reruns(crashed):
    harness, proposal_id, ledger = crashed
    assert harness.gate.get(proposal_id).state == "in_doubt"
    with pytest.raises(NotAllowed, match="in_doubt"):
        harness.gate.execute(proposal_id)
    assert harness.gate.execute_approved() == []
    assert harness.calls == []
    assert ledger.read_text() == "ran\n"
    assert harness.gate.verify().ok


def test_person_resolves_an_in_doubt_execution_without_rerunning(crashed):
    harness, proposal_id, ledger = crashed
    resolved = harness.gate.resolve(
        proposal_id, "reviewer", succeeded=True, note="found the refund in the ledger"
    )
    assert resolved.state == "executed"
    assert harness.calls == []
    assert ledger.read_text() == "ran\n"
    with pytest.raises(NotAllowed):
        harness.gate.resolve(proposal_id, "reviewer", succeeded=False, note="again")


def test_resolve_can_record_failure_and_needs_a_note(crashed):
    harness, proposal_id, _ = crashed
    with pytest.raises(NotAllowed, match="note"):
        harness.gate.resolve(proposal_id, "reviewer", succeeded=False, note=" ")
    assert (
        harness.gate.resolve(proposal_id, "r", succeeded=False, note="not found").state == "failed"
    )


def test_resolve_refuses_anything_not_in_doubt(h):
    with pytest.raises(NotAllowed, match="not in_doubt"):
        h.gate.resolve(h.approved().id, "reviewer", succeeded=True, note="n")
