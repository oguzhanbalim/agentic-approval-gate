# agentic-approval-gate

Agents propose, people decide, and the log can prove what happened.

An agent that can write to production needs a brake that does not depend on the model behaving
well. This is a small queue: agents can only submit typed proposals with evidence, a person
approves or rejects them in a CLI, and approved actions run through a registered executor at
most once.

Everything in this README is synthetic. The executors only append to a local file. No model is
involved at any step, and the agent in the example is a short script.

## Quickstart

```bash
pip install -e ".[dev]"
cd examples
```

A scripted agent proposes two actions. A third has a wrong type and never reaches the queue:

```
$ python agent.py demo.db
proposed p_0785adeefd57feee pending
proposed p_f8e2f6dddabe3458 pending
refused at the door: amount_cents: expected int
$ approval-gate --db demo.db --registry actions:registry list
p_0785adeefd57feee  pending    issue_refund      by refund-bot    expires 2026-10-30T10:33+00:00
p_f8e2f6dddabe3458  pending    post_status       by refund-bot    expires 2026-10-30T10:33+00:00
```

The reviewer sees exactly what would run and why:

```
$ approval-gate --db demo.db --registry actions:registry show p_0785adeefd57feee
id:        p_0785adeefd57feee
state:     pending
action:    issue_refund
proposer:  refund-bot
reason:    Item arrived broken; refund the full order.
expires:   2026-10-30T10:33:16+00:00
payload:   {"amount_cents": 4000, "currency": "USD", "order_id": "ORD-1042"}
sha256:    541aa009cd657cc680494d1faf8a9a4f7b8b1a8edf1621bf07499125ba5fdc88
evidence:
  - ticket://SYN-1001 (customer reports broken item (synthetic))
  - orders://ORD-1042 (total 4000 cents, delivered (synthetic))
```

Decide. Without `--yes`, the CLI shows the proposal and waits for you to type `yes`:

```
$ approval-gate --db demo.db --registry actions:registry approve p_0785adeefd57feee --reviewer reviewer-1 --note "checked the ticket" --yes | tail -1
p_0785adeefd57feee approved by reviewer-1
$ approval-gate --db demo.db --registry actions:registry reject p_f8e2f6dddabe3458 --reviewer reviewer-1 --note "no" --yes | tail -1
p_f8e2f6dddabe3458 rejected by reviewer-1
```

Execute. The second attempt is refused, the ledger has one line, and the agent's retry gets the
existing proposals back rather than creating new ones:

```
$ approval-gate --db demo.db --registry actions:registry execute
p_0785adeefd57feee  executed  {"refunded_cents": 4000}
$ approval-gate --db demo.db --registry actions:registry execute p_0785adeefd57feee
refused: p_0785adeefd57feee is executed, not approved
$ cat demo_ledger.jsonl
{"cents": 4000, "key": "p_0785adeefd57feee", "refund": "ORD-1042"}
$ python agent.py demo.db | head -2
proposed p_0785adeefd57feee executed
proposed p_f8e2f6dddabe3458 rejected
```

Every step is in the chain:

```
$ approval-gate --db demo.db --registry actions:registry log
   1 d29418fdec5c 2026-10-30T09:33:16 p_0785adeefd57feee proposed           refund-bot
   2 e0f7a78fe81e 2026-10-30T09:33:16 p_f8e2f6dddabe3458 proposed           refund-bot
   3 7e6556a0a7c0 2026-10-30T09:33:16 p_0785adeefd57feee approved           reviewer-1
   4 791e750ef9eb 2026-10-30T09:33:16 p_f8e2f6dddabe3458 rejected           reviewer-1
   5 876159d2f742 2026-10-30T09:33:16 p_0785adeefd57feee execution_started  gate
   6 be9fa954d850 2026-10-30T09:33:16 p_0785adeefd57feee executed           gate
$ approval-gate --db demo.db --registry actions:registry verify
ok: 6 events, head 6:be9fa954d85010ec1e3a8292b299b94aa3b04ce6c66b32f30e656b1c1943ed44
```

Now tamper with the database to turn the rejection into an approval:

```
$ sqlite3 demo.db "DROP TRIGGER events_no_update; UPDATE events SET kind = 'approved' WHERE proposal_id = 'p_f8e2f6dddabe3458' AND kind = 'rejected';"
$ approval-gate --db demo.db --registry actions:registry verify
TAMPERED: seq 4: contents do not match the stored hash
$ approval-gate --db demo.db --registry actions:registry execute p_f8e2f6dddabe3458
refused: seq 4: contents do not match the stored hash
```

