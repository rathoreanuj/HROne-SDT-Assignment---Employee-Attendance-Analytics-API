"""
Tests for app/main.py — run with:  pytest tests/test_main.py -v
Requires: pip install httpx pytest
Requires a live MongoDB at MONGO_URI (default localhost:27017) with a test database.
The tests use a dedicated test database and clean up after themselves.
"""
import math
import os
import pathlib
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
import pytest

# Ensure project root is in sys.path
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# ─────────────────────────────────────────────────────────────────────────────
# Unit tests for helpers (no MongoDB needed)
# ─────────────────────────────────────────────────────────────────────────────
# We import helpers directly after patching env vars so MongoClient connects lazily.
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("MONGO_DB", "attendance_test_db")

from app.main import (
    half_up,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime_minutes,
    compute_half_day,
    attendance_date_for_punch,
    ms_to_dt,
    dt_to_ms,
)

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def ist(y, mo, d, h, m, s=0) -> datetime:
    return datetime(y, mo, d, h, m, s, tzinfo=IST)


def utc(y, mo, d, h, m, s=0) -> datetime:
    return datetime(y, mo, d, h, m, s, tzinfo=UTC)


class TestHalfUp:
    def test_basic(self):
        assert half_up(9.125, 2) == 9.13  # differs from banker's rounding
        assert half_up(9.135, 2) == 9.14
        assert half_up(3.5, 0) == 4
        assert half_up(2.5, 0) == 3

    def test_zero(self):
        assert half_up(0.0, 2) == 0.0

    def test_four_decimals(self):
        assert half_up(0.12345, 4) == 0.1235


class TestLateMinutes:
    """R2: grace = 10 min, strictly more than 10 → late."""

    def test_not_late_early(self):
        # 09:28 IST, shift 09:30 — early, not late
        pi = utc(2026, 7, 6, 3, 58)  # 09:28 IST
        assert compute_late_minutes(pi, "09:30", "2026-07-06") == 0

    def test_not_late_exactly_10_min(self):
        # 09:40:00 IST → exactly 10 min → NOT late (R2 says strictly more than 10:00)
        pi = utc(2026, 7, 6, 4, 10)  # 09:40 IST
        assert compute_late_minutes(pi, "09:30", "2026-07-06") == 0

    def test_late_at_10_min_1_sec(self):
        # 09:40:01 IST → 10 min 1 sec → late_minutes = 10 (floor of 10.017 min)
        pi = utc(2026, 7, 6, 4, 10, 1)  # 09:40:01 IST
        assert compute_late_minutes(pi, "09:30", "2026-07-06") == 10

    def test_late_35_min(self):
        # 10:05 IST, shift 09:30 → 35 min late
        pi = utc(2026, 7, 6, 4, 35)  # 10:05 IST
        assert compute_late_minutes(pi, "09:30", "2026-07-06") == 35

    def test_late_45_min(self):
        # 10:15:59 IST → floor(45.98) = 45
        pi = utc(2026, 7, 6, 4, 45, 59)  # 10:15:59 IST
        assert compute_late_minutes(pi, "09:30", "2026-07-06") == 45


class TestWorkHours:
    """R4: half-up rounding to 2 decimals."""

    def test_normal_day(self):
        # 03:58 UTC to 13:05 UTC = 9h 7m = 9.1167 → half-up 9.12
        pi = utc(2026, 7, 6, 3, 58)
        po = utc(2026, 7, 6, 13, 5)
        assert compute_work_hours(pi, po) == 9.12

    def test_half_day(self):
        # 04:00 to 07:30 = 3h 30m = 3.5
        pi = utc(2026, 7, 7, 4, 0)
        po = utc(2026, 7, 7, 7, 30)
        assert compute_work_hours(pi, po) == 3.5

    def test_overnight(self):
        # 16:25 UTC to 01:10+1 UTC = 8h 45m = 8.75
        pi = utc(2026, 7, 6, 16, 25)
        po = utc(2026, 7, 7, 1, 10)
        assert compute_work_hours(pi, po) == 8.75


