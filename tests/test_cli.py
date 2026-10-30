from __future__ import annotations

import sys
from pathlib import Path

import pytest

from approval_gate.cli import _build_parser, main, safe
from conftest import Harness

EXAMPLES = str(Path(__file__).parent.parent / "examples")


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "gate.db")


@pytest.fixture
def harness(db):
    h = Harness(db)
    yield h
    h.gate.close()


def run(db, *args):
    return main(["--db", db, *args])


def test_list_show_and_approve_reject(db, harness, capsys):
    first = harness.propose(idempotency_key="one")
    second = harness.propose(idempotency_key="two")

    assert run(db, "list") == 0
    out = capsys.readouterr().out
    assert first.id in out
    assert second.id in out

    assert run(db, "show", first.id) == 0
    out = capsys.readouterr().out
    assert "ticket://1" in out
    assert "synthetic test" in out
    assert '"amount_cents": 100' in out

    assert run(db, "approve", first.id, "--reviewer", "sam", "--yes") == 0
    assert run(db, "reject", second.id, "--reviewer", "sam", "--note", "no", "--yes") == 0
    assert harness.gate.get(first.id).state == "approved"
    assert harness.gate.get(second.id).state == "rejected"

    capsys.readouterr()
    assert run(db, "list") == 0
    assert "nothing to show" in capsys.readouterr().out
    assert run(db, "list", "--all") == 0
    assert "rejected" in capsys.readouterr().out


def test_approve_prompts_and_only_yes_counts(db, harness, monkeypatch, capsys):
    proposal = harness.propose()
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert run(db, "approve", proposal.id, "--reviewer", "sam") == 1
    assert harness.gate.get(proposal.id).state == "pending"
    assert "no decision recorded" in capsys.readouterr().out

    monkeypatch.setattr("builtins.input", lambda _: "yes")
    assert run(db, "approve", proposal.id, "--reviewer", "sam") == 0
    assert harness.gate.get(proposal.id).state == "approved"


def test_self_approval_is_refused_with_exit_1(db, harness, capsys):
    proposal = harness.propose()
    assert run(db, "approve", proposal.id, "--reviewer", "bot", "--yes") == 1
    assert "refused" in capsys.readouterr().err


def test_execute_runs_once_through_the_cli(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "gate.db")
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("APPROVAL_GATE_DEMO_LEDGER", str(ledger))
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(EXAMPLES)
    sys.modules.pop("actions", None)

    from actions import registry

    from approval_gate import Evidence, Gate

    gate = Gate(db, registry)
    proposal = gate.agent("bot").propose(
        "issue_refund",
        {"order_id": "ORD-1", "amount_cents": 5, "currency": "USD"},
        evidence=[Evidence("ticket://SYN")],
        reason="synthetic",
    )
    gate.close()

    spec = ("--registry", "actions:registry")
    assert main(["--db", db, *spec, "approve", proposal.id, "--reviewer", "sam", "--yes"]) == 0
    assert main(["--db", db, *spec, "execute"]) == 0
    assert "executed" in capsys.readouterr().out
    assert main(["--db", db, *spec, "execute"]) == 0
    assert "nothing approved" in capsys.readouterr().out
    assert main(["--db", db, *spec, "execute", proposal.id]) == 1
    assert len(ledger.read_text().splitlines()) == 1
    sys.modules.pop("actions", None)


def test_verify_head_and_anchor(db, harness, capsys):
    harness.propose()
    capsys.readouterr()
    assert run(db, "head") == 0
    anchor = capsys.readouterr().out.strip()
    assert anchor.startswith("1:")
    assert run(db, "verify", "--anchor", anchor) == 0
    assert "ok: 1 events" in capsys.readouterr().out

    harness.raw().execute("UPDATE events SET actor = 'x' WHERE seq = 1")
    assert run(db, "verify") == 1
    assert "TAMPERED" in capsys.readouterr().out
    assert run(db, "list") == 0
    assert "failed verification" in capsys.readouterr().err


def test_bad_anchor_is_a_clean_exit(db):
    with pytest.raises(SystemExit, match="SEQ:HASH"):
        run(db, "verify", "--anchor", "nonsense")


def test_log_prints_every_event(db, harness, capsys):
    harness.approved()
    capsys.readouterr()
    assert run(db, "log") == 0
    out = capsys.readouterr().out
    assert "proposed" in out
    assert "approved" in out


def test_expire_command(db, harness, capsys):
    from datetime import timedelta

    # Propose in the past: the CLI uses the real clock, so this is already due.
    harness.clock.advance(hours=-1)
    harness.propose(ttl=timedelta(minutes=1))
    capsys.readouterr()
    assert run(db, "expire") == 0
    assert "expired 1 proposal" in capsys.readouterr().out


def test_no_command_can_propose():
    parser = _build_parser()
    subcommands = parser._subparsers._group_actions[0].choices
    assert "propose" not in subcommands


def test_terminal_control_sequences_are_escaped():
    assert safe("ok\x1b[2J\rdone") == "ok\\x1b[2J\\x0ddone"
    assert safe("line one\nline two") == "line one\nline two"
    assert safe("Çay 日本語") == "Çay 日本語"


def test_hostile_reason_is_escaped_in_show(db, harness, capsys):
    proposal = harness.propose(reason="fine\x1b[1A\x1b[2Kapproved by security")
    capsys.readouterr()
    assert run(db, "show", proposal.id) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "\\x1b" in out
