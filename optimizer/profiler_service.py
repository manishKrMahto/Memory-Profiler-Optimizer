from __future__ import annotations

import json
import os
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
    memory_usage: List[float]
    peak_memory: Optional[float]
    execution_time: Optional[float]
    profiler_output: str = ""
    error: str = ""


def _python_exec() -> str:
    # Prefer explicit override.
    override = os.environ.get("PYTHON")
    if override:
        return override

    # Prefer project venv interpreter if it exists (common on Windows).
    try:
        repo_root = Path(__file__).resolve().parents[1]
        venv_py = (repo_root / ".venv" / "Scripts" / "python.exe")
        if venv_py.exists():
            return str(venv_py)
    except Exception:
        pass

    # Fallback: whatever is running Django.
    return sys.executable


def profile_function(module_path: str | Path, qualname: str, *, timeout_s: int = DEFAULT_TIMEOUT_S) -> MemoryStats:
    """
    Profile a function by importing its module from disk in a subprocess and calling it with no args.

    Safety constraints:
    - runs in a separate process
    - hard timeout (process killed)
    - best-effort isolation via `python -I`
    """
    module_path = str(Path(module_path).resolve())
    enable_line_profiler = _env_flag("MPO_ENABLE_LINE_PROFILER", default=False)

    runner = r"""
import importlib.util, io, json, os, sys, time
from memory_profiler import LineProfiler, memory_usage

payload = json.loads(sys.stdin.read() or "{}")
module_path = payload["module_path"]
qualname = payload["qualname"]
interval = float(payload.get("interval", 0.05))
timeout_s = payload.get("timeout_s")
enable_line_profiler = bool(payload.get("enable_line_profiler", False))

spec = importlib.util.spec_from_file_location("_mpo_target", module_path)
if spec is None or spec.loader is None:
    raise RuntimeError("Unable to load module spec")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

obj = mod
for part in qualname.split("."):
    obj = getattr(obj, part, None)
    if obj is None:
        raise RuntimeError("Function not resolvable: " + qualname)

if not callable(obj):
    raise RuntimeError("Resolved object not callable: " + qualname)

def _call():
    return obj()

t0 = time.time()
samples = memory_usage(
    (_call, (), {}),
    interval=interval,
    timeout=timeout_s,
    max_usage=False,
    include_children=True,
)
elapsed = time.time() - t0
peak = float(max(samples)) if samples else None

# Best-effort: capture line-by-line memory report.
lp_out = ""
if enable_line_profiler:
    try:
        lp = LineProfiler()
        lp.add_function(obj)
        lp(obj)()
        s = io.StringIO()
        lp.print_stats(stream=s)
        lp_out = s.getvalue()
    except Exception:
        lp_out = ""

print(json.dumps({
  "memory_usage": [float(x) for x in (samples or [])],
  "peak_memory": peak,
  "execution_time": float(elapsed),
  "profiler_output": lp_out,
}))
"""

    payload = {
        "module_path": module_path,
        "qualname": qualname,
        "interval": 0.2,
        "timeout_s": max(1, int(timeout_s)),
        "enable_line_profiler": enable_line_profiler,
    }
    try:
        res = subprocess.run(
            [_python_exec(), "-I", "-c", runner],
            input=json.dumps(payload).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return MemoryStats(memory_usage=[], peak_memory=None, execution_time=None, profiler_output="", error="timeout")

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
            error="",
        )
    except Exception as e:
        return MemoryStats(memory_usage=[], peak_memory=None, execution_time=None, profiler_output="", error=f"parse_error: {e}")


def profile_optimized_function(
    original_module_path: str | Path,
    qualname: str,
    optimized_code: str,
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> MemoryStats:
    """
    Re-profile optimized code safely.

    Strategy:
    - import original module in subprocess
    - exec optimized function code into module globals (so it can reuse imports/helpers)
    - resolve qualname again and call with no args
    """
    original_module_path = str(Path(original_module_path).resolve())
    enable_line_profiler = _env_flag("MPO_ENABLE_LINE_PROFILER", default=False)

    runner = r"""
import importlib.util, io, json, sys, time
from memory_profiler import LineProfiler, memory_usage

payload = json.loads(sys.stdin.read() or "{}")
module_path = payload["module_path"]
qualname = payload["qualname"]
code = payload["code"]
interval = float(payload.get("interval", 0.05))
timeout_s = payload.get("timeout_s")
enable_line_profiler = bool(payload.get("enable_line_profiler", False))

spec = importlib.util.spec_from_file_location("_mpo_target", module_path)
if spec is None or spec.loader is None:
    raise RuntimeError("Unable to load module spec")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Inject optimized function into module namespace.
exec(code, mod.__dict__, mod.__dict__)

obj = mod
for part in qualname.split("."):
    obj = getattr(obj, part, None)
    if obj is None:
        raise RuntimeError("Optimized function not resolvable: " + qualname)
if not callable(obj):
    raise RuntimeError("Resolved optimized object not callable: " + qualname)

def _call():
    return obj()

t0 = time.time()
samples = memory_usage(
    (_call, (), {}),
    interval=interval,
    timeout=timeout_s,
    max_usage=False,
    include_children=True,
)
elapsed = time.time() - t0
peak = float(max(samples)) if samples else None

lp_out = ""
if enable_line_profiler:
    try:
        lp = LineProfiler()
        lp.add_function(obj)
        lp(obj)()
        s = io.StringIO()
        lp.print_stats(stream=s)
        lp_out = s.getvalue()
    except Exception:
        lp_out = ""

print(json.dumps({
  "memory_usage": [float(x) for x in (samples or [])],
  "peak_memory": peak,
  "execution_time": float(elapsed),
  "profiler_output": lp_out,
}))
"""
    payload = {
        "module_path": original_module_path,
        "qualname": qualname,
        "code": optimized_code,
        "interval": 0.2,
        "timeout_s": max(1, int(timeout_s)),
        "enable_line_profiler": enable_line_profiler,
    }
    try:
        res = subprocess.run(
            [_python_exec(), "-I", "-c", runner],
            input=json.dumps(payload).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return MemoryStats(memory_usage=[], peak_memory=None, execution_time=None, profiler_output="", error="timeout")

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
            error="",
        )
    except Exception as e:
        return MemoryStats(memory_usage=[], peak_memory=None, execution_time=None, profiler_output="", error=f"parse_error: {e}")

