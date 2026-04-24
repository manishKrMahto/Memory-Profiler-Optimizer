from __future__ import annotations

import io
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from optimizer import ast_service, file_service, ingest_service, llm_service, profiler_service
from optimizer.models import Function, ProfilingResult, RepoFile, Repository


def _json_error(message: str, *, status: int = 400) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def _fmt1(v: Any) -> str:
    try:
        if v is None:
            return "—"
        return f"{float(v):.1f}"
    except Exception:
        return "—"


def _log_step(title: str, lines: List[str] | None = None) -> None:
    print(f"\n=== {title} ===", flush=True)
    for ln in (lines or []):
        print(ln, flush=True)


def _log_code_block(title: str, code: str) -> None:
    max_chars = int(os.environ.get("MPO_LOG_CODE_CHARS", "4000") or "4000")
    c = (code or "").rstrip()
    shown = c if len(c) <= max_chars else (c[:max_chars] + "\n... [truncated] ...")
    _log_step(title, shown.splitlines())


@require_GET
def _debug_module(request: HttpRequest) -> JsonResponse:
    import inspect

    import optimizer.views_api as mod

    f = optimize_function
    wrapped = getattr(f, "__wrapped__", None)
    try:
        src_line = inspect.getsourcelines(f)[1]
    except Exception:
        src_line = None
    try:
        wrapped_line = inspect.getsourcelines(wrapped)[1] if wrapped else None
    except Exception:
        wrapped_line = None
    return JsonResponse(
        {
            "views_api_file": getattr(mod, "__file__", None),
            "optimize_func": str(f),
            "optimize_func_name": getattr(f, "__name__", None),
            "optimize_source_line": src_line,
            "optimize_wrapped": str(wrapped) if wrapped else None,
            "optimize_wrapped_name": getattr(wrapped, "__name__", None) if wrapped else None,
            "optimize_wrapped_source_line": wrapped_line,
        }
    )


@require_GET
def repos(request: HttpRequest) -> JsonResponse:
    repos_qs = Repository.objects.all().order_by("-id")
    return JsonResponse(
        {"repos": [{"id": r.id, "name": r.name, "path": r.path} for r in repos_qs]},
    )


@require_GET
def files_for_repo(request: HttpRequest, repo_id: int) -> JsonResponse:
    files_qs = RepoFile.objects.filter(repo_id=repo_id).order_by("file_path")
    return JsonResponse({"files": [{"id": f.id, "file_path": f.file_path} for f in files_qs]})


@require_GET
def functions_for_file(request: HttpRequest, file_id: int) -> JsonResponse:
    fns = Function.objects.filter(file_id=file_id).order_by("start_line", "function_name")
    return JsonResponse(
        {
            "functions": [
                {
                    "id": fn.id,
                    "function_name": fn.function_name,
                    "start_line": fn.start_line,
                    "end_line": fn.end_line,
                    "decision": fn.decision,
                    "preferred_version": fn.preferred_version,
                }
                for fn in fns
            ]
        }
    )


def _latest_profile(function_id: int, version: str) -> Optional[ProfilingResult]:
    return (
        ProfilingResult.objects.filter(function_id=function_id, version=version)
        .order_by("-created_at", "-id")
        .first()
    )


def function_detail(request: HttpRequest, fn_id: int) -> JsonResponse:
    if request.method not in {"GET", "POST"}:
        return _json_error("Method not allowed", status=405)
    try:
        fn = Function.objects.select_related("file__repo").get(id=fn_id)
    except Function.DoesNotExist:
        return _json_error("Function not found", status=404)

    before = _latest_profile(fn.id, "original")
    after = _latest_profile(fn.id, "optimized")

    def _prof_payload(p: Optional[ProfilingResult]) -> Optional[Dict[str, Any]]:
        if not p:
            return None
        return {
            "memory_usage": p.memory_usage or [],
            "peak_memory": p.peak_memory,
            "execution_time": p.execution_time,
            "error": p.error or "",
        }

    comparison: Dict[str, Any] = {"improved": None, "improvement_pct": None}
    if before and after and before.peak_memory is not None and after.peak_memory is not None:
        improvement = float(before.peak_memory) - float(after.peak_memory)
        comparison["improved"] = improvement > 0
        comparison["improvement_pct"] = (improvement / float(before.peak_memory) * 100.0) if before.peak_memory else None

    return JsonResponse(
        {
            "function": {
                "id": fn.id,
                "repo_id": fn.file.repo_id,
                "file_id": fn.file_id,
                "file_path": fn.file.file_path,
                "function_name": fn.function_name,
                "start_line": fn.start_line,
                "end_line": fn.end_line,
                "original_code": fn.original_code,
                "optimized_code": fn.optimized_code,
                "decision": fn.decision,
                "preferred_version": fn.preferred_version,
            },
            "before": _prof_payload(before),
            "after": _prof_payload(after),
            "comparison": comparison,
        }
    )


