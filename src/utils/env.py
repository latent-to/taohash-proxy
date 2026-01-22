from __future__ import annotations

import os
from typing import Optional

_TRUTHY = {"1", "true", "t", "yes", "y", "on"}
_FALSY = {"0", "false", "f", "no", "n", "off"}


def env_bool(name: str, default: bool = False) -> bool:
    """Return environment variable ``name`` parsed as a boolean.

    Accepts common truthy values (``1, true, yes, on``) and falsy values
    (``0, false, no, off``) case-insensitively. Values outside these sets
    fall back to the provided default.
    """

    raw: Optional[str] = os.environ.get(name)
    if raw is None:
        return default

    normalized = raw.strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    return default


def env_int(name: str, default: int) -> int:
    """Return environment variable ``name`` parsed as an integer.

    If the environment variable is not set, returns the default.
    If the value cannot be parsed as an integer, logs a warning and returns the default.
    """
    raw: Optional[str] = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        from src.utils.logger import get_logger

        get_logger(__name__).warning(
            f"Invalid int for {name}={raw!r}, using default={default}"
        )
        return default


def env_float(name: str, default: float) -> float:
    """Return environment variable ``name`` parsed as a float.

    If the environment variable is not set, returns the default.
    If the value cannot be parsed as a float, logs a warning and returns the default.
    """
    raw: Optional[str] = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        from src.utils.logger import get_logger

        get_logger(__name__).warning(
            f"Invalid float for {name}={raw!r}, using default={default}"
        )
        return default
