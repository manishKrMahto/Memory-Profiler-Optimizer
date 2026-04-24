from __future__ import annotations

import ast
import contextlib
import dataclasses
import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, TypedDict
from urllib.parse import urlparse

from django.conf import settings
from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from git import Repo
from langgraph.graph import END, START, StateGraph
from memory_profiler import LineProfiler, memory_usage

try:
    from openai import OpenAI  # type: ignore
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


REPO_STORE_DIRNAME = "repo_store"

# Conservative defaults to avoid accidental resource exhaustion.
MAX_ZIP_BYTES = 200 * 1024 * 1024  # 200 MB
MAX_TOTAL_UNZIPPED_BYTES = 600 * 1024 * 1024  # 600 MB
MAX_FILES = 50_000
MAX_FILE_BYTES_TO_READ = 2 * 1024 * 1024  # 2 MB per file in UI
MAX_SINGLE_FILE_BYTES = 5 * 1024 * 1024  # 5 MB

GIT_CLONE_TIMEOUT_S = 120

# Pipeline defaults
DEFAULT_FILE_SCAN_THRESHOLD_MB = 60.0
DEFAULT_FUNCTION_INCREMENT_THRESHOLD_MB = 1.0
DEFAULT_BATCH_SIZE_FILES = 10
DEFAULT_BATCH_SIZE_FUNCTIONS = 8
DEFAULT_LLM_BATCH_SIZE = 5

# Hard execution limits (best-effort; not a sandbox)
FILE_SCAN_TIMEOUT_S = 20
FUNC_PROFILE_TIMEOUT_S = 15

DEFAULT_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
    ".venv",
    "venv",
    ".idea",
    ".vscode",
}

_RUN_THREADS: Dict[str, threading.Thread] = {}


