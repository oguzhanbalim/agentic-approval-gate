"""Two synthetic actions. The executors only append a line to a local demo ledger file."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from approval_gate import ExecutionContext, Registry

registry = Registry()
LEDGER = Path(os.environ.get("APPROVAL_GATE_DEMO_LEDGER", "demo_ledger.jsonl"))


@dataclass(frozen=True)
class IssueRefund:
    order_id: str
    amount_cents: int
    currency: Literal["USD", "EUR"]

    def __post_init__(self) -> None:
        if self.amount_cents <= 0:
            raise ValueError("amount_cents must be positive")


@dataclass(frozen=True)
class PostStatus:
    channel: str
    text: str


def _append(entry: dict) -> None:
    with LEDGER.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


@registry.action("issue_refund", IssueRefund)
def run_refund(payload: IssueRefund, context: ExecutionContext) -> dict:
    _append({"refund": payload.order_id, "cents": payload.amount_cents, "key": context.proposal_id})
    return {"refunded_cents": payload.amount_cents}


@registry.action("post_status", PostStatus, min_evidence=0)
def run_status(payload: PostStatus, context: ExecutionContext) -> dict:
    _append({"status": payload.text, "channel": payload.channel, "key": context.proposal_id})
    return {"posted_to": payload.channel}
