# Employee Attendance & Analytics API

FastAPI + MongoDB backend for the HROne attendance assignment.

## Requirements

- Python 3.11+
- MongoDB 6.0+ (I tested against 7.0 via Docker)

## Run

    python -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt

    $env:MONGO_URI = "mongodb://127.0.0.1:27017"
    $env:MONGO_DB  = "attendance_db"
    uvicorn app.main:app --port 8000

A local `.env` works too; real environment variables take priority. On Windows,
prefer `127.0.0.1` over `localhost` if Mongo runs in Docker — the IPv6 path is
unreliable.

Verify the app is up:

    curl.exe -s http://127.0.0.1:8000/health

Expected: `{"status":"ok"}`. Returns 503 within a couple of seconds if MongoDB is
unreachable, so the 20-second readiness window is never at risk.

Interactive docs: `http://127.0.0.1:8000/docs`.

## Files

- `app/main.py` — all routes, validation, calculations, MongoDB access, analytics.
- `requirements.txt`
- `REVIEW.md` — defects found in the starter and how they were fixed.
- `DECISIONS.md` — answers to the five questions.

## Notes

Indexes are created on startup, idempotently. If MongoDB is down, startup still
completes and `/health` returns 503 rather than failing the boot.

I verified all five `/admin/explain/*` endpoints against ~100k attendance rows:
every plan reports IXSCAN and no COLLSCAN.

## Verification

No automated tests are shipped. I verified the endpoints manually with curl:
CRUD flows, duplicate-punch 409s, concurrent punch-out, regularization history,
all four analytics endpoints, and the explain plans on the 100k dataset.

## What is missing

Nothing against the OpenAPI contract as far as I know.