def _repo_store_root() -> Path:
    base = Path(settings.BASE_DIR)
    root = base / REPO_STORE_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _new_repo_id() -> str:
    return uuid.uuid4().hex


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ensure_db() -> None:
    """
    Create tables if they don't exist.
    We use raw SQL so we don't need migrations/models (as requested).
    """
    with connection.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS optimization_run (
              id TEXT PRIMARY KEY,
              created_at_ms INTEGER NOT NULL,
              updated_at_ms INTEGER NOT NULL,
              status TEXT NOT NULL,
              repo_url TEXT,
              repo_id TEXT,
              repo_path TEXT,
              config_json TEXT NOT NULL,
              error TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS function_change (
              id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL,
              file_path TEXT NOT NULL,
              function_qualname TEXT NOT NULL,
              start_line INTEGER,
              end_line INTEGER,
              old_code TEXT NOT NULL,
              new_code TEXT NOT NULL,
              before_peak_mb REAL,
              before_increment_mb REAL,
              after_peak_mb REAL,
              after_increment_mb REAL,
              improvement_mb REAL,
              improvement_pct REAL,
              explanation TEXT,
              status TEXT NOT NULL,
              created_at_ms INTEGER NOT NULL,
              updated_at_ms INTEGER NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS file_scan_result (
              id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL,
              file_path TEXT NOT NULL,
              peak_mb REAL,
              size_bytes INTEGER,
              signals_json TEXT NOT NULL,
              selected INTEGER NOT NULL,
              created_at_ms INTEGER NOT NULL
            )
            """
        )


def _db_exec(sql: str, params: Tuple[Any, ...] = ()) -> None:
    _ensure_db()
    with connection.cursor() as cur:
        # IMPORTANT: Django cursors expect "%s" placeholders, even on SQLite.
        # Using "?" can break DEBUG query interpolation.
        cur.execute(sql.replace("?", "%s"), params)


def _db_all(sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    _ensure_db()
    with connection.cursor() as cur:
        cur.execute(sql.replace("?", "%s"), params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _normalize_github_url(raw: str) -> str:
    """
    Accepts common GitHub HTTPS URL forms and normalizes them.
    Only allows github.com URLs in Phase 1.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Missing repo URL")

    # Allow users to paste without scheme.
    if raw.startswith("github.com/"):
        raw = "https://" + raw

    u = urlparse(raw)
    if u.scheme not in ("http", "https"):
        raise ValueError("Repo URL must start with http(s)://")
    host = (u.netloc or "").lower()
    if host != "github.com":
        raise ValueError("Only github.com repos are supported in Phase 1")
    path = (u.path or "").strip("/")
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("GitHub URL must look like https://github.com/<owner>/<repo>")

    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        raise ValueError("Invalid GitHub repo URL")

    return f"https://github.com/{owner}/{repo}.git"


def _safe_join(root: Path, relative_path: str) -> Path:
    """
    Resolve a user-provided relative path under root, preventing traversal.
    """
    rel = Path(relative_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("Invalid path")
    resolved = (root / rel).resolve()
    if root.resolve() not in resolved.parents and resolved != root.resolve():
        raise ValueError("Invalid path")
    return resolved


def _is_binary_bytes(sample: bytes) -> bool:
    # Common quick heuristic: NUL byte strongly indicates binary.
    return b"\x00" in sample


def _read_text_file(path: Path, max_bytes: int) -> Tuple[str, bool]:
    """
    Returns (content, truncated).
    Raises ValueError for binary/unreadable files.
    """
    with path.open("rb") as f:
        raw = f.read(max_bytes + 1)
    if _is_binary_bytes(raw[:4096]):
        raise ValueError("Binary file")
    truncated = len(raw) > max_bytes
    raw = raw[:max_bytes]
    try:
        return raw.decode("utf-8"), truncated
    except UnicodeDecodeError:
        # Fallback for mixed encodings
        return raw.decode("utf-8", errors="replace"), truncated


def _iter_python_files(repo_root: Path) -> List[Path]:
    files: List[Path] = []
    for p in repo_root.rglob("*.py"):
        # skip ignored dirs
        parts = set(p.parts)
        if any(d in parts for d in DEFAULT_IGNORED_DIRS):
            continue
        if p.is_file():
            files.append(p)
    return sorted(files, key=lambda x: x.as_posix().lower())


def _chunked(items: List[Any], batch_size: int) -> List[List[Any]]:
    if batch_size <= 0:
        return [items]
    return [items[i : i + batch_size] for i in range(0, len(items), batch_size)]


def _file_signals(text: str) -> Dict[str, Any]:
    """
    Heuristic signals for quick filter; we still measure peak MB too.
    """
    patterns = {
        "pandas": r"\bimport\s+pandas\b|\bfrom\s+pandas\b",
        "numpy": r"\bimport\s+numpy\b|\bfrom\s+numpy\b",
        "torch": r"\bimport\s+torch\b|\bfrom\s+torch\b",
        "tensorflow": r"\bimport\s+tensorflow\b|\bfrom\s+tensorflow\b",
        "json_loads": r"\bjson\.loads\b|\bjson\.load\b",
        "pickle": r"\bpickle\.load\b|\bpickle\.loads\b",
        "read_csv": r"\bread_csv\b",
        "read_parquet": r"\bread_parquet\b",
        "large_range": r"\brange\(\s*\d{6,}\s*\)",
        "list_append_loop": r"for\s+.+:\s*\n\s*.+\.append\(",
    }
    hits = {k: bool(re.search(v, text)) for k, v in patterns.items()}
    score = sum(1 for v in hits.values() if v)
    return {"hits": hits, "score": score}


def _measure_peak_mb_for_file(file_path: Path, timeout_s: int) -> Tuple[Optional[float], Optional[str]]:
    """
    Best-effort peak memory for running a file as a script.
    Uses memory_profiler.memory_usage sampling the child PID.
    """
    cmd = [
        os.environ.get("PYTHON", "python"),
        "-c",
        "import runpy,sys; runpy.run_path(sys.argv[1], run_name='__main__')",
        str(file_path),
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return None, f"spawn_failed: {e}"

    try:
        samples = memory_usage(
            proc=proc.pid,
            interval=0.1,
            timeout=timeout_s,
            max_usage=False,
            include_children=True,
        )
        # If the process is still running after timeout, terminate.
        if proc.poll() is None:
            with contextlib.suppress(Exception):
                proc.terminate()
        peak = float(max(samples)) if samples else None
        return peak, None
    except Exception as e:
        with contextlib.suppress(Exception):
            proc.terminate()
        return None, f"profile_failed: {e}"


@dataclass(frozen=True)
class FunctionInfo:
    file_rel: str
    qualname: str
    start_line: int
    end_line: int
    code: str
    can_call_without_args: bool


def _extract_functions(repo_root: Path, file_rel: str) -> List[FunctionInfo]:
    abs_path = (repo_root / file_rel).resolve()
    if not abs_path.exists() or not abs_path.is_file():
        return []
    src = abs_path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []

    lines = src.splitlines(keepends=True)
    out: List[FunctionInfo] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: List[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> Any:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
            self._handle_fn(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
            self._handle_fn(node)

        def _handle_fn(self, node: Any) -> None:
            name = node.name
            qual = ".".join(self.stack + [name]) if self.stack else name
            start = int(getattr(node, "lineno", 1))
            end = int(getattr(node, "end_lineno", start))
            code = "".join(lines[start - 1 : end])

            # Determine if callable without args
            args = node.args  # type: ignore
            pos_args = list(getattr(args, "posonlyargs", [])) + list(getattr(args, "args", []))
            defaults = list(getattr(args, "defaults", []))
            kwonlyargs = list(getattr(args, "kwonlyargs", []))
            kw_defaults = list(getattr(args, "kw_defaults", []))

            # Strip implicit self/cls for methods
            decorators = [getattr(d, "id", "") for d in getattr(node, "decorator_list", []) if isinstance(d, ast.Name)]
            is_static = "staticmethod" in decorators
            is_classmethod = "classmethod" in decorators
            if self.stack and pos_args:
                if is_static:
                    pass
                else:
                    # drop first arg (self/cls)
                    pos_args = pos_args[1:]

            required_pos = max(0, len(pos_args) - len(defaults))
            required_kwonly = sum(1 for d in kw_defaults if d is None)
            can_call = required_pos == 0 and required_kwonly == 0

            out.append(
                FunctionInfo(
                    file_rel=file_rel,
                    qualname=qual,
                    start_line=start,
                    end_line=end,
                    code=code,
                    can_call_without_args=can_call,
                )
            )

    Visitor().visit(tree)
    return out


def _profile_function_code(code: str, qualname: str, timeout_s: int) -> Tuple[Optional[float], Optional[float], str]:
    """
    Profiles a function definition by executing it in a subprocess with LineProfiler.
    Only intended for functions callable with no args.
    Returns (peak_mb, increment_mb, report_text).
    """
    runner = r"""
import json, sys, time, io
from memory_profiler import LineProfiler, memory_usage

payload = json.loads(sys.stdin.read() or "{}")
code = payload["code"]
qualname = payload["qualname"]

lp = LineProfiler()
glb = {}
loc = {}
exec(code, glb, loc)

obj = loc
for part in qualname.split("."):
    if isinstance(obj, dict) and part in obj:
        obj = obj[part]
    else:
        obj = getattr(obj, part, None)
    if obj is None:
        raise RuntimeError("Function not resolvable")
func = obj
if not callable(func):
    raise RuntimeError("Resolved object not callable")

lp.add_function(func)
before = memory_usage(-1, interval=0.05, timeout=1, max_usage=True)
t0 = time.time()
lp(func)()
after = memory_usage(-1, interval=0.05, timeout=1, max_usage=True)

s = io.StringIO()
lp.print_stats(stream=s)
report = s.getvalue()

out = {
  "peak_mb": float(after) if after is not None else None,
  "increment_mb": (float(after) - float(before)) if (after is not None and before is not None) else None,
  "report": report,
  "elapsed_s": time.time() - t0,
}
print(json.dumps(out))
"""
    inp = json.dumps({"code": code, "qualname": qualname}).encode("utf-8")
    try:
        res = subprocess.run(
            [os.environ.get("PYTHON", "python"), "-c", runner],
            input=inp,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError("Function profiling timed out")

    if res.returncode != 0:
        raise RuntimeError(res.stderr.decode("utf-8", errors="replace")[:2000])

    out = json.loads(res.stdout.decode("utf-8", errors="replace") or "{}")
    return out.get("peak_mb"), out.get("increment_mb"), out.get("report", "")


def _llm_optimize_batch(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Batch LLM optimization. If no key/client, returns no-op optimizations.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if OpenAI is None or not api_key:
        out = []
        for it in items:
            out.append(
                {
                    "optimized_code": it["code"],
                    "explanation": "LLM not configured (missing OPENAI_API_KEY). Returned original code.",
                }
            )
        return out

    client = OpenAI(api_key=api_key)
    system = (
        "You are a code optimizer focused strictly on reducing memory usage. "
        "Do not rename functions, do not change parameters, and keep return behavior compatible."
    )
    user = {
        "task": "Optimize the following Python functions for memory usage only.",
        "rules": [
            "Do NOT change function name/signature.",
            "Do NOT add new dependencies.",
            "Prefer generators/iterators, avoid materializing large lists.",
            "If no safe improvement exists, return the original code.",
            "Return strict JSON array with objects: optimized_code, explanation.",
        ],
        "items": [
            {
                "function_qualname": it["qualname"],
                "code": it["code"],
                "memory_report": it.get("memory_report", ""),
            }
            for it in items
        ],
    }
    resp = client.responses.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
        input=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user)},
        ],
        temperature=0.2,
    )
    text = resp.output_text
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass

    # Fallback if model didn't comply
    out = []
    for it in items:
        out.append({"optimized_code": it["code"], "explanation": "LLM response was not valid JSON; returned original."})
    return out


def _apply_function_replacement(file_text: str, start_line: int, end_line: int, new_code: str) -> str:
    lines = file_text.splitlines(keepends=True)
    # Replace exact line span
    return "".join(lines[: start_line - 1] + [new_code if new_code.endswith("\n") else new_code + "\n"] + lines[end_line:])


def _diff_text(old: str, new: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )

@dataclass(frozen=True)
class TreeNode:
    name: str
    path: str
    type: str  # "file" | "dir"
    children: Optional[List["TreeNode"]] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"name": self.name, "path": self.path, "type": self.type}
        if self.children is not None:
            data["children"] = [c.to_dict() for c in self.children]
        return data


def _should_ignore_dir(name: str) -> bool:
    return name in DEFAULT_IGNORED_DIRS


def _build_tree(root: Path) -> Tuple[TreeNode, Dict[str, Any]]:
    """
    Build a nested tree from root. Returns (tree, stats).
    """
    file_count = 0
    total_bytes = 0

    def walk_dir(current: Path, rel_prefix: str) -> List[TreeNode]:
        nonlocal file_count, total_bytes

        entries: List[TreeNode] = []
        try:
            dir_entries = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return entries

        for p in dir_entries:
            if p.is_dir():
                if _should_ignore_dir(p.name):
                    continue
                child_rel = f"{rel_prefix}{p.name}/"
                children = walk_dir(p, child_rel)
                entries.append(TreeNode(name=p.name, path=child_rel, type="dir", children=children))
            else:
                file_count += 1
                try:
                    total_bytes += p.stat().st_size
                except OSError:
                    pass
                child_rel = f"{rel_prefix}{p.name}"
                entries.append(TreeNode(name=p.name, path=child_rel, type="file"))

            if file_count > MAX_FILES:
                raise ValueError("Repo too large (file count limit exceeded)")

        return entries

    children = walk_dir(root, "")
    tree = TreeNode(name=root.name, path="", type="dir", children=children)
    stats = {"files": file_count, "bytes": total_bytes}
    return tree, stats


def _validate_and_extract_zip(zip_path: Path, dest_dir: Path) -> Dict[str, Any]:
    """
    Extract zip_path into dest_dir safely. Returns metadata.
    """
    total_unzipped = 0
    extracted_files = 0

    with zipfile.ZipFile(zip_path) as zf:
        # Prevent zip bombs: validate total uncompressed size and paths.
        infos = zf.infolist()
        for info in infos:
            # directory entries have trailing slash
            filename = info.filename.replace("\\", "/")
            if filename.startswith("/") or filename.startswith("../") or "/../" in filename:
                raise ValueError("Zip contains unsafe paths")

            if info.is_dir():
                continue

            total_unzipped += int(info.file_size)
            if total_unzipped > MAX_TOTAL_UNZIPPED_BYTES:
                raise ValueError("Zip too large when extracted")

            extracted_files += 1
            if extracted_files > MAX_FILES:
                raise ValueError("Zip contains too many files")

        for info in infos:
            filename = info.filename.replace("\\", "/")
            if not filename or filename.endswith("/"):
                continue
            target = (dest_dir / filename).resolve()
            if dest_dir.resolve() not in target.parents:
                raise ValueError("Zip contains unsafe paths")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)

    return {"extracted_files": extracted_files, "extracted_bytes": total_unzipped}


def index(request: HttpRequest) -> HttpResponse:
    """
    Single-page File Explorer UI (no frontend build step).
    """
    html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>LLM Memory Optimizer — Phase 1</title>
    <style>
      :root {{
        --bg: #0b1020;
        --panel: #0f1733;
        --panel2: #101a3a;
        --text: #e8ecff;
        --muted: #a9b2d6;
        --border: rgba(255,255,255,0.09);
        --accent: #7aa2ff;
        --danger: #ff6b6b;
        --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji", "Segoe UI Emoji";
        color: var(--text);
        background: radial-gradient(1200px 800px at 15% 0%, #17265a 0%, var(--bg) 55%);
      }}
      header {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 16px 20px;
        border-bottom: 1px solid var(--border);
        background: rgba(11, 16, 32, 0.7);
        backdrop-filter: blur(8px);
        position: sticky;
        top: 0;
        z-index: 5;
      }}
      header h1 {{
        margin: 0;
        font-size: 16px;
        font-weight: 650;
        letter-spacing: 0.2px;
      }}
      header .sub {{
        color: var(--muted);
        font-size: 12px;
      }}
      main {{
        display: grid;
        grid-template-columns: 360px 1fr;
        gap: 12px;
        padding: 14px;
        height: calc(100vh - 62px);
      }}
      .card {{
        border: 1px solid var(--border);
        background: linear-gradient(180deg, rgba(15, 23, 51, 0.92), rgba(15, 23, 51, 0.75));
        border-radius: 14px;
        overflow: hidden;
        display: flex;
        flex-direction: column;
        min-height: 0;
      }}
      .card .card-h {{
        padding: 12px 12px 10px 12px;
        border-bottom: 1px solid var(--border);
        background: rgba(16, 26, 58, 0.65);
      }}
      .row {{
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
        align-items: center;
      }}
      input[type="file"] {{
        width: 100%;
      }}
      button {{
        background: rgba(122, 162, 255, 0.15);
        color: var(--text);
        border: 1px solid rgba(122, 162, 255, 0.35);
        padding: 8px 10px;
        border-radius: 10px;
        cursor: pointer;
        font-weight: 600;
      }}
      button:hover {{ background: rgba(122, 162, 255, 0.22); }}
      button:disabled {{
        opacity: 0.55;
        cursor: not-allowed;
      }}
      .pill {{
        font-size: 12px;
        color: var(--muted);
        padding: 6px 8px;
        border-radius: 999px;
        border: 1px solid var(--border);
        background: rgba(255,255,255,0.03);
      }}
      .content {{
        padding: 12px;
        overflow: auto;
        min-height: 0;
      }}
      .tree {{
        font-family: var(--mono);
        font-size: 12.5px;
        line-height: 1.35;
      }}
      .node {{
        display: grid;
        grid-template-columns: 18px 1fr;
        gap: 6px;
        padding: 3px 6px;
        border-radius: 8px;
      }}
      .node:hover {{
        background: rgba(255,255,255,0.05);
      }}
      .node .twisty {{
        opacity: 0.8;
        user-select: none;
      }}
      .node .name {{
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }}
      .node.file {{ cursor: pointer; }}
      .node.file.active {{
        outline: 1px solid rgba(122, 162, 255, 0.55);
        background: rgba(122, 162, 255, 0.10);
      }}
      .error {{
        color: var(--danger);
        font-size: 12px;
        margin-top: 8px;
      }}
      pre {{
        margin: 0;
        font-family: var(--mono);
        font-size: 12.5px;
        line-height: 1.5;
        color: var(--text);
        white-space: pre;
      }}
      .muted {{ color: var(--muted); }}
      .path {{
        font-family: var(--mono);
        font-size: 12px;
        color: var(--muted);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }}
      @media (max-width: 980px) {{
        main {{ grid-template-columns: 1fr; height: auto; }}
      }}
      .split {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 10px;
      }}
      textarea {{
        width: 100%;
        min-height: 110px;
        padding: 10px 12px;
        border-radius: 10px;
        border: 1px solid var(--border);
        background: rgba(255,255,255,0.03);
        color: var(--text);
        font-family: var(--mono);
        font-size: 12px;
      }}
      .btn-danger {{
        border-color: rgba(255,107,107,0.4);
        background: rgba(255,107,107,0.12);
      }}
    </style>
  </head>
  <body>
    <header>
      <div>
        <h1>LLM Memory Optimizer</h1>
        <div class="sub">Phase 1 — Repo ingestion + file explorer</div>
      </div>
      <div class="row">
        <span class="pill" id="repoPill">No repo loaded</span>
        <span class="pill" id="statsPill">—</span>
      </div>
    </header>
    <main>
      <section class="card">
        <div class="card-h">
          <div class="row" style="justify-content: space-between;">
            <div style="font-weight: 650;">Ingest</div>
            <div class="muted" style="font-size: 12px;">GitHub link or single file</div>
          </div>
        </div>
        <div class="content">
          <div class="row" style="margin-bottom: 10px;">
            <label class="pill" style="cursor:pointer;">
              <input type="radio" name="ingestMode" value="github" checked style="margin-right:6px;" />
              GitHub repo URL
            </label>
            <label class="pill" style="cursor:pointer;">
              <input type="radio" name="ingestMode" value="file" style="margin-right:6px;" />
              Single code file
            </label>
          </div>

          <form id="ingestForm">
            <div id="githubBox">
              <input id="repoUrl" type="text" placeholder="https://github.com/owner/repo" style="width:100%; padding:10px 12px; border-radius:10px; border:1px solid var(--border); background: rgba(255,255,255,0.03); color: var(--text);" />
              <div class="muted" style="font-size: 12px; margin-top: 6px;">Clones shallowly (depth=1). Private repos not supported in Phase 1.</div>
            </div>
            <div id="fileBox" style="display:none;">
              <input type="file" id="codeFile" name="file" />
              <div class="muted" style="font-size: 12px; margin-top: 6px;">Uploads one file and shows it in the explorer.</div>
            </div>
            <div class="row" style="margin-top: 10px;">
              <button id="ingestBtn" type="submit">Ingest</button>
              <button id="refreshBtn" type="button" disabled>Refresh tree</button>
            </div>
            <div id="uploadErr" class="error" style="display:none;"></div>
          </form>
          <div style="margin-top: 12px; font-weight: 650;">Files</div>
          <div id="tree" class="tree" style="margin-top: 8px;"></div>
        </div>
      </section>
      <section class="card">
        <div class="card-h">
          <div style="font-weight: 650;">Viewer</div>
          <div class="path" id="activePath">Select a file…</div>
        </div>
        <div class="content">
          <pre id="fileContent" class="muted">No file selected.</pre>
          <div id="viewerErr" class="error" style="display:none;"></div>

          <div style="margin-top: 14px; font-weight: 650;">Optimize (Hybrid + LangGraph)</div>
          <div class="muted" style="font-size: 12px; margin-top: 6px;">
            Runs a LangGraph pipeline: repo clone → file scan → function profiling → LLM optimization → re-profile → compare → store metadata.
          </div>
          <div class="row" style="margin-top: 10px;">
            <button id="startOptBtn" type="button" disabled>Start optimization</button>
            <span class="pill" id="runPill">No run</span>
          </div>
          <div id="optErr" class="error" style="display:none;"></div>

          <div style="margin-top: 10px; font-weight: 650;">Results</div>
          <div class="muted" style="font-size: 12px; margin-top: 6px;">Click a row to preview diff, then accept/reject.</div>
          <div id="resultsBox" style="margin-top: 8px;"></div>

          <div id="proposalBox" style="display:none; margin-top: 10px;">
            <div class="row" style="justify-content: space-between;">
              <div style="font-weight: 650;">Proposal</div>
              <div class="pill" id="proposalMeta">—</div>
            </div>
            <div class="split" style="margin-top: 8px;">
              <div>
                <div class="muted" style="font-size:12px; margin-bottom: 6px;">OLD</div>
                <textarea id="oldCode" readonly></textarea>
              </div>
              <div>
                <div class="muted" style="font-size:12px; margin-bottom: 6px;">NEW</div>
                <textarea id="newCode" readonly></textarea>
              </div>
            </div>
            <div class="muted" style="font-size:12px; margin-top: 8px;">Diff</div>
            <textarea id="diffText" readonly style="min-height: 160px;"></textarea>
            <div class="row" style="margin-top: 10px;">
              <button id="acceptBtn" type="button">Accept</button>
              <button id="rejectBtn" type="button" class="btn-danger">Reject</button>
            </div>
          </div>
        </div>
      </section>
    </main>
    <script>
      const state = {{
        repoId: null,
        runId: null,
        activeFilePath: null,
        tree: null,
      }};

      const el = {{
        repoPill: document.getElementById('repoPill'),
        statsPill: document.getElementById('statsPill'),
        ingestForm: document.getElementById('ingestForm'),
        repoUrl: document.getElementById('repoUrl'),
        codeFile: document.getElementById('codeFile'),
        githubBox: document.getElementById('githubBox'),
        fileBox: document.getElementById('fileBox'),
        ingestBtn: document.getElementById('ingestBtn'),
        refreshBtn: document.getElementById('refreshBtn'),
        uploadErr: document.getElementById('uploadErr'),
        viewerErr: document.getElementById('viewerErr'),
        tree: document.getElementById('tree'),
        activePath: document.getElementById('activePath'),
        fileContent: document.getElementById('fileContent'),
        startOptBtn: document.getElementById('startOptBtn'),
        runPill: document.getElementById('runPill'),
        optErr: document.getElementById('optErr'),
        resultsBox: document.getElementById('resultsBox'),
        proposalBox: document.getElementById('proposalBox'),
        proposalMeta: document.getElementById('proposalMeta'),
        oldCode: document.getElementById('oldCode'),
        newCode: document.getElementById('newCode'),
        diffText: document.getElementById('diffText'),
        acceptBtn: document.getElementById('acceptBtn'),
        rejectBtn: document.getElementById('rejectBtn'),
      }};

      function showErr(target, msg) {{
        target.style.display = msg ? 'block' : 'none';
        target.textContent = msg || '';
      }}

      function setRepo(repoId, stats) {{
        state.repoId = repoId;
        el.repoPill.textContent = repoId ? `Repo: ${{repoId}}` : 'No repo loaded';
        el.statsPill.textContent = stats ? `${{stats.files}} files • ${{formatBytes(stats.bytes)}}` : '—';
        el.refreshBtn.disabled = !repoId;
        el.startOptBtn.disabled = !repoId;
      }}

      function formatBytes(n) {{
        if (typeof n !== 'number') return '—';
        const units = ['B','KB','MB','GB'];
        let i = 0;
        let v = n;
        while (v >= 1024 && i < units.length - 1) {{ v /= 1024; i++; }}
        return `${{v.toFixed(i === 0 ? 0 : 1)}} ${{units[i]}}`;
      }}

      async function ingestZip(file) {{
        const fd = new FormData();
        fd.append('zip', file);
        const res = await fetch('/api/repos/ingest', {{ method: 'POST', body: fd }});
        const data = await res.json().catch(() => ({{}}));
        if (!res.ok) throw new Error(data.error || `Ingest failed (${{res.status}})`);
        return data;
      }}

      async function ingestGithub(url) {{
        const res = await fetch('/api/repos/ingest/github', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ url }})
        }});
        const data = await res.json().catch(() => ({{}}));
        if (!res.ok) throw new Error(data.error || `Ingest failed (${{res.status}})`);
        return data;
      }}

      async function ingestSingleFile(file) {{
        const fd = new FormData();
        fd.append('file', file);
        const res = await fetch('/api/repos/ingest/file', {{ method: 'POST', body: fd }});
        const data = await res.json().catch(() => ({{}}));
        if (!res.ok) throw new Error(data.error || `Ingest failed (${{res.status}})`);
        return data;
      }}

      async function fetchTree(repoId) {{
        const res = await fetch(`/api/repos/${{repoId}}/tree`);
        const data = await res.json().catch(() => ({{}}));
        if (!res.ok) throw new Error(data.error || `Tree fetch failed (${{res.status}})`);
        return data;
      }}

      async function fetchFile(repoId, path) {{
        const qs = new URLSearchParams({{ path }});
        const res = await fetch(`/api/repos/${{repoId}}/file?` + qs.toString());
        const data = await res.json().catch(() => ({{}}));
        if (!res.ok) throw new Error(data.error || `File fetch failed (${{res.status}})`);
        return data;
      }}

      async function startOptimization(repoUrl) {{
        const res = await fetch('/optimize-repo/', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ repo_url: repoUrl }})
        }});
        const data = await res.json().catch(() => ({{}}));
        if (!res.ok) throw new Error(data.error || `Start failed (${{res.status}})`);
        return data;
      }}

      async function fetchResults() {{
        const res = await fetch('/results/');
        const data = await res.json().catch(() => ({{}}));
        if (!res.ok) throw new Error(data.error || `Results failed (${{res.status}})`);
        return data;
      }}

      async function fetchProposal(changeId) {{
        const res = await fetch('/proposal/?id=' + encodeURIComponent(changeId));
        const data = await res.json().catch(() => ({{}}));
        if (!res.ok) throw new Error(data.error || `Proposal failed (${{res.status}})`);
        return data;
      }}

      async function approve(changeId, action) {{
        const res = await fetch('/approve/', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ id: changeId, action }})
        }});
        const data = await res.json().catch(() => ({{}}));
        if (!res.ok) throw new Error(data.error || `Approve failed (${{res.status}})`);
        return data;
      }}

      function renderTree(node, container, depth = 0) {{
        const wrap = document.createElement('div');
        wrap.style.marginLeft = depth ? '14px' : '0';

        const row = document.createElement('div');
        row.className = 'node ' + (node.type === 'file' ? 'file' : 'dir');

        const twisty = document.createElement('div');
        twisty.className = 'twisty';
        twisty.textContent = node.type === 'dir' ? '▾' : '';

        const name = document.createElement('div');
        name.className = 'name';
        name.textContent = node.name || '(root)';

        row.appendChild(twisty);
        row.appendChild(name);

        if (node.type === 'file') {{
          row.addEventListener('click', async () => {{
            document.querySelectorAll('.node.file.active').forEach(n => n.classList.remove('active'));
            row.classList.add('active');
            state.activeFilePath = node.path;
            el.activePath.textContent = node.path;
            el.fileContent.textContent = 'Loading…';
            showErr(el.viewerErr, '');
            try {{
              const data = await fetchFile(state.repoId, node.path);
              const suffix = data.truncated ? '\\n\\n…(truncated)' : '';
              el.fileContent.textContent = (data.content ?? '') + suffix;
            }} catch (e) {{
              el.fileContent.textContent = '';
              showErr(el.viewerErr, e.message || String(e));
            }}
          }});
        }}

        wrap.appendChild(row);

        if (node.type === 'dir' && Array.isArray(node.children)) {{
          const childBox = document.createElement('div');
          for (const c of node.children) {{
            renderTree(c, childBox, depth + 1);
          }}
          wrap.appendChild(childBox);
        }}

        container.appendChild(wrap);
      }}

      function showTree(tree) {{
        el.tree.innerHTML = '';
        if (!tree) {{
          el.tree.textContent = 'No repo loaded.';
          return;
        }}
        renderTree(tree, el.tree, 0);
      }}

      function getMode() {{
        const r = document.querySelector('input[name=\"ingestMode\"]:checked');
        return r ? r.value : 'github';
      }}

      function syncModeUI() {{
        const mode = getMode();
        el.githubBox.style.display = mode === 'github' ? 'block' : 'none';
        el.fileBox.style.display = mode === 'file' ? 'block' : 'none';
        showErr(el.uploadErr, '');
      }}

      document.querySelectorAll('input[name=\"ingestMode\"]').forEach(i => {{
        i.addEventListener('change', syncModeUI);
      }});
      syncModeUI();

      el.ingestForm.addEventListener('submit', async (ev) => {{
        ev.preventDefault();
        showErr(el.uploadErr, '');
        const mode = getMode();
        el.ingestBtn.disabled = true;
        el.ingestBtn.textContent = 'Ingesting…';
        try {{
          let data = null;
          if (mode === 'github') {{
            const url = (el.repoUrl.value || '').trim();
            if (!url) {{
              showErr(el.uploadErr, 'Paste a GitHub repo URL first.');
              return;
            }}
            data = await ingestGithub(url);
          }} else {{
            const file = el.codeFile.files && el.codeFile.files[0];
            if (!file) {{
              showErr(el.uploadErr, 'Choose a code file first.');
              return;
            }}
            data = await ingestSingleFile(file);
          }}
          setRepo(data.repo_id, data.stats);
          state.tree = data.tree;
          showTree(state.tree);
          state.runId = null;
          el.runPill.textContent = 'No run';
          el.resultsBox.innerHTML = '';
          el.proposalBox.style.display = 'none';
        }} catch (e) {{
          showErr(el.uploadErr, e.message || String(e));
        }} finally {{
          el.ingestBtn.disabled = false;
          el.ingestBtn.textContent = 'Ingest';
        }}
      }});

      el.refreshBtn.addEventListener('click', async () => {{
        if (!state.repoId) return;
        showErr(el.uploadErr, '');
        el.refreshBtn.disabled = true;
        el.refreshBtn.textContent = 'Refreshing…';
        try {{
          const data = await fetchTree(state.repoId);
          setRepo(state.repoId, data.stats);
          state.tree = data.tree;
          showTree(state.tree);
        }} catch (e) {{
          showErr(el.uploadErr, e.message || String(e));
        }} finally {{
          el.refreshBtn.disabled = false;
          el.refreshBtn.textContent = 'Refresh tree';
        }}
      }});

      function renderResults(changes) {{
        if (!Array.isArray(changes) || changes.length === 0) {{
          el.resultsBox.innerHTML = '<div class=\"muted\">No changes yet.</div>';
          return;
        }}
        const rows = changes.map(c => {{
          const saved = (typeof c.improvement_mb === 'number') ? c.improvement_mb.toFixed(2) + ' MB' : '—';
          const st = c.status || 'pending';
          return `<div class=\"node file\" data-id=\"${{c.id}}\" style=\"grid-template-columns: 1fr; margin-bottom:6px;\">
            <div class=\"name\">${{c.file_path}} :: ${{c.function_qualname}} <span class=\"pill\" style=\"margin-left:8px;\">saved ${{saved}}</span> <span class=\"pill\" style=\"margin-left:6px;\">${{st}}</span></div>
          </div>`;
        }}).join('');
        el.resultsBox.innerHTML = rows;
        el.resultsBox.querySelectorAll('[data-id]').forEach(n => {{
          n.addEventListener('click', async () => {{
            const id = n.getAttribute('data-id');
            showErr(el.optErr, '');
            try {{
              const p = await fetchProposal(id);
              el.proposalBox.style.display = 'block';
              el.oldCode.value = p.old_code || '';
              el.newCode.value = p.new_code || '';
              el.diffText.value = p.diff || '';
              el.proposalMeta.textContent = `Before ${{
                (p.before_peak_mb ?? '—')
              }} MB → After ${{
                (p.after_peak_mb ?? '—')
              }} MB`;
              el.acceptBtn.onclick = async () => {{
                await approve(id, 'accept');
                const r = await fetchResults();
                renderResults(r.changes || []);
              }};
              el.rejectBtn.onclick = async () => {{
                await approve(id, 'reject');
                const r = await fetchResults();
                renderResults(r.changes || []);
              }};
            }} catch (e) {{
              showErr(el.optErr, e.message || String(e));
            }}
          }});
        }});
      }}

      el.startOptBtn.addEventListener('click', async () => {{
        showErr(el.optErr, '');
        if (!state.repoId) return;
        // Optimization uses the last GitHub URL entered (Phase: GitHub ingestion only)
        const url = (el.repoUrl.value || '').trim();
        if (!url) {{
          showErr(el.optErr, 'Paste the GitHub repo URL (used for optimization).');
          return;
        }}
        el.startOptBtn.disabled = true;
        el.startOptBtn.textContent = 'Starting…';
        try {{
          const r = await startOptimization(url);
          state.runId = r.run_id;
          el.runPill.textContent = `Run: ${{state.runId}} (${{r.status}})`;
          // poll results
          const poll = async () => {{
            const data = await fetchResults();
            if (data.run) {{
              el.runPill.textContent = `Run: ${{data.run.id}} (${{data.run.status}})`;
            }}
            renderResults(data.changes || []);
            if (data.run && (data.run.status === 'running' || data.run.status === 'queued')) {{
              setTimeout(poll, 1500);
            }} else {{
              el.startOptBtn.disabled = false;
              el.startOptBtn.textContent = 'Start optimization';
            }}
          }};
          poll();
        }} catch (e) {{
          showErr(el.optErr, e.message || String(e));
          el.startOptBtn.disabled = false;
          el.startOptBtn.textContent = 'Start optimization';
        }}
      }});
    </script>
  </body>
