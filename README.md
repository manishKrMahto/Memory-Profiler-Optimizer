# Memory Profiler Optimizer (Django)

Production-minded Django app that:

- Uploads a **single `.py` file** into server storage
- Extracts functions via AST
- Profiles memory usage (original + optimized) in a subprocess
- Optimizes function code with an LLM (optional / configurable)
- Stores before/after results in SQLite
- Exposes JSON APIs used by the built-in UI (no frontend build step)

## What you can do in the UI

- Upload **one `.py` file**
- Browse extracted functions
- Run **profile → LLM optimize → re-profile**
- Compare before/after (peak memory + execution time + chart)
- Accept/reject an optimization and download the updated file

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Then open:

- UI: `http://127.0.0.1:8000/`

## LLM configuration

Set either OpenAI-style env vars or the generic `LLM_*` aliases:

- `OPENAI_API_KEY` (or `LLM_API_KEY`)
- `OPENAI_MODEL` (or `LLM_MODEL`) default: `gpt-4o-mini`
- `OPENAI_BASE_URL` (or `LLM_BASE_URL`) optional for OpenAI-compatible providers

You can start from `.env.example` and copy it to `.env`.

## API

All endpoints are served from the same Django server.

### Ingest (single file)

- `POST /repos/ingest/file` (multipart form field: `file`)

### Browse

- `GET /repos`
- `GET /files/<repo_id>`
- `GET /functions/<file_id>`
- `GET /function/<fn_id>`

### Optimize + decide

- `POST /optimize/<function_id>`
- `POST /function/<fn_id>/decision` body: `{ "action": "accept" | "reject" }`

### Download / visualization

- `GET /function/<fn_id>/memory-chart.png`
- `GET /file/<file_id>/download`

## Notes on safety / sandboxing

- Profiling runs in a **separate Python subprocess** with a hard timeout.
- The system only auto-profiles functions that can be called **with no arguments**.
- This is **not** a hardened sandbox (Python code can still do dangerous things if executed). For higher security, run profiling in an OS/container sandbox with strict filesystem/network controls.

## Deprecated / legacy endpoints

This repo still contains a legacy Phase1 page at `GET /phase1/`, but **GitHub ingestion has been removed** and any GitHub-based endpoints return HTTP 410.

