"""
conftest.py
───────────
Pytest configuration — adds the project root to sys.path so that
``import config``, ``import core``, ``import services``, etc. all
resolve without installing the package.
"""

import sys
from pathlib import Path

# Insert project root at the front of sys.path
sys.path.insert(0, str(Path(__file__).parent))
