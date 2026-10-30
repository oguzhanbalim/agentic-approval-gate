from __future__ import annotations

import json
import sqlite3

import pytest

from approval_gate import TamperedLog
from approval_gate.log import GENESIS, canonical, event_hash


def seeded(h):
    """One finished proposal, one approved, one pending."""
    done = h.approved(idempotency_key="done")
    h.gate.execute(done.id)
    approved = h.approved(idempotency_key="approved")
    pending = h.propose(idempotency_key="pending")
    return done, approved, pending


def rehash_from(conn):
    """What a careful attacker does: recompute every hash after an edit."""
    rows = conn.execute(
        "SELECT seq, proposal_id, kind, actor, at, idempotency_key, data FROM events ORDER BY seq"
    ).fetchall()
    prev = GENESIS
    for seq, proposal_id, kind, actor, at, key, data in rows:
        digest = event_hash(prev, seq, proposal_id, kind, actor, at, key, data)
        conn.execute("UPDATE events SET prev_hash = ?, hash = ? WHERE seq = ?", (prev, digest, seq))
        prev = digest


def test_clean_log_verifies(h):
    seeded(h)
    result = h.gate.verify()
    assert result.ok
    assert result.events == len(h.gate.log.events())
    assert (result.head_seq, result.head_hash) == h.gate.log.head()


def test_empty_log_verifies(h):
    result = h.gate.verify()
    assert result.ok
    assert result.events == 0
    assert result.head_hash == GENESIS


def test_triggers_refuse_update_and_delete(h):
    seeded(h)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        h.gate.log.conn.execute("UPDATE events SET actor = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        h.gate.log.conn.execute("DELETE FROM events")


def edit_data(conn, seq, **changes):
    data = json.loads(conn.execute("SELECT data FROM events WHERE seq = ?", (seq,)).fetchone()[0])
    data.update(changes)
    conn.execute("UPDATE events SET data = ? WHERE seq = ?", (canonical(data), seq))


def test_changed_payload_is_detected_with_the_seq(h):
    seeded(h)
    edit_data(h.raw(), 1, payload={"order_id": "ORD-1", "amount_cents": 999999})
    result = h.gate.verify()
    assert not result.ok
    assert "seq 1" in result.problem
    assert "contents" in result.problem


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("actor", "somebody-else"),
        ("kind", "rejected"),
        ("at", "2020-01-01T00:00:00+00:00"),
        ("proposal_id", "p_other"),
        ("idempotency_key", "other"),
        ("prev_hash", "e" * 64),
        ("hash", "f" * 64),
    ],
)
def test_changing_any_column_is_detected(h, column, value):
    seeded(h)
    h.raw().execute(f"UPDATE events SET {column} = ? WHERE seq = 2", (value,))
    assert not h.gate.verify().ok


def test_deleted_middle_event_is_detected(h):
    seeded(h)
    h.raw().execute("DELETE FROM events WHERE seq = 2")
    result = h.gate.verify()
    assert not result.ok
    assert "missing" in result.problem


def test_reordered_events_are_detected(h):
    seeded(h)
    conn = h.raw()
    conn.execute("UPDATE events SET seq = 1000 WHERE seq = 1")
    conn.execute("UPDATE events SET seq = 1 WHERE seq = 2")
    conn.execute("UPDATE events SET seq = 2 WHERE seq = 1000")
    assert not h.gate.verify().ok


def test_forged_insert_is_detected(h):
    seeded(h)
    head_seq, head_hash = h.gate.log.head()
    h.raw().execute(
        "INSERT INTO events (seq, proposal_id, kind, actor, at, data, prev_hash, hash) "
        "VALUES (?, 'p_x', 'approved', 'forger', 't', '{}', ?, ?)",
        (head_seq + 1, head_hash, "0" * 64),
    )
    assert not h.gate.verify().ok


def test_tampered_payload_is_never_executed(h):
    proposal = h.approved()
    edit_data(h.raw(), 1, payload={"order_id": "ORD-1", "amount_cents": 999999})
    with pytest.raises(TamperedLog):
        h.gate.execute(proposal.id)
    assert h.calls == []
    assert [e.kind for e in h.gate.log.events(proposal.id)] == ["proposed", "approved"]


def test_tampered_log_blocks_new_decisions(h):
    proposal = h.propose(idempotency_key="one")
    h.approved(idempotency_key="two")
    h.raw().execute("UPDATE events SET actor = 'someone' WHERE seq = 2")
    with pytest.raises(TamperedLog):
        h.gate.approve(proposal.id, "reviewer")
    with pytest.raises(TamperedLog):
        h.gate.reject(proposal.id, "reviewer")


def test_forged_approval_via_edited_rejection_is_caught(h):
    proposal = h.propose()
    h.gate.reject(proposal.id, "reviewer")
    h.raw().execute("UPDATE events SET kind = 'approved' WHERE seq = 2")
    with pytest.raises(TamperedLog):
        h.gate.execute(proposal.id)
    assert h.calls == []


# The limits of a bare hash chain, stated as tests.


def test_recomputed_chain_passes_without_an_anchor_but_fails_with_one(h):
    seeded(h)
    anchor = h.gate.log.head()
    conn = h.raw()
    edit_data(conn, 1, payload={"order_id": "ORD-1", "amount_cents": 999999})
    rehash_from(conn)
    # Someone with write access who recomputes everything defeats the chain alone.
    assert h.gate.verify().ok
    result = h.gate.verify(anchor=anchor)
    assert not result.ok
    assert "anchor" in result.problem


def test_truncated_tail_passes_without_an_anchor_but_fails_with_one(h):
    seeded(h)
    anchor = h.gate.log.head()
    h.raw().execute("DELETE FROM events WHERE seq = ?", (anchor[0],))
    assert h.gate.verify().ok
    result = h.gate.verify(anchor=anchor)
    assert not result.ok
    assert "removed" in result.problem


def test_older_anchor_still_verifies_after_more_events(h):
    h.propose(idempotency_key="one")
    anchor = h.gate.log.head()
    h.propose(idempotency_key="two")
    assert h.gate.verify(anchor=anchor).ok


def test_recomputed_chain_with_an_illegal_lifecycle_is_still_caught(h):
    # An execution with no approval is wrong even if every hash lines up.
    proposal = h.propose()
    conn = h.raw()
    conn.execute(
        "INSERT INTO events (seq, proposal_id, kind, actor, at, data, prev_hash, hash) "
        "VALUES (2, ?, 'execution_started', 'gate', 't', '{}', '', '')",
        (proposal.id,),
    )
    rehash_from(conn)
    result = h.gate.verify()
    assert not result.ok
    assert "cannot follow" in result.problem