@csrf_exempt
@require_POST
def ingest_github(request: HttpRequest) -> JsonResponse:
    try:
        import json

        payload = json.loads((request.body or b"{}").decode("utf-8", errors="replace") or "{}")
        url = (payload.get("url") or "").strip()
        if not url:
            return _json_error("Missing url")

        repo_uid, repo_root = ingest_service.ingest_github_repo(url)
        name = Path(repo_root).name
        repo = Repository.objects.create(name=name, path=str(repo_root))

        # Scan files + functions
        py_files = file_service.get_python_files(repo_root)
        for rel in py_files:
            rf = RepoFile.objects.create(repo=repo, file_path=rel)
            meta = ast_service.extract_functions((Path(repo_root) / rel), repo_root=repo_root)
            for fnm in meta:
                Function.objects.create(
                    file=rf,
                    function_name=fnm.function_name,
                    start_line=fnm.start_line,
                    end_line=fnm.end_line,
                    original_code=fnm.code,
                )

        return JsonResponse({"repo": {"id": repo.id, "name": repo.name, "path": repo.path, "repo_uid": repo_uid}})
    except Exception as e:
        return _json_error(str(e))


@csrf_exempt
@require_POST
def ingest_single_file(request: HttpRequest) -> JsonResponse:
    try:
        up = request.FILES.get("file")
        if up is None:
            return _json_error("Missing file upload")
        filename = os.path.basename(getattr(up, "name", "") or "uploaded.py")
        if not filename.endswith(".py"):
            return _json_error("Only .py files are supported")

        repo_uid, repo_root = ingest_service._new_repo_dir()  # type: ignore[attr-defined]
        abs_file = (repo_root / filename).resolve()
        with abs_file.open("wb") as f:
            for chunk in up.chunks():
                f.write(chunk)

        repo = Repository.objects.create(name=Path(repo_root).name, path=str(repo_root))
        rel = Path(filename).as_posix()
        rf = RepoFile.objects.create(repo=repo, file_path=rel)
        meta = ast_service.extract_functions(abs_file, repo_root=repo_root)
        for fnm in meta:
            Function.objects.create(
                file=rf,
                function_name=fnm.function_name,
                start_line=fnm.start_line,
                end_line=fnm.end_line,
                original_code=fnm.code,
            )

        return JsonResponse({"repo": {"id": repo.id, "name": repo.name, "path": repo.path, "repo_uid": repo_uid}})
    except Exception as e:
        return _json_error(str(e))


