"""将项目根目录加入 sys.path，确保可 import jqdata（根目录 jqdata.py）。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_ROOT: Optional[Path] = None


def project_root() -> Path:
    global _ROOT
    if _ROOT is None:
        _ROOT = Path(__file__).resolve().parents[2]
    return _ROOT


def ensure_project_root_on_path() -> Path:
    root = project_root()
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return root
