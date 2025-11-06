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
