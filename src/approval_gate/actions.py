"""Typed actions: a dataclass for the payload and a registered function that runs it.

A payload arrives from an agent as untrusted JSON. ``load_payload`` checks it strictly against
the dataclass: no missing fields, no extra fields, no coercion. ``amount_cents: int`` does not
accept ``"100"``, ``100.0`` or ``True``. Put business rules in ``__post_init__`` and raise
``ValueError``.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class SchemaError(ValueError):
    """A payload does not match its action type."""


def _check(where: str, value: Any, expected: Any) -> None:
    origin = typing.get_origin(expected)
    if expected is Any:
        return
    if expected is None or expected is type(None):
        if value is not None:
            raise SchemaError(f"{where}: expected null")
        return
    if origin is typing.Literal:
        options = typing.get_args(expected)
        if not any(type(value) is type(option) and value == option for option in options):
            raise SchemaError(f"{where}: expected one of {list(options)}")
        return
    if origin is typing.Union or origin is types.UnionType:
        for option in typing.get_args(expected):
            try:
                _check(where, value, option)
                return
            except SchemaError:
                continue
        raise SchemaError(f"{where}: {value!r} does not match {expected}")
    if origin is list:
        if not isinstance(value, list):
            raise SchemaError(f"{where}: expected a list")
        item_type = typing.get_args(expected)[0]
        for index, item in enumerate(value):
            _check(f"{where}[{index}]", item, item_type)
        return
    if origin is dict:
        if not isinstance(value, dict):
            raise SchemaError(f"{where}: expected an object")
        value_type = typing.get_args(expected)[1]
        for key, item in value.items():
            _check(f"{where}.{key}", item, value_type)
        return
    if expected in (str, bool):
        if type(value) is not expected:
            raise SchemaError(f"{where}: expected {expected.__name__}")
        return
    if expected is int:
        if type(value) is not int:
            raise SchemaError(f"{where}: expected int")
        return
    if expected is float:
        if type(value) not in (int, float):
            raise SchemaError(f"{where}: expected a number")
        return
    raise SchemaError(f"{where}: unsupported field type {expected}")


def load_payload(payload_type: type, raw: Any) -> Any:
    """Build a payload dataclass from JSON data, or raise SchemaError."""
    if not isinstance(raw, dict):
        raise SchemaError("payload must be an object")
    hints = typing.get_type_hints(payload_type)
    fields = {field.name: field for field in dataclasses.fields(payload_type)}
    unknown = sorted(set(raw) - set(fields))
    if unknown:
        raise SchemaError(f"unknown fields: {unknown}")
    for name, field in fields.items():
        if name in raw:
            _check(name, raw[name], hints[name])
        elif field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:
            raise SchemaError(f"missing field: {name}")
    try:
        return payload_type(**raw)
    except (ValueError, TypeError) as exc:
        raise SchemaError(str(exc)) from exc


@dataclass(frozen=True)
class Action:
    name: str
    payload_type: type
    executor: Callable[..., Any]
    min_evidence: int = 1


class Registry:
    """Maps action names to payload types and executors. Unregistered actions cannot be proposed."""

    def __init__(self) -> None:
        self._actions: dict[str, Action] = {}

    def action(
        self, name: str, payload_type: type, *, min_evidence: int = 1
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorate ``def run(payload, context) -> result`` to register it as an executor."""
        if not dataclasses.is_dataclass(payload_type):
            raise TypeError("payload_type must be a dataclass")

        def register(executor: Callable[..., Any]) -> Callable[..., Any]:
            if name in self._actions:
                raise ValueError(f"action {name!r} is already registered")
            self._actions[name] = Action(name, payload_type, executor, min_evidence)
            return executor

        return register

    def get(self, name: str) -> Action | None:
        return self._actions.get(name)

    def names(self) -> list[str]:
        return sorted(self._actions)
