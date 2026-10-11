from datetime import datetime, timezone

from app.main import (
    IST,
    compute_half_day,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
)


def test_late_grace_boundary_uses_strict_seconds_and_floor_minutes():
    shift_start = "09:30"
    assert compute_late_minutes(
        datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc),
        shift_start,
    ) == 0
    assert compute_late_minutes(
        datetime(2026, 7, 6, 4, 10, tzinfo=timezone.utc),
        shift_start,
    ) == 0
    assert compute_late_minutes(
        datetime(2026, 7, 6, 4, 10, 1, 900_000, tzinfo=timezone.utc),
        shift_start,
    ) == 10


def test_helpers_apply_ist_and_overnight_shift_date():
    punch_in = datetime(2026, 7, 7, 0, 29, tzinfo=timezone.utc)
    assert compute_late_minutes(
        punch_in,
        "22:00",
        "06:00",
        "2026-07-06",
    ) == 479


def test_overtime_requires_30_full_minutes_and_handles_midnight():
    shift_end = "18:30"
    assert compute_overtime(
        datetime(2026, 7, 7, 13, 29, tzinfo=timezone.utc),
        shift_end,
        "2026-07-07",
    ) == 0
    assert compute_overtime(
        datetime(2026, 7, 7, 13, 30, tzinfo=timezone.utc),
        shift_end,
        "2026-07-07",
    ) == 30
    assert compute_overtime(
        datetime(2026, 7, 7, 1, 10, tzinfo=timezone.utc),
        "06:00",
        "2026-07-06",
        "22:00",
    ) == 40


def test_work_hours_uses_half_up_rounding_and_half_day_threshold():
    punch_in = datetime(2026, 7, 6, 0, 0, 0, 900_000, tzinfo=timezone.utc)
    punch_out = datetime(2026, 7, 6, 4, 30, 1, 100_000, tzinfo=timezone.utc)
    assert compute_work_hours(punch_in, punch_out) == 4.5
    assert compute_half_day(4.5) is False
    assert compute_half_day(4.49) is True

    half_up_in = datetime(2026, 7, 6, 0, 0, tzinfo=IST)
    half_up_out = datetime(2026, 7, 6, 1, 0, 18, 600_000, tzinfo=IST)
    assert compute_work_hours(half_up_in, half_up_out) == 1.01
