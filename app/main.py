"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience)
"""
import math
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
IST_OFFSET_SEC = 5 * 3600 + 30 * 60

EPOCH_MS_MIN = 100_000_000_000  # ~1973
EPOCH_MS_MAX = 4_102_444_800_000  # 2100

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}

def _create_indexes():
    # employees: unique on emp_code (enforces concurrent-create guarantee)
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")

    # attendance_logs: compound unique on (emp_code, date) for punch-in race
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="emp_date_unique",
    )
    # support analytics and list queries
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp_asc")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", ASCENDING)], name="emp_punch_in")


@asynccontextmanager
async def lifespan(app_: FastAPI):
    _create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds → UTC datetime (truncated to seconds)."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc).replace(microsecond=0)


def dt_to_ms(dt: datetime) -> int:
    """datetime → epoch milliseconds."""
    if dt is None:
        return None
    return int(dt.timestamp() * 1000)


def dt_to_ist(dt: datetime) -> datetime:
    """Naive UTC datetime → IST-aware datetime."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def attendance_date_for_punch(ts: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: determine the attendance date (IST calendar day) for a punch-in instant.

    Overnight shift: shift_end <= shift_start.
    A punch-in whose IST time is *earlier than* shift_end belongs to the shift
    that started the previous IST calendar day.
    """
    ts_ist = dt_to_ist(ts)
    is_overnight = _shift_minutes(shift_end) <= _shift_minutes(shift_start)
    if is_overnight:
        end_h, end_m = map(int, shift_end.split(":"))
        punch_minutes = ts_ist.hour * 60 + ts_ist.minute
        shift_end_minutes = end_h * 60 + end_m
        if punch_minutes < shift_end_minutes:
            # belongs to previous day's shift
            prev_day = (ts_ist - timedelta(days=1)).date()
            return prev_day.isoformat()
    return ts_ist.date().isoformat()


def _shift_minutes(t: str) -> int:
    h, m = map(int, t.split(":"))
    return h * 60 + m


def half_up(value: float, decimals: int) -> float:
    """Round half-up (not banker's rounding) to given decimal places."""
    factor = 10 ** decimals
    return math.floor(value * factor + 0.5) / factor


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if strictly more than 10 min after shift_start.
    late_minutes = floor minutes elapsed since shift_start (not since grace period end).
    """
    h, m = map(int, shift_start.split(":"))
    # Build the shift start on the same IST date as date_str
    date_ist = datetime.fromisoformat(date_str)
    shift_start_dt = datetime(
        date_ist.year, date_ist.month, date_ist.day, h, m, 0, tzinfo=IST
    )
    punch_in_ist = dt_to_ist(punch_in if punch_in.tzinfo else punch_in.replace(tzinfo=timezone.utc))
    elapsed_sec = (punch_in_ist - shift_start_dt).total_seconds()
    if elapsed_sec <= 600:  # <= 10 minutes → not late
        return 0
    return int(elapsed_sec // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) / 3600, rounded 2 decimals half-up."""
    secs = (punch_out - punch_in).total_seconds()
    return half_up(secs / 3600, 2)


def compute_overtime_minutes(punch_out: datetime, shift_end: str, date_str: str, shift_start: str) -> int:
    """
    R3: overtime = floor minutes between shift_end and punch_out, only when >= 30; else 0.
    For overnight shifts shift_end is on the next calendar day relative to date_str.
    """
    h, m = map(int, shift_end.split(":"))
    is_overnight = _shift_minutes(shift_end) <= _shift_minutes(shift_start)
    date_ist = datetime.fromisoformat(date_str)
    if is_overnight:
        # shift_end is on the next day
        shift_end_dt = datetime(
            date_ist.year, date_ist.month, date_ist.day, h, m, 0, tzinfo=IST
        ) + timedelta(days=1)
    else:
        shift_end_dt = datetime(
            date_ist.year, date_ist.month, date_ist.day, h, m, 0, tzinfo=IST
        )
    punch_out_ist = dt_to_ist(punch_out if punch_out.tzinfo else punch_out.replace(tzinfo=timezone.utc))
    elapsed = (punch_out_ist - shift_end_dt).total_seconds()
    if elapsed < 30 * 60:
        return 0
    return int(elapsed // 60)


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day = True when rounded work_hours < 4.50."""
    return work_hours < 4.50


def record_to_api(doc: dict) -> dict:
    """Convert a MongoDB attendance_log document to API response shape."""
    out = {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")) if doc.get("punch_in") else None,
        "punch_out": dt_to_ms(doc.get("punch_out")) if doc.get("punch_out") else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": _history_to_api(doc.get("history") or []),
    }
    return out


def _history_to_api(history: list) -> list:
    result = []
    for h in history:
        entry = {
            "at": dt_to_ms(h["at"]) if h.get("at") else None,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": _changes_to_api(h.get("changes", {})),
        }
        result.append(entry)
    return result


def _changes_to_api(changes: dict) -> dict:
    out = {}
    for field, val in changes.items():
        if field in ("punch_in", "punch_out"):
            from_val = val.get("from")
            to_val = val.get("to")
            out[field] = {
                "from": dt_to_ms(from_val) if isinstance(from_val, datetime) else from_val,
                "to": dt_to_ms(to_val) if isinstance(to_val, datetime) else to_val,
            }
        else:
            out[field] = val
    return out


def employee_to_api(doc: dict) -> dict:
    created_at = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(created_at) if created_at else None,
    }


def month_date_range(month: str):
    """Return (first_day_str, last_day_str) for a 'YYYY-MM' month."""
    year, mo = map(int, month.split("-"))
    first = datetime(year, mo, 1)
    if mo == 12:
        last = datetime(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = datetime(year, mo + 1, 1) - timedelta(days=1)
    return first.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d")


def is_weekday(date_str: str) -> bool:
    d = datetime.fromisoformat(date_str)
    return d.weekday() < 5  # Mon=0 … Fri=4


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────────────────────
class EmployeeCreate(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v):
        if not re.match(r"^EMP\d{4,6}$", v):
            raise ValueError("emp_code must match ^EMP\\d{4,6}$")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_time(cls, v):
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
            raise ValueError("shift time must be HH:MM")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v):
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("invalid email")
        return v

    @model_validator(mode="after")
    def validate_shift_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_epoch(cls, v):
        if v is not None:
            if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
                raise ValueError(f"punched_at must be between {EPOCH_MS_MIN} and {EPOCH_MS_MAX}")
        return v

    @field_validator("status")
    @classmethod
    def validate_presence_status(cls, v):
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {PRESENCE_STATUSES}")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_epoch(cls, v):
        if v is not None:
            if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
                raise ValueError(f"punched_at must be between {EPOCH_MS_MIN} and {EPOCH_MS_MAX}")
        return v


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch(cls, v):
        if v is not None:
            if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
                raise ValueError(f"epoch ms must be between {EPOCH_MS_MIN} and {EPOCH_MS_MAX}")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {ALL_STATUSES}")
        return v

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v):
        if len(v) < 5 or len(v) > 200:
            raise ValueError("reason must be 5-200 characters")
        return v

    @field_validator("regularized_by")
    @classmethod
    def validate_regularized_by(cls, v):
        if len(v) < 1 or len(v) > 50:
            raise ValueError("regularized_by must be 1-50 characters")
        return v


# ─────────────────────────────────────────────────────────────────────────────
# /health
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, detail="MongoDB unavailable")

