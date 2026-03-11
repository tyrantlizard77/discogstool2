"""Pytest configuration for discogstool2 tests.

Adds the parent directory (discogstool2) to sys.path so that ``import beatport``,
``import util``, etc. all resolve correctly regardless of where pytest is invoked.
"""

import os
import sys

# Ensure the package root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
