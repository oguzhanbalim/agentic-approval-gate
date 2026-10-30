from __future__ import annotations

from datetime import timedelta

import pytest

from approval_gate import NotAllowed, ProposalExpired, SchemaError


def test_approval_after_the_deadline_is_refused_and_logged(h):
    proposal = h.propose(ttl=timedelta(minutes=10))
    h.clock.advance(minutes=10)
    with pytest.raises(ProposalExpired):
        h.gate.approve(proposal.id, "reviewer")
    stored = h.gate.get(proposal.id)
    assert stored.state == "expired"
    # The refusal survived: the expiry event was committed, not rolled back with the error.
    assert [event.kind for event in stored.events] == ["proposed", "expired"]
    assert stored.decided_by is None


def test_just_before_the_deadline_is_fine(h):
    proposal = h.propose(ttl=timedelta(minutes=10))
    h.clock.advance(minutes=9, seconds=59)
    assert h.gate.approve(proposal.id, "reviewer").state == "approved"


def test_rejection_after_the_deadline_is_also_refused(h):
    proposal = h.propose(ttl=timedelta(minutes=10))
    h.clock.advance(hours=1)
    with pytest.raises(ProposalExpired):
        h.gate.reject(proposal.id, "reviewer")


def test_approval_does_not_outlive_the_deadline(h):
    proposal = h.approved(ttl=timedelta(minutes=10))
    h.clock.advance(minutes=11)
    with pytest.raises(ProposalExpired):
        h.gate.execute(proposal.id)
    assert h.calls == []
    assert h.gate.get(proposal.id).state == "expired"
    assert h.gate.verify().ok


def test_state_reads_expired_before_any_event_is_written(h):
    proposal = h.propose(ttl=timedelta(minutes=10))
    h.clock.advance(hours=1)
    assert h.gate.get(proposal.id).state == "expired"
    assert h.gate.list("pending") == []
    assert [p.id for p in h.gate.list("expired")] == [proposal.id]
    assert len(h.gate.log.events()) == 1


def test_expire_due_writes_once(h):
    proposal = h.propose(ttl=timedelta(minutes=10))
    h.clock.advance(hours=1)
    assert h.gate.expire_due() == [proposal.id]
    assert h.gate.expire_due() == []
    assert [e.kind for e in h.gate.log.events()] == ["proposed", "expired"]


def test_expire_due_leaves_live_proposals_alone(h):
    h.propose(ttl=timedelta(hours=2))
    h.clock.advance(hours=1)
    assert h.gate.expire_due() == []


def test_execution_in_progress_is_not_expired(h):
    # Once claimed, the deadline no longer applies; the outcome must still be recorded.
    proposal = h.approved(ttl=timedelta(minutes=10))
    h.gate._claim(proposal.id)
    h.clock.advance(hours=1)
    assert h.gate.get(proposal.id).state == "in_doubt"
    assert h.gate.expire_due() == []


def test_expired_proposal_cannot_be_revived(h):
    proposal = h.propose(ttl=timedelta(minutes=10))
    h.clock.advance(hours=1)
    h.gate.expire_due()
    assert h.propose().state == "expired"
    with pytest.raises(ProposalExpired):
        h.gate.approve(proposal.id, "reviewer")
    assert len(h.gate.log.events()) == 2


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1), timedelta(days=8)])
def test_ttl_must_be_positive_and_bounded(h, ttl):
    with pytest.raises(SchemaError, match="ttl"):
        h.propose(ttl=ttl)


def test_second_execute_after_expiry_check_stays_refused(h):
    proposal = h.approved(ttl=timedelta(minutes=10))
    h.gate.execute(proposal.id)
    h.clock.advance(hours=1)
    # Finished work stays finished; the deadline does not rewrite it.
    assert h.gate.get(proposal.id).state == "executed"
    with pytest.raises(NotAllowed):
        h.gate.execute(proposal.id)