</html>
"""
    return HttpResponse(html)


@csrf_exempt
@require_POST
def ingest_repo(request: HttpRequest) -> JsonResponse:
    """
    Ingest a repository from a .zip upload.

    Form field: zip=<file>
    Returns: repo_id, tree, stats
    """
    uploaded = request.FILES.get("zip")
    if uploaded is None:
        return JsonResponse({"error": "Missing form file field: zip"}, status=400)

    if not uploaded.name.lower().endswith(".zip"):
        return JsonResponse({"error": "Only .zip uploads are supported in Phase 1"}, status=400)

    if uploaded.size and uploaded.size > MAX_ZIP_BYTES:
        return JsonResponse({"error": f"Zip exceeds max size ({MAX_ZIP_BYTES} bytes)"}, status=413)

    repo_id = _new_repo_id()
    store_root = _repo_store_root()
    repo_root = store_root / repo_id
    tmp_dir = store_root / f"{repo_id}_tmp"

    try:
        tmp_dir.mkdir(parents=True, exist_ok=False)
        zip_path = tmp_dir / "repo.zip"
        with zip_path.open("wb") as f:
            for chunk in uploaded.chunks():
                f.write(chunk)

        repo_root.mkdir(parents=True, exist_ok=False)
        extract_meta = _validate_and_extract_zip(zip_path, repo_root)

        tree, stats = _build_tree(repo_root)
        payload = {
            "repo_id": repo_id,
            "tree": tree.to_dict(),
            "stats": stats,
            "extract_meta": extract_meta,
        }
        return JsonResponse(payload)
    except (zipfile.BadZipFile, ValueError) as e:
        # Clean up repo dir if something went wrong
        shutil.rmtree(repo_root, ignore_errors=True)
        return JsonResponse({"error": str(e)}, status=400)
    except FileExistsError:
        shutil.rmtree(repo_root, ignore_errors=True)
        return JsonResponse({"error": "Repo ingestion collision; retry"}, status=409)
    except Exception:
        shutil.rmtree(repo_root, ignore_errors=True)
        return JsonResponse({"error": "Unexpected error during ingestion"}, status=500)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@csrf_exempt
@require_POST
def ingest_github(request: HttpRequest) -> JsonResponse:
    """
    Ingest a repository by cloning from a GitHub URL (public repos only).
    Body: { "url": "https://github.com/owner/repo" }
    """
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    try:
        url = _normalize_github_url(str(payload.get("url", "")))
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)

    repo_id = _new_repo_id()
    store_root = _repo_store_root()
    repo_root = store_root / repo_id
    tmp_dir = store_root / f"{repo_id}_tmp"

    try:
        tmp_dir.mkdir(parents=True, exist_ok=False)
        repo_root.mkdir(parents=True, exist_ok=False)

        # Shallow clone to reduce bandwidth/time; GitPython uses git under the hood.
        Repo.clone_from(url, repo_root.as_posix(), depth=1, multi_options=["--no-tags"])

        # Remove .git directory to avoid exposing git internals & reduce tree noise.
        shutil.rmtree(repo_root / ".git", ignore_errors=True)

        tree, stats = _build_tree(repo_root)
        return JsonResponse(
            {
                "repo_id": repo_id,
                "source": "github",
                "url": url,
                "tree": tree.to_dict(),
                "stats": stats,
            }
        )
    except subprocess.CalledProcessError:
        shutil.rmtree(repo_root, ignore_errors=True)
        return JsonResponse({"error": "Git clone failed"}, status=400)
    except ValueError as e:
        shutil.rmtree(repo_root, ignore_errors=True)
        return JsonResponse({"error": str(e)}, status=400)
    except Exception:
        shutil.rmtree(repo_root, ignore_errors=True)
        return JsonResponse({"error": "Unexpected error during GitHub ingestion"}, status=500)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@csrf_exempt
@require_POST
def ingest_single_file(request: HttpRequest) -> JsonResponse:
    """
    Ingest a single uploaded code file.
    Form field: file=<file>
    """
    uploaded = request.FILES.get("file")
    if uploaded is None:
        return JsonResponse({"error": "Missing form file field: file"}, status=400)

    if uploaded.size and uploaded.size > MAX_SINGLE_FILE_BYTES:
        return JsonResponse({"error": f"File exceeds max size ({MAX_SINGLE_FILE_BYTES} bytes)"}, status=413)

    # Basic filename hardening (avoid paths).
    name = Path(uploaded.name).name
    if not name or name in (".", ".."):
        name = "uploaded.txt"

    repo_id = _new_repo_id()
    store_root = _repo_store_root()
    repo_root = store_root / repo_id

    try:
        repo_root.mkdir(parents=True, exist_ok=False)
        dest = (repo_root / name).resolve()
        if repo_root.resolve() not in dest.parents:
            return JsonResponse({"error": "Invalid filename"}, status=400)

        with dest.open("wb") as f:
            for chunk in uploaded.chunks():
                f.write(chunk)

        tree, stats = _build_tree(repo_root)
        return JsonResponse(
            {
                "repo_id": repo_id,
                "source": "file",
                "filename": name,
                "tree": tree.to_dict(),
                "stats": stats,
            }
        )
    except FileExistsError:
        shutil.rmtree(repo_root, ignore_errors=True)
        return JsonResponse({"error": "Repo ingestion collision; retry"}, status=409)
    except ValueError as e:
        shutil.rmtree(repo_root, ignore_errors=True)
        return JsonResponse({"error": str(e)}, status=400)
    except Exception:
        shutil.rmtree(repo_root, ignore_errors=True)
        return JsonResponse({"error": "Unexpected error during file ingestion"}, status=500)

@require_GET
def repo_tree(request: HttpRequest, repo_id: str) -> JsonResponse:
    store_root = _repo_store_root()
    repo_root = (store_root / repo_id).resolve()
    if not repo_root.exists() or not repo_root.is_dir():
        return JsonResponse({"error": "Repo not found"}, status=404)

    try:
        tree, stats = _build_tree(repo_root)
        return JsonResponse({"repo_id": repo_id, "tree": tree.to_dict(), "stats": stats})
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception:
        return JsonResponse({"error": "Unexpected error building tree"}, status=500)


@require_GET
def repo_file(request: HttpRequest, repo_id: str) -> JsonResponse:
    rel_path = request.GET.get("path")
    if not rel_path:
        return JsonResponse({"error": "Missing query param: path"}, status=400)

    store_root = _repo_store_root()
    repo_root = (store_root / repo_id).resolve()
    if not repo_root.exists() or not repo_root.is_dir():
        return JsonResponse({"error": "Repo not found"}, status=404)

    try:
        abs_path = _safe_join(repo_root, rel_path)
        if not abs_path.exists() or not abs_path.is_file():
            return JsonResponse({"error": "File not found"}, status=404)

        content, truncated = _read_text_file(abs_path, MAX_FILE_BYTES_TO_READ)
        return JsonResponse(
            {"repo_id": repo_id, "path": rel_path, "content": content, "truncated": truncated}
        )
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception:
        return JsonResponse({"error": "Unexpected error reading file"}, status=500)


# -----------------------------
# LangGraph Hybrid Optimizer
# -----------------------------

class PipelineState(TypedDict, total=False):
    run_id: str
    repo_url: str
    config: Dict[str, Any]
    repo_id: str
    repo_path: str
    selected_files: List[str]
    functions: List[Dict[str, Any]]
    profiled_functions: List[Dict[str, Any]]
    candidates: List[Dict[str, Any]]
    optimized: List[Dict[str, Any]]
    reprofilied: List[Dict[str, Any]]
    reprofiled: List[Dict[str, Any]]


def _node_event(run_id: str, node: str, phase: str, payload: Dict[str, Any]) -> None:
    # Minimal event storage: embed into run.error only on failure; detailed events can be added later.
    # For now, we update run.updated_at_ms and keep status in DB.
    _db_exec(
        "UPDATE optimization_run SET updated_at_ms=? WHERE id=?",
        (_now_ms(), run_id),
    )


def _update_run(run_id: str, **fields: Any) -> None:
    cols = []
    vals: List[Any] = []
    for k, v in fields.items():
        cols.append(f"{k}=?")
        vals.append(v)
    cols.append("updated_at_ms=?")
    vals.append(_now_ms())
    vals.append(run_id)
    _db_exec(f"UPDATE optimization_run SET {', '.join(cols)} WHERE id=?", tuple(vals))


def _insert_file_scan(run_id: str, file_path: str, peak_mb: Optional[float], size_bytes: int, signals: Dict[str, Any], selected: bool) -> None:
    _db_exec(
        "INSERT INTO file_scan_result (id, run_id, file_path, peak_mb, size_bytes, signals_json, selected, created_at_ms) VALUES (?,?,?,?,?,?,?,?)",
        (
            uuid.uuid4().hex,
            run_id,
            file_path,
            peak_mb,
            size_bytes,
            json.dumps(signals),
            1 if selected else 0,
            _now_ms(),
        ),
    )


def _insert_change(
    run_id: str,
    file_path: str,
    qualname: str,
    start_line: int,
    end_line: int,
    old_code: str,
    new_code: str,
    before_peak: Optional[float],
    before_inc: Optional[float],
    after_peak: Optional[float],
    after_inc: Optional[float],
    explanation: str,
) -> str:
    improvement_mb: Optional[float] = None
    improvement_pct: Optional[float] = None
    if before_peak is not None and after_peak is not None:
        improvement_mb = float(before_peak) - float(after_peak)
        if before_peak and before_peak > 0:
            improvement_pct = (improvement_mb / float(before_peak)) * 100.0

    cid = uuid.uuid4().hex
    _db_exec(
        """
        INSERT INTO function_change
        (id, run_id, file_path, function_qualname, start_line, end_line, old_code, new_code,
         before_peak_mb, before_increment_mb, after_peak_mb, after_increment_mb,
         improvement_mb, improvement_pct, explanation, status, created_at_ms, updated_at_ms)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            cid,
            run_id,
            file_path,
            qualname,
            start_line,
            end_line,
            old_code,
            new_code,
            before_peak,
            before_inc,
            after_peak,
            after_inc,
            improvement_mb,
            improvement_pct,
            explanation,
            "pending",
            _now_ms(),
            _now_ms(),
        ),
    )
    return cid


