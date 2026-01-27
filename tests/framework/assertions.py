from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from difflib import unified_diff
from typing import Any, Iterable


def _to_jsonable(obj: Any) -> Any:
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


def pretty(obj: Any) -> str:
    return json.dumps(_to_jsonable(obj), sort_keys=True, indent=2, default=str)


def assert_equal(actual: Any, expected: Any, *, message: str = "") -> None:
    """Assert equality with a structured diff."""
    if actual == expected:
        return

    actual_s = pretty(actual)
    expected_s = pretty(expected)
    diff = "\n".join(
        unified_diff(
            expected_s.splitlines(),
            actual_s.splitlines(),
            fromfile="expected",
            tofile="actual",
            lineterm="",
        )
    )
    prefix = (message + "\n") if message else ""
    raise AssertionError(prefix + diff)


def assert_contains(haystack: str, needle: str, *, message: str = "") -> None:
    if needle in haystack:
        return
    raise AssertionError(message or f"Expected to find {needle!r} in {haystack!r}")


def assert_set_equal(
    actual: Iterable[Any], expected: Iterable[Any], *, message: str = ""
) -> None:
    actual_set = set(actual)
    expected_set = set(expected)
    if actual_set == expected_set:
        return
    raise AssertionError(message or f"Expected set {expected_set!r} but got {actual_set!r}")
