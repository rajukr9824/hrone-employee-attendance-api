# Design decisions

## 1. Which indexes did you create, and why?

`create_indexes()` creates unique indexes on `employees.emp_code` and `attendance_logs(emp_code, date)`. They enforce unique employee codes and one attendance record per employee/date. Duplicate-key errors return HTTP 409. Supporting indexes accelerate department, date, employee, and status filters.

## 2. What happens when two identical punch-ins arrive at the same instant?

Both requests attempt insertion for the same employee/date. The unique compound index allows only one insert; the other receives HTTP 409. This prevents race conditions caused by checking before inserting. `test_concurrent_duplicate_punch_ins` verifies one HTTP 201 and seven HTTP 409 responses.

## 3. What does the leaderboard return when there is a tie at the cutoff?

`late_leaderboard()` sums stored `late_minutes`, excludes orphan attendance logs, and sorts by total minutes and `emp_code`. Competition ranking produces `1, 2, 2, 4`. Applying the limit after ranking preserves all ties at the cutoff.

## 4. How does the department summary count employees with no logs?

`department_summary()` starts from eligible employees and uses `$lookup` with preserved empty results. Employees without attendance logs count toward headcount but not attendance metrics.

## 5. What would you change for 100 times the data?

Measure query plans, latency, examined documents, and write costs first. Then consider materialized analytics or pre-aggregated daily summaries to reduce repeated aggregation and joins. These are future improvements, not implemented features.