def _build_langgraph() -> Any:
    g: StateGraph = StateGraph(PipelineState)

    def n1_repo_ingest(state: PipelineState) -> PipelineState:
        run_id = state["run_id"]
        _node_event(run_id, "repo_ingest", "start", {})
        repo_url = state["repo_url"]
        url = _normalize_github_url(repo_url)
        repo_id = _new_repo_id()
        store_root = _repo_store_root()
        repo_root = store_root / repo_id
        repo_root.mkdir(parents=True, exist_ok=False)
        Repo.clone_from(url, repo_root.as_posix(), depth=1, multi_options=["--no-tags"])
        shutil.rmtree(repo_root / ".git", ignore_errors=True)
        state["repo_id"] = repo_id
        state["repo_path"] = str(repo_root)
        _update_run(run_id, repo_url=repo_url, repo_id=repo_id, repo_path=str(repo_root))
        _node_event(run_id, "repo_ingest", "end", {"repo_id": repo_id})
        return state

    def n2_file_scan(state: PipelineState) -> PipelineState:
        run_id = state["run_id"]
        _node_event(run_id, "file_scan", "start", {})
        repo_root = Path(state["repo_path"])
        threshold_mb = float(state["config"].get("file_scan_threshold_mb", DEFAULT_FILE_SCAN_THRESHOLD_MB))
        batch_size = int(state["config"].get("batch_size_files", DEFAULT_BATCH_SIZE_FILES))
        py_files = _iter_python_files(repo_root)
        selected: List[str] = []

        for batch in _chunked(py_files, batch_size):
            for p in batch:
                rel = p.relative_to(repo_root).as_posix()
                try:
                    txt = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    txt = ""
                signals = _file_signals(txt)
                peak, err = _measure_peak_mb_for_file(p, FILE_SCAN_TIMEOUT_S)
                size_b = int(p.stat().st_size) if p.exists() else 0
                # Selection strategy (hybrid): memory-based OR heuristic signals OR large files
                sel = (
                    (peak is not None and peak >= threshold_mb)
                    or (signals.get("score", 0) >= 1)
                    or (size_b >= 200_000)
                )
                if sel:
                    selected.append(rel)
                _insert_file_scan(run_id, rel, peak, size_b, {**signals, "error": err}, sel)

        state["selected_files"] = selected
        _node_event(run_id, "file_scan", "end", {"selected_files": len(selected)})
        return state

    def n3_ast_extract(state: PipelineState) -> PipelineState:
        run_id = state["run_id"]
        _node_event(run_id, "ast_extract", "start", {})
        repo_root = Path(state["repo_path"])
        selected_files: List[str] = list(state.get("selected_files", []))
        max_functions = int(state["config"].get("max_functions", 60))
        functions: List[Dict[str, Any]] = []
        for f in selected_files:
            for info in _extract_functions(repo_root, f):
                functions.append(dataclasses.asdict(info))
                if len(functions) >= max_functions:
                    break
            if len(functions) >= max_functions:
                break
        state["functions"] = functions
        _node_event(run_id, "ast_extract", "end", {"functions": len(functions)})
        return state

    def n4_profile_functions(state: PipelineState) -> PipelineState:
        run_id = state["run_id"]
        _node_event(run_id, "func_profile", "start", {})
        repo_root = Path(state["repo_path"])
        funcs: List[Dict[str, Any]] = list(state.get("functions", []))
        batch_size = int(state["config"].get("batch_size_functions", DEFAULT_BATCH_SIZE_FUNCTIONS))
        profiled: List[Dict[str, Any]] = []

        for batch in _chunked(funcs, batch_size):
            for f in batch:
                if not f.get("can_call_without_args"):
                    profiled.append({**f, "before_peak_mb": None, "before_increment_mb": None, "memory_report": "Skipped: requires args"})
                    continue
                try:
                    peak, inc, report = _profile_function_code(f["code"], f["qualname"], FUNC_PROFILE_TIMEOUT_S)
                    profiled.append({**f, "before_peak_mb": peak, "before_increment_mb": inc, "memory_report": report})
                except Exception as e:
                    profiled.append({**f, "before_peak_mb": None, "before_increment_mb": None, "memory_report": f"Profile failed: {e}"})
        state["profiled_functions"] = profiled
        _node_event(run_id, "func_profile", "end", {"profiled": len(profiled)})
        return state

    def n5_candidate_select(state: PipelineState) -> PipelineState:
        run_id = state["run_id"]
        _node_event(run_id, "candidate_select", "start", {})
        inc_threshold = float(state["config"].get("function_increment_threshold_mb", DEFAULT_FUNCTION_INCREMENT_THRESHOLD_MB))
        candidates: List[Dict[str, Any]] = []
        for f in state.get("profiled_functions", []):
            inc = f.get("before_increment_mb")
            peak = f.get("before_peak_mb")
            if isinstance(inc, (int, float)) and inc >= inc_threshold:
                candidates.append(f)
            elif isinstance(peak, (int, float)) and peak >= float(state["config"].get("file_scan_threshold_mb", DEFAULT_FILE_SCAN_THRESHOLD_MB)):
                candidates.append(f)
        # If runtime profiling couldn't identify candidates (common for functions needing args),
        # fall back to a small static batch from selected files so the LLM stage still runs.
        if not candidates:
            fallback_limit = int(state["config"].get("fallback_candidate_limit", 20))
            for f in state.get("profiled_functions", [])[:fallback_limit]:
                candidates.append(f)
        state["candidates"] = candidates
        _node_event(run_id, "candidate_select", "end", {"candidates": len(candidates)})
        return state

    def n6_llm_optimize(state: PipelineState) -> PipelineState:
        run_id = state["run_id"]
        _node_event(run_id, "llm_optimize", "start", {})
        candidates: List[Dict[str, Any]] = list(state.get("candidates", []))
        llm_batch_size = int(state["config"].get("llm_batch_size", DEFAULT_LLM_BATCH_SIZE))
        optimized: List[Dict[str, Any]] = []
        for batch in _chunked(candidates, llm_batch_size):
            resp = _llm_optimize_batch(
                [{"qualname": b["qualname"], "code": b["code"], "memory_report": b.get("memory_report", "")} for b in batch]
            )
            for b, r in zip(batch, resp):
                optimized.append({**b, "optimized_code": r.get("optimized_code", b["code"]), "llm_explanation": r.get("explanation", "")})
        state["optimized"] = optimized
        _node_event(run_id, "llm_optimize", "end", {"optimized": len(optimized)})
        return state

    def n7_reprofile(state: PipelineState) -> PipelineState:
        run_id = state["run_id"]
        _node_event(run_id, "reprofile", "start", {})
        optimized: List[Dict[str, Any]] = list(state.get("optimized", []))
        repro: List[Dict[str, Any]] = []
        for f in optimized:
            code = f.get("optimized_code", f["code"])
            try:
                peak, inc, report = _profile_function_code(code, f["qualname"], FUNC_PROFILE_TIMEOUT_S)
                repro.append({**f, "after_peak_mb": peak, "after_increment_mb": inc, "after_report": report})
            except Exception as e:
                repro.append({**f, "after_peak_mb": None, "after_increment_mb": None, "after_report": f"Reprofile failed: {e}"})
        state["reprofiled"] = repro
        _node_event(run_id, "reprofile", "end", {"reprofiled": len(repro)})
        return state

    def n8_compare_store(state: PipelineState) -> PipelineState:
        run_id = state["run_id"]
        _node_event(run_id, "compare_store", "start", {})
        repo_root = Path(state["repo_path"])
        for f in state.get("reprofiled", []):
            file_rel = f["file_rel"]
            abs_path = repo_root / file_rel
            try:
                file_text = abs_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            old_code = f["code"]
            new_code = f.get("optimized_code", old_code)
            cid = _insert_change(
                run_id=run_id,
                file_path=file_rel,
                qualname=f["qualname"],
                start_line=int(f["start_line"]),
                end_line=int(f["end_line"]),
                old_code=old_code,
                new_code=new_code,
                before_peak=f.get("before_peak_mb"),
                before_inc=f.get("before_increment_mb"),
                after_peak=f.get("after_peak_mb"),
                after_inc=f.get("after_increment_mb"),
                explanation=str(f.get("llm_explanation", "")),
            )
            if new_code.strip() == old_code.strip():
                _db_exec(
                    "UPDATE function_change SET status=? WHERE id=?",
                    ("no_change", cid),
                )
        _node_event(run_id, "compare_store", "end", {})
        return state

    g.add_node("repo_ingest", n1_repo_ingest)
    g.add_node("file_scan", n2_file_scan)
    g.add_node("ast_extract", n3_ast_extract)
    g.add_node("func_profile", n4_profile_functions)
    g.add_node("candidate_select", n5_candidate_select)
    g.add_node("llm_optimize", n6_llm_optimize)
    g.add_node("reprofile", n7_reprofile)
    g.add_node("compare_store", n8_compare_store)

    g.add_edge(START, "repo_ingest")
    g.add_edge("repo_ingest", "file_scan")
    g.add_edge("file_scan", "ast_extract")
    g.add_edge("ast_extract", "func_profile")
    g.add_edge("func_profile", "candidate_select")
    g.add_edge("candidate_select", "llm_optimize")
    g.add_edge("llm_optimize", "reprofile")
    g.add_edge("reprofile", "compare_store")
    g.add_edge("compare_store", END)

    return g.compile()


