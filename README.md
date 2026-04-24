# Memory Profiler Optimizer (Django)

Production-minded Django app that:

- Ingests a GitHub repo or a `.zip` upload into server storage
- Discovers `.py` files (with safe exclusions)
- Extracts functions via AST
- Profiles memory usage (original + optimized) in a subprocess
- Optimizes function code with an LLM (configurable)
- Stores before/after results in SQLite
- Exposes JSON APIs used by the frontend (added next)

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

## LLM configuration

Set either OpenAI-style env vars or the generic `LLM_*` aliases:

- `OPENAI_API_KEY` (or `LLM_API_KEY`)
- `OPENAI_MODEL` (or `LLM_MODEL`) default: `gpt-4.1-mini`
- `OPENAI_BASE_URL` (or `LLM_BASE_URL`) optional for OpenAI-compatible providers

You can start from `.env.example` and copy it to `.env`.

## API

See `docs/API.md`.

## Notes on safety / sandboxing

- Profiling runs in a **separate Python subprocess** with a hard timeout.
- The system only auto-profiles functions that can be called **with no arguments**.
- This is **not** a hardened sandbox (Python code can still do dangerous things if executed). For higher security, run profiling in an OS/container sandbox with strict filesystem/network controls.

