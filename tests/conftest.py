"""Pytest configuration.

The scenario framework avoids external dependencies by using in-memory mocks.
"""

import os


def pytest_configure():
    os.environ.setdefault("ORDER_ALLOCATOR_MINER_COOLDOWN_SECONDS", "600")
    os.environ.setdefault("ORDER_ALLOCATOR_MINER_LAST_MOVE_TTL_SECONDS", "3600")
