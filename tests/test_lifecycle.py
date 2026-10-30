from __future__ import annotations

import pytest

from approval_gate import (
    Evidence,
    GateError,
    IdempotencyConflict,
    NotAllowed,
    Registry,
    SchemaError,
)
from conftest import Refund


def kinds(gate, proposal_id):
    return [event.kind for event in gate.log.events(proposal_id)]


def test_happy_path(h):
    proposal = h.propose()
    assert proposal.state == "pending"
    assert h.calls == []

    approved = h.gate.approve(proposal.id, "reviewer", "looks right")
    assert approved.state == "approved"
    assert approved.decided_by == "reviewer"
    assert h.calls == []  # Approval alone runs nothing.

    done = h.gate.execute(proposal.id)
    assert done.state == "executed"
    assert done.result == {"refunded": 100}
    assert kinds(h.gate, proposal.id) == [
        "proposed",
        "approved",
        "execution_started",
        "executed",
    ]


def test_executor_gets_the_typed_payload_and_stable_key(h):
    proposal = h.approved(idempotency_key="my-key")
    h.gate.execute(proposal.id)
    payload, context = h.calls[0]
    assert payload.order_id == "ORD-1"
    assert payload.currency == "USD"
    assert context.idempotency_key == "my-key"
    assert context.approved_by == "reviewer"


def test_rejected_proposal_cannot_run_or_be_approved(h):
    proposal = h.propose()
    h.gate.reject(proposal.id, "reviewer", "no")
    with pytest.raises(NotAllowed, match="rejected"):
        h.gate.approve(proposal.id, "other")
    with pytest.raises(NotAllowed, match="rejected"):
        h.gate.execute(proposal.id)
    assert h.calls == []


def test_pending_proposal_cannot_run(h):
    proposal = h.propose()
    with pytest.raises(NotAllowed, match="pending"):
        h.gate.execute(proposal.id)
    assert h.calls == []


def test_agent_cannot_approve_its_own_proposal(h):
    proposal = h.propose()
    with pytest.raises(NotAllowed, match="cannot also decide"):
        h.gate.approve(proposal.id, "bot")
    assert h.gate.get(proposal.id).state == "pending"


def test_agent_client_has_no_write_surface_beyond_propose(h):
    public = {name for name in dir(h.agent) if not name.startswith("_")}
    assert public == {"name", "propose", "status"}


def test_second_decision_is_refused(h):
    proposal = h.approved()
    with pytest.raises(NotAllowed, match="approved, not pending"):
        h.gate.reject(proposal.id, "other")


def test_empty_reviewer_is_refused(h):
    with pytest.raises(NotAllowed):
        h.gate.approve(h.propose().id, "  ")


def test_unknown_proposal(h):
    with pytest.raises(GateError, match="no proposal"):
        h.gate.get("p_missing")


def test_failed_executor_is_recorded_and_not_retried(h):
    h.fail_with = RuntimeError("downstream said no")
    proposal = h.approved()
    done = h.gate.execute(proposal.id)
    assert done.state == "failed"
    assert done.error == "RuntimeError: downstream said no"
    h.fail_with = None
    with pytest.raises(NotAllowed, match="failed"):
        h.gate.execute(proposal.id)
    assert len(h.calls) == 1


def test_non_json_result_is_kept_as_repr(h):
    class Weird:
        def __repr__(self):
            return "<Weird>"

    registry = Registry()

    @registry.action("refund", Refund)
    def run(payload, context):
        return Weird()

    h.gate.registry = registry
    assert h.gate.execute(h.approved().id).result == "<Weird>"


# Idempotency


def test_same_content_returns_the_same_proposal_without_a_new_event(h):
    first = h.propose()
    second = h.propose()
    assert second.id == first.id
    assert len(h.gate.log.events()) == 1


def test_default_key_ignores_field_order(h):
    first = h.propose(payload={"order_id": "A", "amount_cents": 1})
    second = h.propose(payload={"amount_cents": 1, "order_id": "A"})
    assert first.id == second.id


def test_explicit_key_with_different_content_conflicts(h):
    h.propose(idempotency_key="k", payload={"order_id": "A", "amount_cents": 1})
    with pytest.raises(IdempotencyConflict):
        h.propose(idempotency_key="k", payload={"order_id": "A", "amount_cents": 999})
    assert len(h.gate.log.events()) == 1


def test_distinct_keys_allow_a_real_repeat(h):
    first = h.propose(idempotency_key="one")
    second = h.propose(idempotency_key="two")
    assert first.id != second.id


def test_agent_retry_after_execution_does_not_run_again(h):
    proposal = h.approved()
    h.gate.execute(proposal.id)
    retried = h.propose()
    assert retried.id == proposal.id
    assert retried.state == "executed"
    assert len(h.calls) == 1


def test_rejection_sticks_so_an_agent_cannot_nag(h):
    proposal = h.propose()
    h.gate.reject(proposal.id, "reviewer")
    assert h.propose().state == "rejected"


# Typed proposals


@pytest.mark.parametrize(
    "payload",
    [
        {"order_id": "A"},
        {"order_id": "A", "amount_cents": "100"},
        {"order_id": "A", "amount_cents": 100.0},
        {"order_id": "A", "amount_cents": True},
        {"order_id": 1, "amount_cents": 100},
        {"order_id": "A", "amount_cents": 100, "extra": 1},
        {"order_id": "A", "amount_cents": 100, "currency": "XYZ"},
        {"order_id": "A", "amount_cents": -5},
        ["not", "an", "object"],
    ],
)
def test_bad_payload_is_refused_at_proposal_time(h, payload):
    with pytest.raises(SchemaError):
        h.propose(payload=payload)
    assert h.gate.log.events() == []


def test_unknown_action_is_refused(h):
    with pytest.raises(SchemaError, match="unknown action"):
        h.agent.propose("drop_table", {}, evidence=[Evidence("x://1")], reason="r")


def test_evidence_is_required_and_kept(h):
    with pytest.raises(SchemaError, match="evidence"):
        h.propose(evidence=[])
    digest = "a" * 64
    proposal = h.propose(evidence=[Evidence("logs://1", note="n", sha256=digest)])
    assert proposal.evidence == [Evidence("logs://1", note="n", sha256=digest)]


@pytest.mark.parametrize(("uri", "digest"), [("", None), ("  ", None), ("x://1", "nothex")])
def test_malformed_evidence_is_refused(uri, digest):
    with pytest.raises(SchemaError):
        Evidence(uri, sha256=digest)


def test_reason_is_required(h):
    with pytest.raises(SchemaError, match="reason"):
        h.propose(reason=" ")
