"""The proposal lifecycle.

    proposed -> approved -> execution_started -> executed
       |          |                |-----------> execution_failed
       |          '-> expired
       |-> rejected
       '-> expired

Proposal state is never stored. It is derived from the log each time, so there is no second
copy to disagree with the record.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from approval_gate.actions import Action, Registry, SchemaError, load_payload
from approval_gate.log import Event, EventLog, VerifyResult, canonical

SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class GateError(Exception):
    """Base class for refusals from the gate."""


class NotAllowed(GateError):
    """The proposal is not in a state that permits this, or this actor may not do it."""


class ProposalExpired(GateError):
    """The proposal passed its deadline."""


class IdempotencyConflict(GateError):
    """The key was already used for a different action or payload."""


@dataclass(frozen=True)
class Evidence:
    """A pointer to what supports a proposal. The gate stores it and does not fetch it."""

    uri: str
    note: str = ""
    sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.uri, str) or not self.uri.strip():
            raise SchemaError("evidence uri must be a non-empty string")
        if self.sha256 is not None and not SHA256_PATTERN.fullmatch(str(self.sha256)):
            raise SchemaError("evidence sha256 must be 64 lowercase hex characters")


@dataclass(frozen=True)
class ExecutionContext:
    """Passed to executors. Send ``idempotency_key`` downstream where the target supports it."""

    proposal_id: str
    idempotency_key: str
    approved_by: str


@dataclass
class Proposal:
    id: str
    action: str
    payload: dict[str, Any]
    payload_sha256: str
    evidence: list[Evidence]
    reason: str
    proposer: str
    proposed_at: datetime
    expires_at: datetime
    idempotency_key: str
    # pending, approved, rejected, expired, in_doubt, executed or failed.
    state: str = "pending"
    decided_by: str | None = None
    note: str | None = None
    result: Any = None
    error: str | None = None
    events: list[Event] = field(default_factory=list)


def _hash_payload(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def _build(events: list[Event], now: datetime) -> Proposal:
    first = events[0].data
    proposal = Proposal(
        id=events[0].proposal_id,
        action=first["action"],
        payload=first["payload"],
        payload_sha256=first["payload_sha256"],
        evidence=[Evidence(**item) for item in first["evidence"]],
        reason=first["reason"],
        proposer=events[0].actor,
        proposed_at=datetime.fromisoformat(events[0].at),
        expires_at=datetime.fromisoformat(first["expires_at"]),
        idempotency_key=events[0].idempotency_key or "",
        events=events,
    )
    for event in events[1:]:
        if event.kind in ("approved", "rejected"):
            proposal.state = event.kind
            proposal.decided_by = event.actor
            proposal.note = event.data.get("note")
        elif event.kind == "expired":
            proposal.state = "expired"
        elif event.kind == "execution_started":
            proposal.state = "in_doubt"
        elif event.kind == "executed":
            proposal.state = "executed"
            proposal.result = event.data.get("result")
        elif event.kind == "execution_failed":
            proposal.state = "failed"
            proposal.error = event.data.get("error")
    # A deadline counts from the moment it passes, whether or not an event was written yet.
    if proposal.state in ("pending", "approved") and now >= proposal.expires_at:
        proposal.state = "expired"
    return proposal


class AgentClient:
    """All an agent gets: it can propose, and it can read the status of a proposal."""

    def __init__(self, gate: Gate, name: str) -> None:
        self._gate = gate
        self.name = name

    def propose(
        self,
        action: str,
        payload: dict[str, Any],
        *,
        evidence: Sequence[Evidence],
        reason: str,
        ttl: timedelta = timedelta(hours=1),
        idempotency_key: str | None = None,
    ) -> Proposal:
        return self._gate._propose(
            self.name, action, payload, evidence, reason, ttl, idempotency_key
        )

    def status(self, proposal_id: str) -> Proposal:
        return self._gate.get(proposal_id)


class Gate:
    """One queue, backed by one SQLite file. Use one Gate per thread."""

    def __init__(
        self,
        path: str | Path,
        registry: Registry,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_ttl: timedelta = timedelta(days=7),
    ) -> None:
        self.log = EventLog(path)
        self.registry = registry
        self.clock = clock
        self.max_ttl = max_ttl

    def close(self) -> None:
        self.log.close()

    def agent(self, name: str) -> AgentClient:
        if not name.strip():
            raise ValueError("agent name must not be empty")
        return AgentClient(self, name)

    # Reading

    def get(self, proposal_id: str) -> Proposal:
        events = self.log.events(proposal_id)
        if not events:
            raise GateError(f"no proposal {proposal_id}")
        return _build(events, self.clock())

    def list(self, state: str | None = None) -> list[Proposal]:
        grouped: dict[str, list[Event]] = {}
        for event in self.log.events():
            grouped.setdefault(event.proposal_id, []).append(event)
        now = self.clock()
        proposals = [_build(events, now) for events in grouped.values()]
        return [p for p in proposals if state is None or p.state == state]

    def verify(self, anchor: tuple[int, str] | None = None) -> VerifyResult:
        return self.log.verify(anchor)

    # Agent side

    def _propose(
        self,
        proposer: str,
        action_name: str,
        payload: dict[str, Any],
        evidence: Sequence[Evidence],
        reason: str,
        ttl: timedelta,
        idempotency_key: str | None,
    ) -> Proposal:
        action = self.registry.get(action_name)
        if action is None:
            raise SchemaError(f"unknown action {action_name!r}; known: {self.registry.names()}")
        load_payload(action.payload_type, payload)
        if len(evidence) < action.min_evidence:
            raise SchemaError(
                f"{action_name} needs at least {action.min_evidence} evidence reference(s)"
            )
        if ttl <= timedelta(0) or ttl > self.max_ttl:
            raise SchemaError(f"ttl must be positive and at most {self.max_ttl}")
        if not reason.strip():
            raise SchemaError("a proposal needs a reason")

        payload_sha256 = _hash_payload(payload)
        key = idempotency_key or f"auto:{action_name}:{payload_sha256}"
        proposal_id = "p_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        now = self.clock()
        with self.log.transaction():
            existing = self.log.find_by_key(key)
            if existing is None:
                self.log.append(
                    proposal_id,
                    "proposed",
                    proposer,
                    now.isoformat(),
                    {
                        "action": action_name,
                        "payload": payload,
                        "payload_sha256": payload_sha256,
                        "evidence": [dataclasses.asdict(item) for item in evidence],
                        "reason": reason,
                        "expires_at": (now + ttl).isoformat(),
                    },
                    idempotency_key=key,
                )
        proposal = self.get(proposal_id)
        if proposal.action != action_name or proposal.payload_sha256 != payload_sha256:
            raise IdempotencyConflict(
                f"key {key!r} was already used by {proposal.id} for different content"
            )
        return proposal

    # Reviewer side

    def _decide(self, proposal_id: str, kind: str, reviewer: str, note: str | None) -> Proposal:
        if not reviewer.strip():
            raise NotAllowed("a decision needs a reviewer name")
        expired = False
        with self.log.transaction():
            self.log.require_intact()
            proposal = self.get(proposal_id)
            if reviewer == proposal.proposer:
                raise NotAllowed(f"{reviewer} proposed this and cannot also decide it")
            if proposal.state == "expired":
                self._write_expiry(proposal)
                expired = True
            elif proposal.state != "pending":
                raise NotAllowed(f"{proposal_id} is {proposal.state}, not pending")
            else:
                self.log.append(
                    proposal_id,
                    kind,
                    reviewer,
                    self.clock().isoformat(),
                    {"note": note, "payload_sha256": proposal.payload_sha256},
                )
        if expired:
            raise ProposalExpired(f"{proposal_id} expired at {proposal.expires_at.isoformat()}")
        return self.get(proposal_id)

    def approve(self, proposal_id: str, reviewer: str, note: str | None = None) -> Proposal:
        return self._decide(proposal_id, "approved", reviewer, note)

    def reject(self, proposal_id: str, reviewer: str, note: str | None = None) -> Proposal:
        return self._decide(proposal_id, "rejected", reviewer, note)

    def _write_expiry(self, proposal: Proposal) -> None:
        if not any(event.kind == "expired" for event in proposal.events):
            self.log.append(proposal.id, "expired", "gate", self.clock().isoformat(), {})

    def expire_due(self) -> list[str]:
        """Write the expiry event for anything past its deadline. Safe to run on a schedule."""
        written = []
        with self.log.transaction():
            for proposal in self.list("expired"):
                if not any(event.kind == "expired" for event in proposal.events):
                    self._write_expiry(proposal)
                    written.append(proposal.id)
        return written

    # Execution

    def _claim(self, proposal_id: str) -> tuple[Proposal, Action, Any]:
        """Atomically move an approved proposal to execution_started, or refuse."""
        expired = False
        action: Action | None = None
        loaded: Any = None
        with self.log.transaction():
            self.log.require_intact()
            proposal = self.get(proposal_id)
            if proposal.state == "expired":
                self._write_expiry(proposal)
                expired = True
            elif proposal.state != "approved":
                raise NotAllowed(f"{proposal_id} is {proposal.state}, not approved")
            else:
                action = self.registry.get(proposal.action)
                if action is None:
                    raise NotAllowed(f"no executor registered for {proposal.action!r}")
                loaded = load_payload(action.payload_type, proposal.payload)
                self.log.append(
                    proposal_id,
                    "execution_started",
                    "gate",
                    self.clock().isoformat(),
                    {"payload_sha256": proposal.payload_sha256},
                )
        if expired:
            raise ProposalExpired(f"{proposal_id} expired at {proposal.expires_at.isoformat()}")
        assert action is not None
        return self.get(proposal_id), action, loaded

    def execute(self, proposal_id: str) -> Proposal:
        """Run an approved proposal's executor, at most once, ever.

        The claim is committed before the executor starts, so a crash mid-run leaves the proposal
        ``in_doubt`` rather than eligible to run again. A failing executor is recorded and not
        retried. To try again, a person approves a new proposal with a new key.
        """
        proposal, action, loaded = self._claim(proposal_id)
        context = ExecutionContext(
            proposal.id, proposal.idempotency_key, proposal.decided_by or "unknown"
        )
        try:
            result = action.executor(loaded, context)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._record_outcome(proposal_id, "execution_failed", "gate", {"error": error})
        else:
            self._record_outcome(proposal_id, "executed", "gate", {"result": _jsonable(result)})
        return self.get(proposal_id)

    def _record_outcome(
        self, proposal_id: str, kind: str, actor: str, data: dict[str, Any]
    ) -> None:
        with self.log.transaction():
            self.log.append(proposal_id, kind, actor, self.clock().isoformat(), data)

    def execute_approved(self) -> list[Proposal]:
        """Run everything approved. A proposal another worker claims first is skipped."""
        finished = []
        for proposal in self.list("approved"):
            try:
                finished.append(self.execute(proposal.id))
            except (NotAllowed, ProposalExpired):
                continue
        return finished

    def resolve(self, proposal_id: str, reviewer: str, *, succeeded: bool, note: str) -> Proposal:
        """Record what a person found out about an in-doubt execution. It never re-runs anything."""
        if not reviewer.strip() or not note.strip():
            raise NotAllowed("resolving needs a reviewer name and a note saying what was checked")
        with self.log.transaction():
            self.log.require_intact()
            proposal = self.get(proposal_id)
            if proposal.state != "in_doubt":
                raise NotAllowed(f"{proposal_id} is {proposal.state}, not in_doubt")
            if succeeded:
                self.log.append(
                    proposal_id,
                    "executed",
                    reviewer,
                    self.clock().isoformat(),
                    {"result": None, "resolved": True, "note": note},
                )
            else:
                self.log.append(
                    proposal_id,
                    "execution_failed",
                    reviewer,
                    self.clock().isoformat(),
                    {"error": "resolved by hand", "resolved": True, "note": note},
                )
        return self.get(proposal_id)


def _jsonable(value: Any) -> Any:
    """Results land in the log, so anything that is not JSON becomes its repr."""
    try:
        canonical(value)
    except (TypeError, ValueError):
        return repr(value)
    return value