def _run_pipeline_background(run_id: str, repo_url: str, config: Dict[str, Any]) -> None:
    try:
        _update_run(run_id, status="running")
        graph = _build_langgraph()
        graph.invoke({"run_id": run_id, "repo_url": repo_url, "config": config})
        _update_run(run_id, status="completed")
    except Exception as e:
        _update_run(run_id, status="failed", error=str(e))


@csrf_exempt
@require_POST
def optimize_repo(request: HttpRequest) -> JsonResponse:
    """
    POST /optimize-repo/
    Body: { "repo_url": "https://github.com/owner/repo", "config": {...optional...} }
    """
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    repo_url = str(payload.get("repo_url", "")).strip()
    if not repo_url:
        return JsonResponse({"error": "Missing repo_url"}, status=400)

    # Validate early
    try:
        _normalize_github_url(repo_url)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)

    config = payload.get("config") or {}
    if not isinstance(config, dict):
        config = {}

    run_id = uuid.uuid4().hex
    now = _now_ms()
    _ensure_db()
    _db_exec(
        "INSERT INTO optimization_run (id, created_at_ms, updated_at_ms, status, repo_url, config_json) VALUES (?,?,?,?,?,?)",
        (run_id, now, now, "queued", repo_url, json.dumps(config)),
    )

    t = threading.Thread(target=_run_pipeline_background, args=(run_id, repo_url, config), daemon=True)
    _RUN_THREADS[run_id] = t
    t.start()
    return JsonResponse({"run_id": run_id, "status": "queued"})


