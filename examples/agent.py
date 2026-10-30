"""A synthetic agent. It can only propose. Run it twice and nothing new appears."""

from __future__ import annotations

import sys

from actions import registry

from approval_gate import Evidence, Gate, SchemaError

gate = Gate(sys.argv[1], registry)
agent = gate.agent("refund-bot")

refund = agent.propose(
    "issue_refund",
    {"order_id": "ORD-1042", "amount_cents": 4000, "currency": "USD"},
    evidence=[
        Evidence("ticket://SYN-1001", note="customer reports broken item (synthetic)"),
        Evidence("orders://ORD-1042", note="total 4000 cents, delivered (synthetic)"),
    ],
    reason="Item arrived broken; refund the full order.",
)
print("proposed", refund.id, refund.state)

status = agent.propose(
    "post_status",
    {"channel": "#synthetic-demo", "text": "All refunds are free today!"},
    evidence=[],
    reason="Thought people would like this.",
)
print("proposed", status.id, status.state)

try:
    agent.propose(
        "issue_refund",
        {"order_id": "ORD-1", "amount_cents": "4000", "currency": "USD"},
        evidence=[Evidence("ticket://SYN-1002")],
        reason="Wrong type on purpose.",
    )
except SchemaError as exc:
    print("refused at the door:", exc)

gate.close()