@csrf_exempt
def optimize_function(request: HttpRequest, function_id: int) -> JsonResponse:
    if request.method != "POST":
        return _json_error("Method not allowed (use POST)", status=405)
    try:
        fn = Function.objects.select_related("file__repo").get(id=function_id)
    except Function.DoesNotExist:
        return _json_error("Function not found", status=404)

    repo_root = Path(fn.file.repo.path).resolve()
    rel_path = fn.file.file_path
    abs_file = (repo_root / rel_path).resolve()

    _log_step(
        "OPTIMIZE START",
        [
            f"function_id={fn.id}",
            f"repo_id={fn.file.repo_id}",
            f"file={fn.file.file_path}",
            f"qualname={fn.function_name}",
        ],
    )
    _log_code_block("ORIGINAL CODE", fn.original_code or "")

    # Profile original
    _log_step("STEP 1/4: Profile original")
    before = profiler_service.profile_function(abs_file, fn.function_name, timeout_s=profiler_service.DEFAULT_TIMEOUT_S)
    ProfilingResult.objects.create(
        function=fn,
        version="original",
        memory_usage=before.memory_usage,
        peak_memory=before.peak_memory,
        execution_time=before.execution_time,
        profiler_output=before.profiler_output,
        error=before.error,
    )
    _log_step(
        "ORIGINAL PROFILE RESULT",
        [
            f"peak_memory={_fmt1(before.peak_memory)} MB",
            f"execution_time={_fmt1(before.execution_time)} s",
            f"samples={len(before.memory_usage or [])}",
            f"error={(before.error or '').strip() or '—'}",
        ],
    )

    # Optimize with LLM using original profile data
    _log_step("STEP 2/4: Call LLM optimizer")
    opt = llm_service.optimize_function_with_llm(
        fn.original_code,
        {"memory_usage": before.memory_usage, "peak_memory": before.peak_memory, "execution_time": before.execution_time, "error": before.error},
    )
    fn.optimized_code = opt.optimized_code or fn.original_code
    fn.save(update_fields=["optimized_code", "updated_at"])
    _log_step("LLM RESULT", [f"llm_error={(opt.error or '').strip() or '—'}"])
    _log_code_block("OPTIMIZED CODE (FROM LLM)", fn.optimized_code or "")

    # Profile optimized code
    _log_step("STEP 3/4: Profile optimized")
    after = profiler_service.profile_optimized_function(abs_file, fn.function_name, fn.optimized_code, timeout_s=profiler_service.DEFAULT_TIMEOUT_S)
    ProfilingResult.objects.create(
        function=fn,
        version="optimized",
        memory_usage=after.memory_usage,
        peak_memory=after.peak_memory,
        execution_time=after.execution_time,
        profiler_output=after.profiler_output,
        error=after.error,
    )
    _log_step(
        "OPTIMIZED PROFILE RESULT",
        [
            f"peak_memory={_fmt1(after.peak_memory)} MB",
            f"execution_time={_fmt1(after.execution_time)} s",
            f"samples={len(after.memory_usage or [])}",
            f"error={(after.error or '').strip() or '—'}",
        ],
    )

    # Summary
    improved = None
    improvement_pct = None
    if before.peak_memory is not None and after.peak_memory is not None and float(before.peak_memory) != 0.0:
        delta = float(before.peak_memory) - float(after.peak_memory)
        improved = delta > 0
        improvement_pct = (delta / float(before.peak_memory)) * 100.0
    _log_step(
        "STEP 4/4: Summary",
        [
            f"improved={improved if improved is not None else '—'}",
            f"improvement_pct={_fmt1(improvement_pct)} %",
        ],
    )
    _log_step("OPTIMIZE END")

    # Return detail payload (reusing function_detail logic)
    return function_detail(request, fn.id)


@csrf_exempt
@require_POST
def function_decision(request: HttpRequest, fn_id: int) -> JsonResponse:
    try:
        import json

        fn = Function.objects.select_related("file__repo").get(id=fn_id)
        payload = json.loads((request.body or b"{}").decode("utf-8", errors="replace") or "{}")
        action = (payload.get("action") or "").strip().lower()
        if action not in ("accept", "reject"):
            return _json_error("action must be accept or reject")
        fn.decision = "accepted" if action == "accept" else "rejected"
        fn.preferred_version = "optimized" if action == "accept" else "original"
        fn.save(update_fields=["decision", "preferred_version", "updated_at"])

        download_url = None
        applied = False
        backup_name = None

        if action == "accept":
            # Apply optimized code into the ingested repo file so the download reflects the accepted version.
            repo_root = Path(fn.file.repo.path).resolve()
            info = file_service.resolve_repo_file(repo_root, fn.file.file_path)
            if info.abs_file.exists() and info.abs_file.is_file():
                old_text = info.abs_file.read_text(encoding="utf-8", errors="replace")

                # backup
                bak = info.abs_file.with_suffix(info.abs_file.suffix + f".bak_fn{fn.id}")
                bak.write_text(old_text, encoding="utf-8")

                lines = old_text.splitlines(keepends=True)
                start = max(1, int(fn.start_line or 1))
                end = max(start, int(fn.end_line or start))
                new_code = (fn.optimized_code or fn.original_code or "").rstrip("\n") + "\n"
                new_text = "".join(lines[: start - 1] + [new_code] + lines[end:])
                info.abs_file.write_text(new_text, encoding="utf-8")

                applied = True
                backup_name = bak.name
                download_url = f"/file/{fn.file_id}/download?ts={int(time.time())}"

        return JsonResponse(
            {
                "ok": True,
                "decision": fn.decision,
                "preferred_version": fn.preferred_version,
                "applied": applied,
                "backup": backup_name,
                "download_url": download_url,
            }
        )
    except Function.DoesNotExist:
        return _json_error("Function not found", status=404)
    except Exception as e:
        return _json_error(str(e))


