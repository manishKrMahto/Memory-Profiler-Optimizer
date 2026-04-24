from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass(frozen=True)
class FunctionMeta:
    function_name: str  # qualified (Class.method or func)
    code: str
    start_line: int
    end_line: int
    file_path: str  # repo-relative posix path
    can_call_without_args: bool


def _can_call_without_args(node: ast.AST, in_class: bool) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False

    args = node.args
    pos_args = list(getattr(args, "posonlyargs", [])) + list(getattr(args, "args", []))
    defaults = list(getattr(args, "defaults", []))
    kwonlyargs = list(getattr(args, "kwonlyargs", []))
    kw_defaults = list(getattr(args, "kw_defaults", []))

    decorators = [
        getattr(d, "id", "")
        for d in getattr(node, "decorator_list", [])
        if isinstance(d, ast.Name)
    ]
    is_static = "staticmethod" in decorators

    # If method (inside class) and not staticmethod, drop first implicit arg (self/cls).
    if in_class and pos_args and not is_static:
        pos_args = pos_args[1:]

    required_pos = max(0, len(pos_args) - len(defaults))
    required_kwonly = sum(1 for d in kw_defaults if d is None)
    return required_pos == 0 and required_kwonly == 0


def extract_functions(file_path: str | Path, *, repo_root: str | Path | None = None) -> List[FunctionMeta]:
    """
    Parse a `.py` file with AST and extract functions with code spans.

    Returns qualified names for methods (Class.method) and plain names for top-level funcs.
    """
    p = Path(file_path).resolve()
    if not p.exists() or not p.is_file():
        return []

    rel_posix = p.name
    if repo_root is not None:
        try:
            rel_posix = p.relative_to(Path(repo_root).resolve()).as_posix()
        except Exception:
            rel_posix = p.name

    src = p.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []

    lines = src.splitlines(keepends=True)
    out: List[FunctionMeta] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.class_stack: List[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.class_stack.append(node.name)
            self.generic_visit(node)
            self.class_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._handle(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._handle(node)

        def _handle(self, node: ast.AST) -> None:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return

            in_class = bool(self.class_stack)
            qual = ".".join(self.class_stack + [node.name]) if in_class else node.name
            start = int(getattr(node, "lineno", 1))
            end = int(getattr(node, "end_lineno", start))
            code = "".join(lines[start - 1 : end])
            out.append(
                FunctionMeta(
                    function_name=qual,
                    code=code,
                    start_line=start,
                    end_line=end,
                    file_path=rel_posix,
                    can_call_without_args=_can_call_without_args(node, in_class=in_class),
                )
            )

    Visitor().visit(tree)
    return out