@require_GET
def get_results(request: HttpRequest) -> JsonResponse:
    """
    GET /results/
    Returns the latest run + all function_change rows.
    """
    _ensure_db()
    runs = _db_all("SELECT * FROM optimization_run ORDER BY created_at_ms DESC LIMIT 1")
    run = runs[0] if runs else None
    changes: List[Dict[str, Any]] = []
    file_scans: List[Dict[str, Any]] = []
    if run:
        changes = _db_all(
            "SELECT id, file_path, function_qualname, before_peak_mb, after_peak_mb, improvement_mb, status FROM function_change WHERE run_id=? ORDER BY improvement_mb DESC",
            (run["id"],),
        )
        file_scans = _db_all(
            "SELECT file_path, peak_mb, size_bytes, selected, signals_json FROM file_scan_result WHERE run_id=? ORDER BY (selected) DESC, peak_mb DESC",
            (run["id"],),
        )
    return JsonResponse({"run": run, "file_scans": file_scans, "changes": changes})


@require_GET
def proposal(request: HttpRequest) -> JsonResponse:
    """
    GET /proposal/?id=<change_id>
    """
    cid = str(request.GET.get("id", "")).strip()
    if not cid:
        return JsonResponse({"error": "Missing id"}, status=400)
    rows = _db_all("SELECT * FROM function_change WHERE id=?", (cid,))
    if not rows:
        return JsonResponse({"error": "Not found"}, status=404)
    c = rows[0]
    diff = _diff_text(c["old_code"], c["new_code"], c["file_path"])
    return JsonResponse(
        {
            "id": c["id"],
            "file_path": c["file_path"],
            "function_qualname": c["function_qualname"],
            "old_code": c["old_code"],
            "new_code": c["new_code"],
            "diff": diff,
            "before_peak_mb": c["before_peak_mb"],
            "after_peak_mb": c["after_peak_mb"],
            "explanation": c.get("explanation", ""),
            "status": c["status"],
        }
    )