@require_GET
def function_memory_chart_png(request: HttpRequest, fn_id: int) -> HttpResponse:
    """
    Renders a Matplotlib line chart comparing memory samples:
    - Old function: red
    - Optimized function: green
    """
    try:
        fn = Function.objects.get(id=fn_id)
        before = _latest_profile(fn_id, "original")
        after = _latest_profile(fn_id, "optimized")
        old = (before.memory_usage if before else []) or []
        new = (after.memory_usage if after else []) or []

        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.ticker import MaxNLocator

        # Dark UI friendly: transparent background + white typography.

        fig = plt.figure(figsize=(8, 3.2), dpi=140)
        ax = fig.add_subplot(1, 1, 1)

        start_line = int(getattr(fn, "start_line", 1) or 1)
        end_line = int(getattr(fn, "end_line", start_line) or start_line)
        span = max(1, (end_line - start_line))

        def _x_for(n: int) -> List[float]:
            # Map sample indices to code line numbers (best-effort, since samples are time-based).
            if n <= 1:
                return [float(start_line)]
            return [float(start_line) + (span * (i / float(n - 1))) for i in range(n)]

        if old:
            ax.plot(_x_for(len(old)), old, color="#ff6b6b", linewidth=2.0, label="Old Function")
        if new:
            ax.plot(_x_for(len(new)), new, color="#44d19a", linewidth=2.0, label="Optimized Function")

        ax.set_xlabel("Code line", color="white")
        ax.set_ylabel("Memory (MB)", color="white")
        ax.tick_params(axis="both", colors="white", labelsize=9)
        ax.xaxis.set_major_locator(MaxNLocator(nbins=6, integer=True))

        ax.grid(True, alpha=0.22, linewidth=0.8, color=(1, 1, 1, 0.35))
        for spine in ax.spines.values():
            spine.set_color((1, 1, 1, 0.25))

        leg = ax.legend(loc="best", frameon=False)
        for t in leg.get_texts():
            t.set_color("white")

        fig.patch.set_facecolor("none")
        ax.set_facecolor("none")
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", transparent=True)
        plt.close(fig)
        buf.seek(0)
        return HttpResponse(buf.getvalue(), content_type="image/png")
    except Exception as e:
        # If chart fails, return a small text response rather than 500 HTML.
        return HttpResponse(f"chart_error: {e}".encode("utf-8", errors="replace"), content_type="text/plain", status=500)


@csrf_exempt
@require_POST
def optimize_file(request: HttpRequest, file_id: int) -> JsonResponse:
    """
    Best-effort helper to optimize every function in a file.
    """
    try:
        file_obj = RepoFile.objects.select_related("repo").get(id=file_id)
    except RepoFile.DoesNotExist:
        return _json_error("File not found", status=404)

    results: List[Dict[str, Any]] = []
    for fn in Function.objects.filter(file=file_obj).order_by("start_line", "id"):
        resp = optimize_function(request, fn.id)
        try:
            import json

            payload = json.loads((getattr(resp, "content", b"") or b"{}").decode("utf-8", errors="replace") or "{}")
        except Exception:
            payload = {"error": "optimize_failed"}
        results.append({"function_id": fn.id, "function_name": fn.function_name, "result": payload})
    return JsonResponse({"file_id": file_id, "results": results})


@require_GET
def file_merged_code(request: HttpRequest, file_id: int) -> JsonResponse:
    """
    Returns the repo file as text. (A richer merge-by-spans can be added later.)
    """
    try:
        file_obj = RepoFile.objects.select_related("repo").get(id=file_id)
    except RepoFile.DoesNotExist:
        return _json_error("File not found", status=404)

    info = file_service.resolve_repo_file(file_obj.repo.path, file_obj.file_path)
    if not info.abs_file.exists() or not info.abs_file.is_file():
        return _json_error("File missing on disk", status=404)
    code = info.abs_file.read_text(encoding="utf-8", errors="replace")
    return JsonResponse({"file_id": file_id, "file_path": file_obj.file_path, "merged_code": code})


@require_GET
def file_download(request: HttpRequest, file_id: int) -> HttpResponse:
    try:
        file_obj = RepoFile.objects.select_related("repo").get(id=file_id)
    except RepoFile.DoesNotExist:
        return HttpResponse("File not found", status=404, content_type="text/plain")

    info = file_service.resolve_repo_file(file_obj.repo.path, file_obj.file_path)
    if not info.abs_file.exists() or not info.abs_file.is_file():
        return HttpResponse("File missing on disk", status=404, content_type="text/plain")

    code = info.abs_file.read_text(encoding="utf-8", errors="replace")
    resp = HttpResponse(code, content_type="text/x-python; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{Path(file_obj.file_path).name}"'
    return resp

