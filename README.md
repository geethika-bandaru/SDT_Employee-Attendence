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