@csrf_exempt
@require_POST
def approve_change(request: HttpRequest) -> JsonResponse:
    """
    POST /approve/
    Body: { "id": "<change_id>", "action": "accept" | "reject" }
    If accepted: apply change to file and create a .bak backup.
    """
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    cid = str(payload.get("id", "")).strip()
    action = str(payload.get("action", "")).strip().lower()
    if not cid or action not in ("accept", "reject"):
        return JsonResponse({"error": "Invalid id/action"}, status=400)

    rows = _db_all("SELECT * FROM function_change WHERE id=?", (cid,))
    if not rows:
        return JsonResponse({"error": "Not found"}, status=404)
    c = rows[0]

    if action == "reject":
        _db_exec("UPDATE function_change SET status=?, updated_at_ms=? WHERE id=?", ("rejected", _now_ms(), cid))
        return JsonResponse({"ok": True, "status": "rejected"})

    # accept/apply
    run_rows = _db_all("SELECT * FROM optimization_run WHERE id=?", (c["run_id"],))
    if not run_rows:
        return JsonResponse({"error": "Run not found"}, status=404)
    run = run_rows[0]
    repo_path = run.get("repo_path")
    if not repo_path:
        return JsonResponse({"error": "Repo path missing for run"}, status=400)

    repo_root = Path(str(repo_path))
    abs_path = (repo_root / c["file_path"]).resolve()
    if not abs_path.exists():
        return JsonResponse({"error": "File missing on disk"}, status=404)

    try:
        old_text = abs_path.read_text(encoding="utf-8", errors="replace")
        # backup
        bak = abs_path.with_suffix(abs_path.suffix + f".bak_{cid[:8]}")
        bak.write_text(old_text, encoding="utf-8")
        new_text = _apply_function_replacement(old_text, int(c["start_line"]), int(c["end_line"]), str(c["new_code"]))
        abs_path.write_text(new_text, encoding="utf-8")
        _db_exec("UPDATE function_change SET status=?, updated_at_ms=? WHERE id=?", ("accepted", _now_ms(), cid))
        return JsonResponse({"ok": True, "status": "accepted", "backup": bak.name})
    except Exception as e:
        return JsonResponse({"error": f"Apply failed: {e}"}, status=500)

