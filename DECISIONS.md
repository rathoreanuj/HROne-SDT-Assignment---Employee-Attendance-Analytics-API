# DECISIONS.md

1. **Indexes.**
   - `employees.emp_code` — unique index. Enforces the one-creation guarantee atomically; removes the starter's check-then-insert race condition. Concurrent duplicate creates receive exactly one 201 and the rest 409 via `DuplicateKeyError`.
   - `employees.(department, joined_on)` — supports department-filtered headcount queries and the department summary's `joined_on <= last_day` filter in one index scan.
   - `attendance_logs.(emp_code, date)` — unique compound index. Enables O(1) punch-in duplicate detection atomically; also used by the regularize lookup and employee-monthly aggregation.
   - `attendance_logs.(date DESC, emp_code ASC)` — matches the sort required by `GET /attendance` so the cursor never falls back to a COLLSCAN.
   - `attendance_logs.(emp_code, punch_in ASC)` — supports the punch-out query that finds the most recent open record `punch_in <= ts_out`.
   - Considered but rejected: a separate index on `attendance_logs.status` — selectivity is low (only 5 values), and status is almost always used together with `date` or `emp_code` where the compound index already applies.

2. **Punch-in race.**
   Two identical punch-in requests arrive at the same instant. Both pass employee lookup and compute the same `date_str`. Both attempt `insert_one` with the same `(emp_code, date)` pair. MongoDB's unique compound index allows exactly one insert to succeed; the other raises `DuplicateKeyError`, which the handler catches and converts to HTTP 409. No application-level lock or two-step check is needed; the database enforces the invariant atomically.

3. **Ties.**
   The leaderboard uses MongoDB's `$rank` window function, which implements standard competition ranking (1, 2, 2, 4). After computing ranks, the pipeline filters `rank <= limit`. If two employees are tied at exactly the cutoff rank, both are included in the response — the result set can therefore contain more than `limit` rows, which is what the spec requires. Rows are ordered by `total_late_minutes` descending, then `emp_code` ascending, matching the spec's ordering requirement.

4. **Headcount with zero logs.**
   The department summary aggregation starts from the `employees` collection (not `attendance_logs`), uses `$lookup` to left-join logs, and groups by department counting distinct `emp_code` values with `$addToSet`. An employee who joined on or before the last day of the month appears in the initial `$match` and contributes to the headcount even if the `$lookup` returns no log documents at all.

5. **One change for 100x data.**
   The department trend's per-day headcount query currently runs one `count_documents` per calendar day (up to 92 queries). At 100x data volume this would be the bottleneck. I would replace it with a single aggregation over `employees` that counts, per day in the range, how many employees have `joined_on <= day`. This can be done with `$bucketAuto` or by pre-grouping join dates and computing a cumulative sum, eliminating the per-day round-trips entirely.
