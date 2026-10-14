# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the HROne assignment. All application code is in `app/main.py`.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"   # or put both in a local .env
export MONGO_DB="attendance_db"
python sample_seed.py                           # optional: the nine sample documents
uvicorn app.main:app --port 8000
```

MongoDB 6.0+ is required. Indexes are created at startup (idempotent) and re-attempted from `/health` if MongoDB was
not reachable when the app started. Real environment variables take priority over `.env`.

## Layout of `app/main.py`

`Clock` (epoch-ms conversion, half-up rounding), `Shift` (IST shift window, overnight rule R1), `Rules` (R2-R5),
Pydantic request models, `Serializer`, `Store` (connection, indexes), `Attendance` (punch-in/out, regularisation),
`Analytics` (the aggregation pipelines, shared with `/admin/explain`), `Explainer`, then the routes.

## Assumptions

* The trend range limit counts days inclusively: `from`..`to` may cover at most 92 rows (a full quarter is allowed).
* When `punched_at` is omitted, the server clock (UTC) is used, truncated to whole seconds like every other instant.
* Naive datetimes coming from MongoDB are treated as UTC.
* `?department=` (empty string) is an exact filter for an empty department name, not "no filter".

## Verified

Checked against the sample data and a 100,000-record synthetic dataset: all business rules, status codes, the three
race conditions, and `/admin/explain/*` plans (IXSCAN, no COLLSCAN) for every endpoint. Nothing from the contract is
missing.
