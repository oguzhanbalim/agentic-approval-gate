from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pytest

from approval_gate.actions import Registry, SchemaError, load_payload


@dataclass(frozen=True)
class Everything:
    name: str
    count: int
    ratio: float
    active: bool
    mode: Literal["a", "b"]
    maybe: str | None = None
    tags: list[str] = field(default_factory=list)
    extra: dict[str, int] = field(default_factory=dict)


GOOD = {"name": "n", "count": 1, "ratio": 0.5, "active": True, "mode": "a"}


def test_valid_payload_builds_the_dataclass():
    loaded = load_payload(Everything, {**GOOD, "maybe": None, "tags": ["x"], "extra": {"k": 1}})
    assert loaded.tags == ["x"]
    assert loaded.extra == {"k": 1}


def test_int_is_accepted_where_float_is_declared():
    assert load_payload(Everything, {**GOOD, "ratio": 1}).ratio == 1


@pytest.mark.parametrize(
    "change",
    [
        {"name": 1},
        {"count": 1.5},
        {"count": True},
        {"ratio": "0.5"},
        {"ratio": True},
        {"active": 1},
        {"mode": "c"},
        {"maybe": 5},
        {"tags": "x"},
        {"tags": [1]},
        {"extra": []},
        {"extra": {"k": "v"}},
    ],
)
def test_wrong_types_are_refused(change):
    with pytest.raises(SchemaError):
        load_payload(Everything, {**GOOD, **change})


def test_unsupported_field_type_fails_closed():
    @dataclass
    class Odd:
        value: set

    with pytest.raises(SchemaError, match="unsupported"):
        load_payload(Odd, {"value": []})


def test_registry_refuses_duplicates_and_non_dataclasses():
    registry = Registry()

    @registry.action("a", Everything)
    def run(payload, context):
        return None

    with pytest.raises(ValueError, match="already"):
        registry.action("a", Everything)(run)
    with pytest.raises(TypeError):
        registry.action("b", dict)
    assert registry.names() == ["a"]
