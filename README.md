# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the HROne trainee assignment. All application code lives in
[`app/main.py`](app/main.py); see [`REVIEW.md`](REVIEW.md) for the defects found in the starter and
[`DECISIONS.md`](DECISIONS.md) for the design choices.

## Run

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_db   # or a local .env
python sample_seed.py                                   # optional: the nine sample documents
uvicorn app.main:app --port 8000
```

MongoDB 6.0+ (tested on 7.0). Indexes are created idempotently at startup.

## What it does

* Employees keyed by the customer's `emp_code` (unique index; duplicate creates → 409).
* Punch-in/out with rules R1-R5: IST attendance dates incl. overnight shifts, 10-minute late grace,
  30-minute overtime minimum, half-up rounding to 2 dp, half-day under 4.50 h.
* Manual regularizations append an audit entry to `history` (`$push`, so concurrent corrections
  never lose each other's history).
* Analytics (employee monthly, department summary, late leaderboard, department trend) each run as a
  **single MongoDB aggregation** over the `(date, emp_code)` index — nothing is loaded into memory.
  Standard-competition ranking, R9 headcount (zero-log employees included), and a `$densify`
  gap-filled daily trend.
* `/admin/explain/{endpoint}` runs `explain(executionStats)` on the exact query/aggregation each
  endpoint uses; all five plans are IXSCAN with no COLLSCAN.

## Verify

```bash
python test_app.py        # 111 assertions; needs MongoDB on MONGO_URI (uses MONGO_DB=attendance_test)
```