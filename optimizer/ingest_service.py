from __future__ import annotations

import shutil
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple
from urllib.parse import urlparse

from django.conf import settings

from git import Repo  # type: ignore


REPO_STORE_DIRNAME = "repo_store"

MAX_ZIP_BYTES = 200 * 1024 * 1024
MAX_TOTAL_UNZIPPED_BYTES = 600 * 1024 * 1024
MAX_FILES = 50_000


def _repo_store_root() -> Path:
    root = Path(settings.BASE_DIR).resolve() / REPO_STORE_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _new_repo_dir() -> Tuple[str, Path]:
    repo_id = uuid.uuid4().hex
    root = _repo_store_root()
    repo_root = (root / repo_id).resolve()
    repo_root.mkdir(parents=True, exist_ok=False)
    return repo_id, repo_root


def normalize_github_url(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.startswith("github.com/"):
        raw = "https://" + raw
    u = urlparse(raw)
    if u.scheme not in ("http", "https"):
        raise ValueError("Repo URL must start with http(s)://")
    if (u.netloc or "").lower() != "github.com":
        raise ValueError("Only github.com repos are supported")
    path = (u.path or "").strip("/")
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("GitHub URL must look like https://github.com/<owner>/<repo>")
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return f"https://github.com/{owner}/{repo}.git"


def validate_and_extract_zip(zip_path: Path, dest_dir: Path) -> Dict[str, int]:
    total_unzipped = 0
    extracted_files = 0

    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        for info in infos:
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


def ingest_github_repo(url: str) -> Tuple[str, Path]:
    repo_id, repo_root = _new_repo_dir()
    try:
        norm = normalize_github_url(url)
        Repo.clone_from(norm, repo_root.as_posix(), depth=1, multi_options=["--no-tags"])
        shutil.rmtree(repo_root / ".git", ignore_errors=True)
        return repo_id, repo_root
    except Exception:
        shutil.rmtree(repo_root, ignore_errors=True)
        raise


def ingest_zip_repo(zip_path: Path) -> Tuple[str, Path, Dict[str, int]]:
    repo_id, repo_root = _new_repo_dir()
    try:
        meta = validate_and_extract_zip(zip_path, repo_root)
        return repo_id, repo_root, meta
    except Exception:
        shutil.rmtree(repo_root, ignore_errors=True)
        raise

