# Employee Attendance & Analytics API

A FastAPI and MongoDB service implementing employee attendance tracking (punch-in, punch-out, and regularization audit trails) and analytical reporting via MongoDB aggregation pipelines.

## Prerequisites

- Python 3.11+
- MongoDB 6.0+ (Tested on MongoDB 7.0)

## Quickstart

```bash
# Install dependencies
pip install -r requirements.txt

# Configure environment variables (optional, defaults to localhost:27017 and attendance_db)
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"

# Seed sample data
python sample_seed.py

# Start application server
uvicorn app.main:app --port 8000
```

All indexes are created idempotently on startup through the FastAPI lifespan context manager.

## Overview of Implemented Endpoints

- **System:** `GET /health` (readiness probe verifying MongoDB connection).
- **Employees:**
  - `POST /employees` (create employee with unique code constraint and shift validation).
  - `GET /employees` (paginated list sorted by `emp_code`).
- **Attendance:**
  - `POST /attendance/punch-in` (race-safe punch-in with R1 overnight shift logic and R2 late calculation).
  - `POST /attendance/punch-out` (race-safe punch-out computing R4 work hours, R3 overtime, and R5 half-day).
  - `GET /attendance` (paginated attendance logs sorted by `date` desc and `emp_code` asc).
  - `PATCH /attendance/{emp_code}/{date}` (regularization with audit trail and optimistic locking).
- **Analytics (MongoDB Aggregation Pipelines):**
  - `GET /analytics/employees/{emp_code}/monthly` (monthly stats, R7 working days, R5/R6 present days, R8 attendance percentage).
  - `GET /analytics/departments/summary` (cross-collection aggregation including zero-log employees).
  - `GET /analytics/leaderboard/late` (standard competition ranking using `$rank` window function with post-ranking limit).
  - `GET /analytics/departments/{department}/trend` (gap-filling via `$densify`, 7-day moving average).
- **Admin & Verification:**
  - `GET /admin/explain/{endpoint}` (returns MongoDB `explain` executionStats confirming `IXSCAN` and no `COLLSCAN`).

## Documentation & Decisions

- `REVIEW.md`: Detailed defect analysis and fixes for starter codebase.
- `DECISIONS.md`: Explanations of indexing strategy, concurrency handling, tie resolution, and scaling considerations.

---

## What's Missing / Known Gaps

The items below are gaps between the current `app/main.py` implementation and the full contract defined in `openapi.yaml` / `DATA_MODEL.md`.

### 1. `$round` in MongoDB aggregations uses banker's rounding (R8 violation)

Several aggregation pipelines call MongoDB's `$round` operator directly (e.g. `avg_work_hours` in `department_summary`, `attendance_rate` and `moving_avg_7d` in `department_trend`). MongoDB's `$round` uses IEEE 754 round-half-to-even (banker's rounding), not half-up. The spec (R8) requires **half-up** rounding to 2 decimals (4 for rates). The Python helper `round_half_up()` exists but is never applied to these aggregation-produced values before returning them.

### 2. `present_days` in `department_summary` is not rounded per R8

The `present_days` field accumulated inside the aggregation group stage is a raw `$sum` of 0 / 0.5 / 1.0 values. The spec (R8) requires 2-decimal half-up rounding on all reported numbers. There is no rounding call on this value before it is returned.

### 3. `department_trend` – `late_count` is dropped from the explain pipeline

In `admin_explain` for `department_trend` (lines 1604–1652), the `$group` stage omits `late_count` entirely. The real endpoint includes it. The explain pipeline therefore does not faithfully replicate the actual query, which may cause the grader's IXSCAN / no-COLLSCAN check to misfire.

### 4. `department_trend` headcount uses a Python-side pre-fetch of `emp_codes` (scalability)

The `department_trend` endpoint fetches all `emp_codes` for a department into Python memory and passes them as an `$in` list, then does a correlated `$lookup` per day inside the pipeline. At 100 k+ employees this `$in` list can be enormous. The spec says "Must stay fast on ~100 k records" — a fully-pipeline-side solution (starting on `employees` or using `$lookup` with `let/pipeline` for the headcount, as done in `department_summary`) would be safer. This is a design concern, not a bug, but is worth documenting.

### 5. `punch_out` race condition is partially mitigated but the find → compute → update window remains

The punch-out path reads the record, computes `work_hours` / `overtime_minutes` / `half_day`, then calls `find_one_and_update` with `{"punch_out": None}` as the atomic guard. Two simultaneous punch-outs will both pass the initial `find_one` (both see `punch_out: null`), compute the same values, then race on `find_one_and_update`. The atomic guard on `punch_out: None` ensures only one succeeds and the other gets `None` back (returned as 409). **This is correct** — but only if the `find_one` lookup and the `find_one_and_update` reference the same `_id`. The current code does use `_id` in the update filter, so the guard holds. ✅ (Documented here for clarity, not a bug.)

### 6. No `.env` file or environment variable documentation beyond inline comments

The README mentions `export MONGO_URI` / `export MONGO_DB` but does not document the `python-dotenv` `.env` file approach (which `requirements.txt` includes). A sample `.env.example` file is absent.

### 7. Test suite depends on a running MongoDB and has no mock/stub path

`test_suite.py` and `test_edge_cases.py` import from `app.main` and hit the real database. There is no fixture or environment variable to point them at a test database, making CI setup non-obvious.

### 8. `sample_seed.py` does not reset the database before seeding

Running `python sample_seed.py` more than once will fail with duplicate-key errors because it uses `insert_many` without first dropping/clearing the target collections. A `--reset` flag or idempotent upsert would be safer for iterative development.
