from __future__ import annotations

from pathlib import Path
from typing import Literal

Language = Literal["python", "node"]


def detect_language(file_path: str | Path) -> Language:
    """
    Route language by file extension.
    - .py -> python
    - .js/.ts/.jsx/.tsx -> node
    """
    ext = Path(str(file_path)).suffix.lower()
    if ext == ".py":
        return "python"
    if ext in {".js", ".ts", ".jsx", ".tsx"}:
        return "node"
    # Default: keep existing behavior conservative (Python-only) unless explicitly supported.
    return "python"


def is_supported_ingest(filename: str) -> bool:
    ext = Path(filename).suffix.lower()
    return ext in {".py", ".js", ".ts", ".jsx", ".tsx"}

