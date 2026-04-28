from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass(frozen=True)
class JSFunctionMeta:
    function_name: str
    code: str
    start_line: int
    end_line: int
    file_path: str  # repo-relative posix
    can_call_without_args: bool


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _npx_exec() -> Optional[str]:
    # Windows uses npx.cmd
    return shutil.which("npx") or shutil.which("npx.cmd")


def extract_functions(file_path: str | Path, *, repo_root: str | Path | None = None, timeout_s: int = 30) -> List[JSFunctionMeta]:
    """
    Extract function spans from a JS/TS file by calling the TypeScript AST parser tool.
    Returns a list compatible with Python's FunctionMeta fields used by Django UI.
    """
    p = Path(file_path).resolve()
    if not p.exists() or not p.is_file():
        return []

    repo_root_abs = Path(repo_root).resolve() if repo_root is not None else None
    npx = _npx_exec()
    if not npx:
        return []

    tool_root = _repo_root()
    parser_path = tool_root / "backend" / "parser" / "js_ast_parser.ts"

    payload = {"file_path": str(p), "repo_root": str(repo_root_abs) if repo_root_abs else None}
    try:
        res = subprocess.run(
            [npx, "tsx", str(parser_path)],
            input=json.dumps(payload).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(tool_root),
            timeout=timeout_s,
            check=False,
            env={**os.environ},
        )
    except Exception:
        return []

    if res.returncode != 0:
        return []

    try:
        out = json.loads(res.stdout.decode("utf-8", errors="replace") or "{}")
        funcs = out.get("functions") or []
        metas: List[JSFunctionMeta] = []
        for f in funcs:
            metas.append(
                JSFunctionMeta(
                    function_name=str(f.get("function_name") or ""),
                    code=str(f.get("code") or ""),
                    start_line=int(f.get("start_line") or 1),
                    end_line=int(f.get("end_line") or 1),
                    file_path=str(f.get("file_path") or p.name),
                    can_call_without_args=bool(f.get("can_call_without_args")),
                )
            )
        return [m for m in metas if m.function_name and m.code]
    except Exception:
        return []

