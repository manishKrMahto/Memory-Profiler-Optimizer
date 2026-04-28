from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_TIMEOUT_S = 90


def _env_flag(name: str, default: bool = False) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        return default
    return v in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class MemoryStats:
    memory_usage: List[float]  # MB samples
    peak_memory: Optional[float]  # MB
    execution_time: Optional[float]  # seconds
    profiler_output: str = ""
    error: str = ""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _node_exec() -> Optional[str]:
    return shutil.which("node") or shutil.which("node.exe") or os.environ.get("NODE")


def _npx_exec() -> Optional[str]:
    return shutil.which("npx") or shutil.which("npx.cmd")


def _tsx_cli_path(repo_root: Path) -> Optional[Path]:
    # Prefer local tsx install for speed and to allow `node --expose-gc`.
    p = repo_root / "node_modules" / "tsx" / "dist" / "cli.mjs"
    return p if p.exists() else None


def _run_tsx(args: List[str], *, input_json: Dict[str, Any], timeout_s: int, expose_gc: bool) -> subprocess.CompletedProcess[bytes]:
    root = _repo_root()
    node = _node_exec()
    tsx_cli = _tsx_cli_path(root)

    payload = json.dumps(input_json).encode("utf-8")

    if node and tsx_cli:
        cmd = [node]
        if expose_gc:
            cmd.append("--expose-gc")
        cmd += [str(tsx_cli), *args]
        return subprocess.run(
            cmd,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(root),
            timeout=timeout_s,
            check=False,
            env={**os.environ},
        )

    # Fallback: npx tsx (may be slower)
    npx = _npx_exec()
    if not npx:
        raise RuntimeError("Node tooling not found (missing node/npx). Install Node.js and run npm install.")

    cmd = [npx, "tsx", *args]
    return subprocess.run(
        cmd,
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(root),
        timeout=timeout_s,
        check=False,
        env={**os.environ},
    )


def profile_function_code(code: str, qualname: str, *, timeout_s: int = DEFAULT_TIMEOUT_S, language: str | None = None) -> MemoryStats:
    """
    Profile a JS/TS function by running the Node profiler tool in a subprocess and calling it with no args.
    """
    root = _repo_root()
    tool = root / "backend" / "profiler" / "node_profiler.ts"

    expose_gc = _env_flag("MPO_NODE_EXPOSE_GC", default=True)
    trigger_gc = _env_flag("MPO_NODE_TRIGGER_GC", default=True)

    payload: Dict[str, Any] = {
        "code": code or "",
        "qualname": qualname,
        "timeout_s": max(1, int(timeout_s)),
        "trigger_gc": bool(trigger_gc),
        "sample_interval_ms": int(os.environ.get("MPO_NODE_SAMPLE_INTERVAL_MS", "25") or "25"),
    }
    if language:
        payload["language"] = str(language).lower()

    try:
        res = _run_tsx([str(tool)], input_json=payload, timeout_s=timeout_s, expose_gc=expose_gc)
    except subprocess.TimeoutExpired:
        return MemoryStats(memory_usage=[], peak_memory=None, execution_time=None, profiler_output="", error="timeout")
    except Exception as e:
        return MemoryStats(memory_usage=[], peak_memory=None, execution_time=None, profiler_output="", error=str(e))

    if res.returncode != 0:
        err = res.stderr.decode("utf-8", errors="replace")[-2000:]
        return MemoryStats(memory_usage=[], peak_memory=None, execution_time=None, profiler_output="", error=err)

    try:
        out = json.loads(res.stdout.decode("utf-8", errors="replace") or "{}")
        mem = out.get("memory_usage") or []
        return MemoryStats(
            memory_usage=[float(x) for x in mem],
            peak_memory=float(out["peak_memory"]) if out.get("peak_memory") is not None else None,
            execution_time=float(out["execution_time"]) if out.get("execution_time") is not None else None,
            profiler_output=str(out.get("profiler_output") or ""),
            error=str(out.get("error") or ""),
        )
    except Exception as e:
        return MemoryStats(memory_usage=[], peak_memory=None, execution_time=None, profiler_output="", error=f"parse_error: {e}")

