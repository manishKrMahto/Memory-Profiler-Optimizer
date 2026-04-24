from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Iterable, List, Tuple

from django.db import transaction

from optimizer.ast_service import FunctionMeta, extract_functions
from optimizer.file_service import get_python_files, resolve_repo_file
from optimizer.models import Function, ProfilingResult, RepoFile, Repository


@transaction.atomic
def create_repository(name: str, path: str) -> Repository:
    return Repository.objects.create(name=name, path=str(Path(path).resolve()))


@transaction.atomic
def upsert_repo_files_and_functions(repo: Repository) -> Tuple[int, int]:
    """
    Scans repo.path for `.py` files and extracts functions into DB.

    Returns: (files_count, functions_count)
    """
    repo_root = Path(repo.path).resolve()
    py_files = get_python_files(repo_root)

    files_created = 0
    funcs_created = 0

    for rel in py_files:
        rf, created = RepoFile.objects.get_or_create(repo=repo, file_path=rel)
        if created:
            files_created += 1

        info = resolve_repo_file(repo_root, rel)
        metas: List[FunctionMeta] = extract_functions(info.abs_file, repo_root=repo_root)
        for m in metas:
            _, fn_created = Function.objects.get_or_create(
                file=rf,
                function_name=m.function_name,
                start_line=m.start_line,
                end_line=m.end_line,
                defaults={
                    "original_code": m.code,
                    "optimized_code": "",
                },
            )
            if fn_created:
                funcs_created += 1

    return files_created, funcs_created


@transaction.atomic
def store_profiling_result(
    fn: Function,
    *,
    version: str,
    memory_usage: list,
    peak_memory: float | None,
    execution_time: float | None,
    profiler_output: str = "",
    error: str = "",
) -> ProfilingResult:
    return ProfilingResult.objects.create(
        function=fn,
        version=version,
        memory_usage=memory_usage,
        peak_memory=peak_memory,
        execution_time=execution_time,
        profiler_output=profiler_output,
        error=error,
    )

