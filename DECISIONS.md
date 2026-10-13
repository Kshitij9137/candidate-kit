# DECISIONS.md

**Indexes.** On `employees`: unique `emp_code` for the code contract and the 409 on
duplicate creates, plus `department` and `joined_on` for the summary and trend
lookups. On `attendance_logs`: unique `(emp_code, date)` for one-record-per-day and
the race-safe punch-in, `(date desc, emp_code asc)` for the list sort, and
`(date, status)` for month filters. No index on `history` — it's read with its parent.

**Punch-in race.** Both requests go straight to `insert_one`. The unique
`(emp_code, date)` index lets one through; the loser gets `DuplicateKeyError`, which
I map to 409. No find-then-insert, so no window between check and write. If the
index can't be created at startup, the app still boots but `/health` returns 503,
so the failure is visible immediately.

**Ties.** `$setWindowFields` sorts by `total_late_minutes` and uses `$rank`, so
equal totals share a rank and the next rank is skipped. I filter `rank <= limit`
*after* ranking, so a tie at the cutoff returns everyone in it. Final order is
`total_late_minutes` desc, then `emp_code` asc.

**Headcount.** The summary aggregation starts on `employees`, matches
`joined_on <= month_end`, then `$lookup` collapses each employee's logs into one
small stats doc. An employee with no logs gets a zero-stats doc, so they're counted;
a later joiner is filtered out before the lookup.

**100x data.** Shard `attendance_logs` on `emp_code`, write a monthly rollup
collection from a change stream, cache the trend per department, and leave the raw
collection serving only writes and single-employee reads.