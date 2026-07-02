"""Ensures the repo root is importable so tests can `import mcp_gateway`,
regardless of the working directory or pytest import mode."""

import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
