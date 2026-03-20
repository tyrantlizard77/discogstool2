"""Pytest configuration for discogstool2 tests.

Adds the parent directory (discogstool2) to sys.path so that ``import beatport``,
``import util``, etc. all resolve correctly regardless of where pytest is invoked.
"""

import os
import sys

import pytest

# Ensure the package root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: live network tests requiring real API credentials "
        "(run with: pytest -m integration)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip integration tests unless -m integration (or similar) is specified."""
    if config.option.markexpr and "integration" in config.option.markexpr:
        return
    skip_integration = pytest.mark.skip(reason="use -m integration to run live tests")
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(skip_integration)
