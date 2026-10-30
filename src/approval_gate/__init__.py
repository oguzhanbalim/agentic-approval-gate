"""agentic-approval-gate: agents propose, people decide, the log remembers."""

from approval_gate.actions import Registry, SchemaError
from approval_gate.gate import (
    AgentClient,
    Evidence,
    ExecutionContext,
    Gate,
    GateError,
    IdempotencyConflict,
    NotAllowed,
    Proposal,
    ProposalExpired,
)
from approval_gate.log import TamperedLog, VerifyResult

__all__ = [
    "AgentClient",
    "Evidence",
    "ExecutionContext",
    "Gate",
    "GateError",
    "IdempotencyConflict",
    "NotAllowed",
    "Proposal",
    "ProposalExpired",
    "Registry",
    "SchemaError",
    "TamperedLog",
    "VerifyResult",
]
__version__ = "0.1.0"
