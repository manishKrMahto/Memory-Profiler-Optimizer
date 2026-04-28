from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from dotenv import find_dotenv, load_dotenv

try:
    from openai import OpenAI  # type: ignore
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


@dataclass(frozen=True)
class LLMOptimization:
    optimized_code: str
    raw_text: str = ""
    error: str = ""


_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\n|\n```$", re.MULTILINE)


def _strip_code_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = _FENCE_RE.sub("", t).strip()
    return t


def optimize_function_with_llm(function_code: str, memory_data: Dict[str, Any], *, language: str = "python") -> LLMOptimization:
    """
    Input:
    - function_code: original function code (string)
    - memory_data: { memory_usage: [...], peak_memory: float, execution_time: float }

    Output:
    - improved code ONLY (no markdown), per prompt rules
    """
    # Load `.env` so API keys are available during `runserver`.
    # Use find_dotenv() so it works even if CWD differs.
    load_dotenv(find_dotenv(usecwd=True), override=False)

    api_key = (
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("OPENAI_KEY")
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("API_KEY")
        or ""
    ).strip()
    # Force the model requested by user unless explicitly overridden.
    model = (os.environ.get("OPENAI_MODEL") or os.environ.get("LLM_MODEL") or "gpt-4o-mini").strip()
    base_url = (os.environ.get("OPENAI_BASE_URL") or os.environ.get("LLM_BASE_URL") or "").strip()

    if OpenAI is None or not api_key:
        return LLMOptimization(optimized_code=function_code, error="LLM not configured (missing API key).")

    client_kwargs: Dict[str, Any] = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = OpenAI(**client_kwargs)

    lang = (language or "python").strip().lower()
    is_node = lang in {"js", "javascript", "ts", "typescript", "node"}

    rules: List[str] = [
        "Do NOT change functionality.",
        "Do NOT rename the function or change its signature.",
        "Do NOT add new third-party dependencies.",
        "Output improved code ONLY (no markdown fences, no explanations).",
        "If no safe improvement exists, output the original code unchanged.",
    ]
    if not is_node:
        rules.insert(
            3,
            "If you switch return type (e.g., list -> generator), you MUST mention it explicitly (but still output code only).",
        )

    prompt = {
        "goal": "Optimize the function for memory usage.",
        "language": "javascript/typescript" if is_node else "python",
        "rules": rules,
        "function_code": function_code,
        "profiler": {
            "memory_usage": memory_data.get("memory_usage", []),
            "peak_memory": memory_data.get("peak_memory"),
            "execution_time": memory_data.get("execution_time"),
            "error": memory_data.get("error", ""),
        },
    }

    if is_node:
        system = "You are a senior JavaScript/TypeScript performance engineer. Optimize strictly for memory and keep behavior identical."
    else:
        system = "You are a senior Python performance engineer. Optimize strictly for memory and keep behavior identical."

    try:
        resp = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(prompt)},
            ],
            temperature=0.2,
        )
        text = (resp.output_text or "").strip()
        code = _strip_code_fences(text)
        if not code:
            return LLMOptimization(optimized_code=function_code, raw_text=text, error="empty_llm_response")
        if not is_node:
            # Validate: must be syntactically valid Python (AST parseable)
            try:
                ast.parse(code)
            except SyntaxError as e:
                return LLMOptimization(
                    optimized_code=function_code,
                    raw_text=text,
                    error=f"llm_returned_invalid_python: {e}",
                )
        else:
            # JS/TS validation: lightweight checks only (avoid rejecting valid TS with type syntax).
            # Ensure the function name still exists in the returned code.
            m = re.search(r"\bfunction\s+([A-Za-z_$][A-Za-z0-9_$]*)\b", function_code or "")
            expected = m.group(1) if m else None
            if expected and expected not in code:
                return LLMOptimization(
                    optimized_code=function_code,
                    raw_text=text,
                    error="llm_returned_invalid_js: function name missing",
                )
        return LLMOptimization(optimized_code=code, raw_text=text, error="")
    except Exception as e:
        return LLMOptimization(optimized_code=function_code, error=str(e))