class TestOvertimeMinutes:
    """R3: >= 30 minutes after shift_end only."""

    def test_no_overtime_under_30(self):
        # Punch out 5 min after shift end — not overtime
        po = utc(2026, 7, 6, 13, 5)  # 18:35 IST, shift ends 18:30
        assert compute_overtime_minutes(po, "18:30", "2026-07-06", "09:30") == 0

    def test_overtime_exactly_30(self):
        # Punch out exactly 30 min after shift end → 30
        po = utc(2026, 7, 6, 13, 0)  # 18:30 IST → shift end
        po30 = utc(2026, 7, 6, 13, 30)  # 19:00 IST
        assert compute_overtime_minutes(po30, "18:30", "2026-07-06", "09:30") == 30

    def test_overtime_40_min(self):
        # 19:10 IST, shift ends 18:30 → 40 min overtime
        po = utc(2026, 7, 6, 13, 40)  # 19:10 IST
        assert compute_overtime_minutes(po, "18:30", "2026-07-06", "09:30") == 40

    def test_overnight_overtime(self):
        # Shift 22:00-06:00. Punch out 06:40 IST on the 7th → 40 min overtime
        po = utc(2026, 7, 7, 1, 10)  # 06:40 IST
        assert compute_overtime_minutes(po, "06:00", "2026-07-06", "22:00") == 40


class TestHalfDay:
    """R5: half_day when work_hours < 4.50."""

    def test_half_day_true(self):
        assert compute_half_day(3.5) is True
        assert compute_half_day(4.49) is True

    def test_half_day_false(self):
        assert compute_half_day(4.5) is False
        assert compute_half_day(9.0) is False


class TestAttendanceDate:
    """R1: IST date, overnight shift handling."""

    def test_normal_shift(self):
        # 03:58 UTC = 09:28 IST on 2026-07-06
        ts = utc(2026, 7, 6, 3, 58)
        assert attendance_date_for_punch(ts, "09:30", "18:30") == "2026-07-06"

    def test_overnight_punch_after_midnight_ist(self):
        # Shift 22:00-06:00. Punch at 01:10 IST (prev day 19:40 UTC+prev)
        # 01:10 IST on 7th = 2026-07-06T19:40Z → IST time 01:10, < shift_end 06:00 → belongs to 7th-1 = 6th
        ts = utc(2026, 7, 7, 1, 10)  # 06:40 IST on 7th
        # 06:40 IST >= 06:00 shift_end → NOT early part, belongs to 7th's shift? No.
        # Actually 06:40 >= shift_end 06:00 → punch_minutes=400 >= shift_end_minutes=360 → NOT < shift_end → date = 7th
        assert attendance_date_for_punch(ts, "22:00", "06:00") == "2026-07-07"

    def test_overnight_punch_before_shift_end(self):
        # Shift 22:00-06:00. Punch at 01:10 IST on the 7th (00:40 UTC on 7th? No...)
        # 01:10 IST on 7th = 7th 01:10 IST. shift_end=06:00. 01:10 < 06:00 → belongs to 6th
        ts = utc(2026, 7, 6, 19, 40)  # = 2026-07-07 01:10 IST
        assert attendance_date_for_punch(ts, "22:00", "06:00") == "2026-07-06"

    def test_overnight_punch_in_main_window(self):
        # 21:55 IST on 6th = shift starts. punch at 16:25 UTC = 21:55 IST 6th
        ts = utc(2026, 7, 6, 16, 25)  # 21:55 IST
        assert attendance_date_for_punch(ts, "22:00", "06:00") == "2026-07-06"


class TestEpochConversions:
    def test_ms_to_dt_truncates(self):
        # 1751776680999 ms → same as 1751776680000 ms (truncated to second)
        dt = ms_to_dt(1751776680999)
        assert dt.microsecond == 0
        assert dt.second == 0  # 1751776680000 is at :00 sec

    def test_round_trip(self):
        ms = 1751776680000
        dt = ms_to_dt(ms)
        assert dt_to_ms(dt) == ms
