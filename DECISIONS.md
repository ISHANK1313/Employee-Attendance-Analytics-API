# DECISIONS.md

**1. Indexes.** `employees.emp_code` (unique) backs employee lookup and the duplicate-create guarantee; `employees.(department, joined_on)` backs the R9 headcount filter (join date ≤ month end) and department listing. `attendance_logs.(emp_code, date)` is unique — it makes punch-in idempotent per day and is the race guard — and serves `employee_monthly` and punch-out lookup. `attendance_logs.(date, emp_code)` serves `/attendance` (date desc, emp_code asc) and every month-range aggregation (`department_summary`, `late_leaderboard`, `department_trend`). Rejected: a separate `(emp_code)` index on attendance — the compound `(emp_code, date)` already covers emp-code prefixes. `_id` is never used to identify anything.

**2. Punch-in race.** Both requests pass the `find_one` check. Each inserts a document with the same `(emp_code, date)`. The unique index lets exactly one insert succeed (201); the other raises `DuplicateKeyError`, which is caught and returned as 409. No check-then-insert window.

**3. Ties.** Ranking is standard competition ranking computed over the *full* sorted set before `limit` is applied (walking the ordered rows, bumping the rank only when the total changes). Everyone whose rank ≤ `limit` is returned, so a tie at the cutoff returns every tied employee, and the next rank is skipped. Rows are ordered by total then `emp_code`.

**4. Headcount.** Counted from `employees` (where `joined_on` ≤ month end), not from logs, grouped by department and left-joined to the log aggregation. Departments with zero logs still appear; only departments with headcount 0 for the month are dropped.

**5. At 100x.** Precompute per-day attendance into a summary collection (nightly rollup), so the month aggregations stop scanning raw punches; and shard/partition `attendance_logs` on `date`. The trend's `$densify` stays cheap, but the department `$lookup` should move to a pre-joined field.