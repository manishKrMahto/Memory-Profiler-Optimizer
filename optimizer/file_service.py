from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List


DEFAULT_EXCLUDED_DIRS = {
    ".venv",
    "venv",
    "__pycache__",
    ".git",
    "node_modules",
    "build",
    "dist",
}


def _is_hidden_dir(name: str) -> bool:
    # Cross-platform: treat dot-prefixed dirs as hidden (Windows "hidden" attribute is not checked here).
    return name.startswith(".")


def get_python_files(repo_path: str | Path, excluded_dirs: Iterable[str] = DEFAULT_EXCLUDED_DIRS) -> List[str]:
    """
    Recursively scan a repo and return repo-relative `.py` files.

    Rules:
    - include ONLY `.py` files
    - exclude common virtualenv/cache/build dirs
    - exclude hidden folders (dot-prefixed)
    """
    root = Path(repo_path).resolve()
    if not root.exists() or not root.is_dir():
        return []

    excluded = set(excluded_dirs)
    out: List[str] = []

    for current_root, dirnames, filenames in os.walk(root):
        # prune dirs in-place
        dirnames[:] = [
            d
            for d in dirnames
            if (d not in excluded)
            and (not _is_hidden_dir(d))
            and (d not in {"__pycache__"})
        ]

        cur = Path(current_root)
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            p = cur / fn
            try:
                rel = p.relative_to(root).as_posix()
            except Exception:
                continue
            out.append(rel)

    return sorted(set(out), key=str.lower)


@dataclass(frozen=True)
class RepoPathInfo:
    repo_root: Path
    abs_file: Path
    rel_path: str


def resolve_repo_file(repo_root: str | Path, rel_path: str) -> RepoPathInfo:
    root = Path(repo_root).resolve()
    rel = Path(rel_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("Invalid file path")
    abs_file = (root / rel).resolve()
    if root not in abs_file.parents and abs_file != root:
        raise ValueError("Invalid file path")
    return RepoPathInfo(repo_root=root, abs_file=abs_file, rel_path=rel.as_posix())

