# DECISIONS.md

1. **Indexes.**
   - `employees`: `{emp_code: 1}` (unique) guarantees employee uniqueness; `{department: 1, joined_on: 1}` accelerates department lookups and headcount calculations.
   - `attendance_logs`: `{emp_code: 1, date: 1}` (unique) enforces one record per employee/day and guarantees race-safe punch-ins; `{date: -1, emp_code: 1}` powers paginated attendance listings; `{emp_code: 1, punch_in: -1}` backs punch-out lookups; `{date: 1, late_minutes: 1}` powers the monthly leaderboard.
   - *Rejected:* A standalone `{date: 1}` index, because the compound index `{date: -1, emp_code: 1}` already covers date-prefixed queries without additional index write overhead.

2. **Punch-in race.**
   Both requests calculate the attendance date and call `insert_one`. Because MongoDB enforces the unique compound index on `(emp_code, date)`, the database atomically inserts one document and rejects the concurrent one with a duplicate key error (code 11000). The winner returns 201; the loser catches `DuplicateKeyError` and returns 409.

3. **Ties.**
   The pipeline computes standard competition ranks via `$setWindowFields` with `$rank` (e.g., 1, 2, 2, 4), and applies `rank <= limit` afterward. If employees tie at the cutoff rank, all tied employees are included (returning more than `limit` rows), ordered by `emp_code` ascending.

4. **Headcount.**
   The aggregation begins on `employees` (filtering `joined_on <= month_end`), grouping all active employees into departments before `$lookup`-ing their attendance logs. This ensures employees without logs remain in the pipeline and contribute to the headcount.

5. **One thing to change for 100x the data.**
   Maintain daily and monthly pre-aggregated summary collections via MongoDB change streams or background rollups, avoiding on-the-fly cross-collection aggregations over tens of millions of raw punch records.
