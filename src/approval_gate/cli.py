"""The reviewer's command line. There is deliberately no command that proposes."""

from __future__ import annotations

import argparse
import getpass
import importlib
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

from approval_gate.actions import Registry
from approval_gate.gate import Gate, GateError, Proposal
from approval_gate.log import TamperedLog


def safe(value: Any) -> str:
    """Make agent-chosen text safe for a terminal.

    Control characters are escaped, so ANSI sequences or carriage returns in a payload cannot
    repaint what the reviewer reads.
    """
    text = str(value)
    return "".join(
        char if char == "\n" or (char.isprintable()) else f"\\x{ord(char):02x}" for char in text
    )


def _load_registry(spec: str | None) -> Registry:
    if not spec:
        return Registry()
    module_name, _, attribute = spec.partition(":")
    if not attribute:
        raise SystemExit(f"error: expected module:attribute, got {spec!r}")
    sys.path.insert(0, os.getcwd())
    try:
        registry = getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError) as exc:
        raise SystemExit(f"error: cannot load registry {spec!r}: {exc}") from exc
    if not isinstance(registry, Registry):
        raise SystemExit(f"error: {spec!r} is not a Registry")
    return registry


def _print_list(proposals: list[Proposal]) -> None:
    if not proposals:
        print("nothing to show")
        return
    for p in proposals:
        print(
            f"{p.id}  {p.state:9}  {safe(p.action):16}  by {safe(p.proposer):12}  "
            f"expires {p.expires_at.isoformat(timespec='minutes')}"
        )


def _print_detail(p: Proposal) -> None:
    print(f"id:        {p.id}")
    print(f"state:     {p.state}")
    print(f"action:    {safe(p.action)}")
    print(f"proposer:  {safe(p.proposer)}")
    print(f"reason:    {safe(p.reason)}")
    print(f"expires:   {p.expires_at.isoformat(timespec='seconds')}")
    print(f"payload:   {json.dumps(p.payload, sort_keys=True, ensure_ascii=True)}")
    print(f"sha256:    {p.payload_sha256}")
    print("evidence:")
    for item in p.evidence:
        note = f" ({safe(item.note)})" if item.note else ""
        digest = f" sha256={item.sha256}" if item.sha256 else ""
        print(f"  - {safe(item.uri)}{note}{digest}")
    if p.decided_by:
        note = f": {safe(p.note)}" if p.note else ""
        print(
            f"decided:   {p.state if p.state in ('approved', 'rejected') else 'approved'} "
            f"by {safe(p.decided_by)}{note}"
        )
    if p.state == "in_doubt":
        print("note:      execution started but no outcome was recorded. It may have run.")
        print("           Check the target system, then record what you found with `resolve`.")
    if p.result is not None:
        print(f"result:    {json.dumps(p.result, sort_keys=True, ensure_ascii=True)}")
    if p.error:
        print(f"error:     {safe(p.error)}")


def _print_outcome(p: Proposal) -> None:
    detail = p.error if p.state == "failed" else json.dumps(p.result, ensure_ascii=True)
    print(f"{p.id}  {p.state}  {safe(detail)}")


def _confirm(p: Proposal, verb: str, assume_yes: bool) -> bool:
    _print_detail(p)
    if assume_yes:
        return True
    answer = input(f"\n{verb} this proposal? Type 'yes': ")
    return answer.strip().lower() == "yes"


def _parse_anchor(value: str | None) -> tuple[int, str] | None:
    if value is None:
        return None
    seq, separator, digest = value.partition(":")
    if not separator or not seq.isdigit() or not digest:
        raise SystemExit("error: --anchor expects SEQ:HASH, as printed by `head`")
    return int(seq), digest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="approval-gate", description=__doc__)
    parser.add_argument("--db", required=True, help="SQLite file")
    parser.add_argument("--registry", help="module:attribute of the action Registry")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="show pending proposals")
    listing.add_argument("--all", action="store_true", help="show every state")

    show = sub.add_parser("show", help="show one proposal in full")
    show.add_argument("id")

    for name, text in (("approve", "approve a proposal"), ("reject", "reject a proposal")):
        decide = sub.add_parser(name, help=text)
        decide.add_argument("id")
        decide.add_argument("--reviewer", default=None, help="default: the OS user name")
        decide.add_argument("--note")
        decide.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    execute = sub.add_parser("execute", help="run approved proposals; needs --registry")
    execute.add_argument("id", nargs="?", help="default: everything approved")

    resolve = sub.add_parser("resolve", help="record the outcome of an in-doubt execution")
    resolve.add_argument("id")
    resolve.add_argument("--outcome", required=True, choices=["succeeded", "failed"])
    resolve.add_argument("--note", required=True, help="what you checked")
    resolve.add_argument("--reviewer", default=None)

    sub.add_parser("expire", help="write expiry events for anything past its deadline")

    verify = sub.add_parser("verify", help="check the hash chain and every lifecycle")
    verify.add_argument("--anchor", help="SEQ:HASH recorded earlier by `head`")

    sub.add_parser("head", help="print the current head, to store somewhere else")
    sub.add_parser("log", help="print every event")
    return parser


def _dispatch(args: argparse.Namespace, gate: Gate) -> int:
    reviewer = getattr(args, "reviewer", None) or getpass.getuser()
    if args.command == "list":
        result = gate.verify()
        if not result.ok:
            print(f"warning: the log failed verification: {result.problem}", file=sys.stderr)
        _print_list(gate.list(None if args.all else "pending"))
    elif args.command == "show":
        _print_detail(gate.get(args.id))
    elif args.command in ("approve", "reject"):
        verb = args.command.capitalize()
        if not _confirm(gate.get(args.id), verb, args.yes):
            print("no decision recorded")
            return 1
        decided = getattr(gate, args.command)(args.id, reviewer, args.note)
        print(f"{decided.id} {decided.state} by {safe(reviewer)}")
    elif args.command == "execute":
        finished = [gate.execute(args.id)] if args.id else gate.execute_approved()
        if not finished:
            print("nothing approved to run")
        for proposal in finished:
            _print_outcome(proposal)
    elif args.command == "resolve":
        resolved = gate.resolve(
            args.id, reviewer, succeeded=args.outcome == "succeeded", note=args.note
        )
        print(f"{resolved.id} {resolved.state}, resolved by {safe(reviewer)}")
    elif args.command == "expire":
        print(f"expired {len(gate.expire_due())} proposal(s)")
    elif args.command == "verify":
        result = gate.verify(_parse_anchor(args.anchor))
        if not result.ok:
            print(f"TAMPERED: {result.problem}")
            return 1
        print(f"ok: {result.events} events, head {result.head_seq}:{result.head_hash}")
    elif args.command == "head":
        seq, digest = gate.log.head()
        print(f"{seq}:{digest}")
    elif args.command == "log":
        for event in gate.log.events():
            print(
                f"{event.seq:>4} {event.hash[:12]} {event.at[:19]} {event.proposal_id} "
                f"{event.kind:18} {safe(event.actor)}"
            )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    gate = Gate(args.db, _load_registry(args.registry))
    try:
        return _dispatch(args, gate)
    except (GateError, TamperedLog, ValueError) as exc:
        print(f"refused: {safe(exc)}", file=sys.stderr)
        return 1
    finally:
        gate.close()


if __name__ == "__main__":
    raise SystemExit(main())