## Defining an action

```python
from dataclasses import dataclass
from typing import Literal

from approval_gate import Evidence, ExecutionContext, Gate, Registry

registry = Registry()

@dataclass(frozen=True)
class IssueRefund:
    order_id: str
    amount_cents: int
    currency: Literal["USD", "EUR"]

@registry.action("issue_refund", IssueRefund)
def run_refund(payload: IssueRefund, context: ExecutionContext) -> dict:
    # Send context.idempotency_key downstream if the target supports one.
    ...

agent = Gate("gate.db", registry).agent("refund-bot")
agent.propose(
    "issue_refund",
    {"order_id": "ORD-1", "amount_cents": 4000, "currency": "USD"},
    evidence=[Evidence("ticket://SYN-1", note="why this is justified")],
    reason="Item arrived broken.",
)
```

## Guarantees and how they are enforced

| Property | Mechanism |
| --- | --- |
| Agents only propose | `AgentClient` exposes `propose` and `status`. The CLI has no propose command. A proposer cannot decide its own proposal. |
| Typed | Payloads are checked against a dataclass with no coercion. `"100"`, `100.0` and `True` are not an `int`. Unknown and missing fields are refused. |
| Idempotent | The key defaults to a hash of action and payload. A retry returns the existing proposal. A reused key with different content is an error. |
| Rejection sticks | Re-proposing the same content returns the rejected proposal, so an agent cannot ask until someone tires. A real repeat needs a new key. |
| Evidence | At least one reference is required by default, with an optional sha256. |
| Expiry | Every proposal has a deadline, capped by `max_ttl`. An approval does not outlive it: a proposal approved and not run in time is refused. |
| At most one run | A claim is committed before the executor starts. A partial unique index refuses a second claim in the database, across threads and processes. |
| Crash safety | A crash between claim and outcome leaves `in_doubt`. It is never re-run. A person checks the target and records the result with `resolve`. |
| Failures are final | A raising executor is recorded as `failed` and not retried. |
| Tamper detection | Each event hashes the one before it. `approve`, `reject`, `execute` and `resolve` verify the chain first and refuse on a mismatch. |

State is never stored. It is derived from the event log every time, so the two cannot disagree.

### "Exactly once"

The gate guarantees that it starts an approved executor at most once, and that every run ends in a
recorded outcome or a visible `in_doubt`. It cannot make an external side effect atomic with a
database write. If the process dies after the side effect and before the outcome is written, you
get `in_doubt`, not a silent re-run. For true exactly-once effects, pass `context.idempotency_key`
to a target that honors it.

### What the hash chain does and does not prove

A bare hash chain has two known holes, and the tests state both of them:

- Someone with write access can recompute every hash after an edit.
- Someone can delete events from the end.

The defense is to store the head somewhere the writer cannot reach:

```bash
approval-gate --db gate.db head                     # Save this elsewhere, for example in CI logs.
approval-gate --db gate.db verify --anchor 7:ab12...  # Later: catches both holes.
```

`verify` also replays every proposal's lifecycle, so a recomputed chain containing an execution
with no approval still fails.

## Tests

```
$ pytest
102 passed in 0.57s
```

These are self-written tests. `test_double_execution.py` races 8 threads and 6 separate processes
on one proposal, and kills a process inside its executor with `os._exit`. `test_tampering.py` edits
every column, deletes, reorders and forges rows with the triggers removed, and covers the two chain
limits above. `test_expiry.py` covers deadlines with a fake clock.

## Limits

- Separation is by convention inside one Python process, and by file permissions across
  processes. An agent that can open the SQLite file or import the executor can get around the gate.
  Keep agents outside that boundary, for example behind a small service you write. This repository
  does not include one.
- `--reviewer` is an asserted name, not an authenticated identity. The self-approval check compares
  strings.
- Evidence references are stored, not fetched. Nothing checks that a URI exists or that a sha256
  matches, so the reviewer has to look.
- Without an anchor kept elsewhere, a person with write access to the database can rewrite history
  undetected.
- Each decision and execution verifies the whole chain, so cost grows linearly with the log. This is for a human-paced
  queue, and it has not been measured on large logs.
- SQLite means one host. There is no notification when something needs review.
- Tested locally on Python 3.12 and SQLite on macOS only. CI is configured for 3.11 and 3.12 but
  has not run, since this repository has not been pushed.

## License

MIT
