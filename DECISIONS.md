# DECISIONS.md

1. **Indexes.**
   - Unique index on `employees.emp_code` guarantees uniqueness and turns creation races into 409s.
   - Unique compound index on `attendance_logs (emp_code: 1, date: 1)` prevents duplicate day punches atomically.
   - `attendance_logs (date: -1, emp_code: 1)`, `(emp_code: 1, date: -1)`, and `(status: 1, date: -1, emp_code: 1)` serve paginated list queries directly from the index tree without in-memory SORT.
   - `attendance_logs (emp_code: 1, punch_in: -1)` accelerates punch-out lookups.
   - `employees (department: 1, joined_on: 1)` accelerates department headcount checks.
   - *Rejected:* Ascending `(date: 1, emp_code: 1)` because B-tree index traversal cannot reverse direction on just one compound key (`date DESC, emp_code ASC`).

2. **Punch-in race.**
   Both requests run `insert_one` against the unique `(emp_code, date)` index. The first caller commits and returns 201; the concurrent duplicate triggers a `DuplicateKeyError`, which is caught and returned as HTTP 409. Check-then-insert is avoided.

3. **Ties.**
   The aggregation uses `$setWindowFields` with `$rank` on `total_late_minutes DESC` (competition rank 1, 2, 2, 4) followed by `$match: {rank: {$lte: limit}}`. When multiple employees tie at or below the cutoff rank, all are included (the returned list may exceed `limit`). Rows are sorted by total late minutes descending, then `emp_code` ascending.

4. **Headcount.**
   The pipeline queries `employees` first (`joined_on <= month_end`), performing a `$lookup` into `attendance_logs` and an `$unwind` with `preserveNullAndEmptyArrays: true`. Employees with zero logs are retained in the pipeline and counted toward headcount.

5. **One thing to change for 100x scale.**
   Maintain daily pre-aggregated department summaries using MongoDB Change Streams or a nightly rollup collection, removing runtime `$lookup` across hundreds of millions of attendance logs.
