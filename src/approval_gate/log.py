"""The append-only, hash-chained event log in SQLite.

Each event commits to the one before it:

    hash = sha256(prev_hash + canonical_json(seq, proposal_id, kind, actor, at, key, data))

Changing, removing or reordering any row breaks every later link. Triggers refuse UPDATE and
DELETE, which stops accidents. Partial unique indexes enforce the lifecycle in the database, so
two processes cannot both claim the same execution, even if the code above them is wrong.

What this does not stop: someone with write access can drop the triggers and recompute the whole
chain from the point they edited. Only a head hash stored elsewhere catches that, which is what
``verify(anchor=...)`` is for.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GENESIS = "0" * 64

# Event kind -> the event kinds that may follow it for the same proposal.
TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({"proposed"}),
    "proposed": frozenset({"approved", "rejected", "expired"}),
    "approved": frozenset({"execution_started", "expired"}),
    "execution_started": frozenset({"executed", "execution_failed"}),
    "rejected": frozenset(),
    "expired": frozenset(),
    "executed": frozenset(),
    "execution_failed": frozenset(),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    at TEXT NOT NULL,
    idempotency_key TEXT,
    data TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_proposal ON events (proposal_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS one_proposal_per_key
    ON events (idempotency_key) WHERE kind = 'proposed';
CREATE UNIQUE INDEX IF NOT EXISTS one_proposed_event
    ON events (proposal_id) WHERE kind = 'proposed';
CREATE UNIQUE INDEX IF NOT EXISTS one_decision
    ON events (proposal_id) WHERE kind IN ('approved', 'rejected');
CREATE UNIQUE INDEX IF NOT EXISTS one_expiry
    ON events (proposal_id) WHERE kind = 'expired';
CREATE UNIQUE INDEX IF NOT EXISTS one_execution_claim
    ON events (proposal_id) WHERE kind = 'execution_started';
CREATE UNIQUE INDEX IF NOT EXISTS one_execution_outcome
    ON events (proposal_id) WHERE kind IN ('executed', 'execution_failed');
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
    BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
    BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
"""


class TamperedLog(Exception):
    """The log failed verification. Nothing should be decided or run on top of it."""


@dataclass(frozen=True)
class Event:
    seq: int
    proposal_id: str
    kind: str
    actor: str
    at: str
    idempotency_key: str | None
    data: dict[str, Any]
    prev_hash: str
    hash: str


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    events: int
    head_seq: int
    head_hash: str
    problem: str | None = None


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_hash(
    prev_hash: str,
    seq: int,
    proposal_id: str,
    kind: str,
    actor: str,
    at: str,
    idempotency_key: str | None,
    data_json: str,
) -> str:
    body = canonical([seq, proposal_id, kind, actor, at, idempotency_key, data_json])
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


class EventLog:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        # Autocommit mode: every write transaction below is opened explicitly.
        self.conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Hold the write lock, so reading the head and appending to it is one atomic step."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def append(
        self,
        proposal_id: str,
        kind: str,
        actor: str,
        at: str,
        data: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> Event:
        """Append one event. Call inside ``transaction()``."""
        row = self.conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        seq, prev_hash = (row[0] + 1, row[1]) if row else (1, GENESIS)
        data_json = canonical(data)
        digest = event_hash(
            prev_hash, seq, proposal_id, kind, actor, at, idempotency_key, data_json
        )
        self.conn.execute(
            "INSERT INTO events (seq, proposal_id, kind, actor, at, idempotency_key, data, "
            "prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (seq, proposal_id, kind, actor, at, idempotency_key, data_json, prev_hash, digest),
        )
        return Event(seq, proposal_id, kind, actor, at, idempotency_key, data, prev_hash, digest)

    def _rows(self, where: str = "", params: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
        return self.conn.execute(
            "SELECT seq, proposal_id, kind, actor, at, idempotency_key, data, prev_hash, hash "
            f"FROM events {where} ORDER BY seq",
            params,
        )

    def events(self, proposal_id: str | None = None) -> list[Event]:
        rows = self._rows("WHERE proposal_id = ?", (proposal_id,)) if proposal_id else self._rows()
        return [Event(*row[:6], json.loads(row[6]), row[7], row[8]) for row in rows]

    def find_by_key(self, idempotency_key: str) -> str | None:
        row = self.conn.execute(
            "SELECT proposal_id FROM events WHERE kind = 'proposed' AND idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return row[0] if row else None

    def head(self) -> tuple[int, str]:
        row = self.conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return (row[0], row[1]) if row else (0, GENESIS)

    def verify(self, anchor: tuple[int, str] | None = None) -> VerifyResult:
        """Walk the whole chain and the lifecycle of every proposal.

        ``anchor`` is a ``(seq, hash)`` pair recorded earlier and kept somewhere else. It catches
        truncation and a full rewrite, which the chain alone cannot.
        """
        prev_hash = GENESIS
        expected_seq = 1
        last_kind: dict[str, str] = {}
        anchor_seen = anchor is None or anchor[0] == 0

        def fail(reason: str) -> VerifyResult:
            return VerifyResult(False, expected_seq - 1, expected_seq - 1, prev_hash, reason)

        for row in self._rows():
            seq, proposal_id, kind, actor, at, key, data_json, stored_prev, stored_hash = row
            if seq != expected_seq:
                return fail(f"expected seq {expected_seq}, found {seq}: an event is missing")
            if stored_prev != prev_hash:
                return fail(f"seq {seq}: prev_hash does not match the previous event")
            computed = event_hash(prev_hash, seq, proposal_id, kind, actor, at, key, data_json)
            if computed != stored_hash:
                return fail(f"seq {seq}: contents do not match the stored hash")
            if kind not in TRANSITIONS[last_kind.get(proposal_id)]:
                return fail(
                    f"seq {seq}: {kind!r} cannot follow {last_kind.get(proposal_id)!r} "
                    f"for proposal {proposal_id}"
                )
            if anchor is not None and seq == anchor[0]:
                if stored_hash != anchor[1]:
                    return fail(f"seq {seq}: hash differs from the anchor; history was rewritten")
                anchor_seen = True
            last_kind[proposal_id] = kind
            prev_hash = stored_hash
            expected_seq = seq + 1

        if not anchor_seen:
            return fail(f"anchor seq {anchor[0]} is not in the log; events were removed")
        return VerifyResult(True, expected_seq - 1, expected_seq - 1, prev_hash)

    def require_intact(self) -> None:
        result = self.verify()
        if not result.ok:
            raise TamperedLog(result.problem)
