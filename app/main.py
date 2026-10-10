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


# ─────────────────────────────────────────────────────────────────────────────
# Employees
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    doc.pop("_id", None)
    return employee_to_api(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    return {
        "items": [employee_to_api(e) for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.get("/employees/{emp_code}")
def get_employee(emp_code: str):
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, detail="Employee not found")
    return employee_to_api(emp)


# ─────────────────────────────────────────────────────────────────────────────
# Attendance – punch-in
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    # R1: truncate to whole seconds
    if body.punched_at is not None:
        ts = ms_to_dt(body.punched_at)
    else:
        ts = datetime.now(tz=timezone.utc).replace(microsecond=0)

    date_str = attendance_date_for_punch(ts, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(ts, emp["shift_start"], date_str)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="Already punched in for this attendance date")

    doc.pop("_id", None)
    return record_to_api(doc)


# ─────────────────────────────────────────────────────────────────────────────
# Attendance – punch-out
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    # R1: truncate to whole seconds
    if body.punched_at is not None:
        ts_out = ms_to_dt(body.punched_at)
    else:
        ts_out = datetime.now(tz=timezone.utc).replace(microsecond=0)

    # Find the most recent open record whose punch_in <= ts_out
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "status": {"$in": list(PRESENCE_STATUSES)},
            "punch_in": {"$lte": ts_out},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        # Check if there's a record that's already punched out
        closed = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "status": {"$in": list(PRESENCE_STATUSES)},
                "punch_in": {"$lte": ts_out},
                "punch_out": {"$ne": None},
            },
            sort=[("punch_in", DESCENDING)],
        )
        if closed:
            raise HTTPException(409, detail="Record already punched out")
        raise HTTPException(404, detail="No open punch-in found for this employee")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # 422 validations
    if ts_out <= punch_in_dt:
        raise HTTPException(422, detail="punched_at must be after punch_in")
    if (ts_out - punch_in_dt).total_seconds() > 86400:
        raise HTTPException(422, detail="punched_at must be within 24 hours of punch_in")

    wh = compute_work_hours(punch_in_dt, ts_out)
    ot = compute_overtime_minutes(ts_out, emp["shift_end"], record["date"], emp["shift_start"])
    hd = compute_half_day(wh)

    # Atomic update: only update if punch_out is still null (concurrency)
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts_out, "work_hours": wh, "overtime_minutes": ot, "half_day": hd}},
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, detail="Record already punched out by a concurrent request")

    return record_to_api(result)


# ─────────────────────────────────────────────────────────────────────────────
# Attendance – list
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if status and status not in ALL_STATUSES:
        raise HTTPException(422, detail=f"status must be one of {ALL_STATUSES}")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must not be after date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [record_to_api(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Attendance – regularize (PATCH)
# ─────────────────────────────────────────────────────────────────────────────
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, detail="Attendance record not found")

    new_status = body.status if body.status is not None else record["status"]
    new_pi_ms = body.punch_in
    new_po_ms = body.punch_out

    # Validate: ABSENT/LEAVE cannot have punch times
    if new_status in ("ABSENT", "LEAVE"):
        if new_pi_ms is not None or new_po_ms is not None:
            raise HTTPException(422, detail="ABSENT/LEAVE status cannot have punch_in or punch_out")

    # Resolve final punch_in
    if new_status in ("ABSENT", "LEAVE"):
        final_pi = None
        final_po = None
    else:
        # presence status requires punch_in
        if new_pi_ms is not None:
            final_pi = ms_to_dt(new_pi_ms)
        elif record.get("punch_in"):
            pi = record["punch_in"]
            final_pi = pi if pi.tzinfo else pi.replace(tzinfo=timezone.utc)
        else:
            raise HTTPException(422, detail="Presence status requires a punch_in")

        if new_po_ms is not None:
            final_po = ms_to_dt(new_po_ms)
        elif record.get("punch_out"):
            po = record["punch_out"]
            final_po = po if po.tzinfo else po.replace(tzinfo=timezone.utc)
        else:
            final_po = None

    # Validate punch_in date stays on the attendance date (R1)
    if final_pi is not None:
        pi_date = attendance_date_for_punch(final_pi, emp["shift_start"], emp["shift_end"])
        if pi_date != date:
            raise HTTPException(422, detail="punch_in must remain on the record's attendance date")

    # Validate punch_out is after punch_in and within 24 hours
    if final_pi is not None and final_po is not None:
        if final_po <= final_pi:
            raise HTTPException(422, detail="punch_out must be after punch_in")
        if (final_po - final_pi).total_seconds() > 86400:
            raise HTTPException(422, detail="punch_out must be within 24 hours of punch_in")

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        final_late = 0
        final_wh = None
        final_ot = 0
        final_hd = False
    elif final_pi is not None and final_po is not None:
        final_late = compute_late_minutes(final_pi, emp["shift_start"], date)
        final_wh = compute_work_hours(final_pi, final_po)
        final_ot = compute_overtime_minutes(final_po, emp["shift_end"], date, emp["shift_start"])
        final_hd = compute_half_day(final_wh)
    elif final_pi is not None:
        final_late = compute_late_minutes(final_pi, emp["shift_start"], date)
        final_wh = None
        final_ot = 0
        final_hd = False
    else:
        final_late = 0
        final_wh = None
        final_ot = 0
        final_hd = False

    # Build changes dict — only fields that actually changed
    old_pi = record.get("punch_in")
    if old_pi and old_pi.tzinfo is None:
        old_pi = old_pi.replace(tzinfo=timezone.utc)
    old_po = record.get("punch_out")
    if old_po and old_po.tzinfo is None:
        old_po = old_po.replace(tzinfo=timezone.utc)

    changes = {}
    if new_status != record["status"]:
        changes["status"] = {"from": record["status"], "to": new_status}
    if final_pi != old_pi:
        changes["punch_in"] = {
            "from": dt_to_ms(old_pi) if old_pi else None,
            "to": dt_to_ms(final_pi) if final_pi else None,
        }
    if final_po != old_po:
        changes["punch_out"] = {
            "from": dt_to_ms(old_po) if old_po else None,
            "to": dt_to_ms(final_po) if final_po else None,
        }
    old_wh = record.get("work_hours")
    if final_wh != old_wh:
        changes["work_hours"] = {"from": old_wh, "to": final_wh}
    old_late = record.get("late_minutes", 0)
    if final_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": final_late}
    old_ot = record.get("overtime_minutes", 0)
    if final_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": final_ot}
    old_hd = record.get("half_day", False)
    if final_hd != old_hd:
        changes["half_day"] = {"from": old_hd, "to": final_hd}

    if not changes:
        raise HTTPException(422, detail="Request changes nothing")

    history_entry = {
        "at": datetime.now(tz=timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": _changes_from_api(changes),
    }

    set_fields: Dict[str, Any] = {
        "status": new_status,
        "punch_in": final_pi,
        "punch_out": final_po,
        "work_hours": final_wh,
        "late_minutes": final_late,
        "overtime_minutes": final_ot,
        "half_day": final_hd,
    }

    # Atomic: compare-and-swap on history length to detect concurrent edits
    current_history_len = len(record.get("history") or [])
    result = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, current_history_len]},
        },
        {"$set": set_fields, "$push": {"history": history_entry}},
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, detail="Concurrent edit detected; please retry")

    return record_to_api(result)


def _changes_from_api(changes: dict) -> dict:
    """Convert API-form changes (ms ints) to MongoDB-form (datetimes for punch fields)."""
    out = {}
    for field, val in changes.items():
        if field in ("punch_in", "punch_out"):
            from_val = val["from"]
            to_val = val["to"]
            out[field] = {
                "from": ms_to_dt(from_val) if isinstance(from_val, int) else from_val,
                "to": ms_to_dt(to_val) if isinstance(to_val, int) else to_val,
            }
        else:
            out[field] = val
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Analytics – employee monthly
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    first_day, last_day = month_date_range(month)
    # R7: working days counted from joined_on
    effective_start = max(first_day, emp["joined_on"])

    # Count working days (Mon-Fri) in [effective_start, last_day]
    working_days = _count_working_days(effective_start, last_day)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$eq": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 0]},  # placeholder
                                ]
                            },
                            0,
                            {
                                "$cond": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {
                                        "$cond": [
                                            {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                            0.5,
                                            1.0,
                                        ]
                                    },
                                    0,
                                ]
                            },
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    # Present_days: R7 only Mon-Fri. Build it in Python from aggregation over individual docs.
    # More correct: do per-doc aggregation with dayOfWeek
    pipeline2 = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        {
            "$addFields": {
                "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                # $dayOfWeek: 1=Sun, 2=Mon, ..., 6=Fri, 7=Sat
                "is_weekday": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                    ]
                },
                "is_presence": {"$in": ["$status", list(PRESENCE_STATUSES)]},
                "half_day_val": {"$ifNull": ["$half_day", False]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_presence", "$is_weekday"]},
                            {"$cond": ["$half_day_val", 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    rows = list(db.attendance_logs.aggregate(pipeline2))
    if rows:
        r = rows[0]
        present_days = half_up(r["present_days"], 2)
        leave_days = r["leave_days"]
        late_count = r["late_count"]
        total_late_minutes = r["total_late_minutes"]
        total_overtime_minutes = r["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = half_up(present_days / working_days * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


def _count_working_days(from_str: str, to_str: str) -> int:
    """Count Mon-Fri days between from_str and to_str inclusive."""
    from_d = datetime.fromisoformat(from_str).date()
    to_d = datetime.fromisoformat(to_str).date()
    if from_d > to_d:
        return 0
    count = 0
    current = from_d
    while current <= to_d:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


# ─────────────────────────────────────────────────────────────────────────────
# Analytics – department summary
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    first_day, last_day = month_date_range(month)

    # R9: headcount = employees with joined_on <= last_day of month
    emp_match: Dict[str, Any] = {"joined_on": {"$lte": last_day}}
    if department:
        emp_match["department"] = department

    # Aggregate headcount and logs per department
    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$ec"]},
                            "date": {"$gte": first_day, "$lte": last_day},
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$addFields": {
                "log_status": {"$ifNull": ["$logs.status", None]},
                "log_date": {"$ifNull": ["$logs.date", None]},
                "log_late": {"$ifNull": ["$logs.late_minutes", 0]},
                "log_ot": {"$ifNull": ["$logs.overtime_minutes", 0]},
                "log_wh": "$logs.work_hours",
                "log_half": {"$ifNull": ["$logs.half_day", False]},
                "is_presence": {
                    "$in": [{"$ifNull": ["$logs.status", ""]}, list(PRESENCE_STATUSES)]
                },
                "is_weekday_log": {
                    "$cond": [
                        {"$eq": ["$logs.date", None]},
                        False,
                        {
                            "$and": [
                                {
                                    "$gte": [
                                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                                        2,
                                    ]
                                },
                                {
                                    "$lte": [
                                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                                        6,
                                    ]
                                },
                            ]
                        },
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_presence", "$is_weekday_log"]},
                            {"$cond": ["$log_half", 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "wh_values": {
                    "$push": {
                        "$cond": [
                            {"$and": ["$is_presence", {"$ne": ["$log_wh", None]}]},
                            "$log_wh",
                            "$$REMOVE",
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$log_late", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$log_late"},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$log_status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$log_status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$headcount"},
                "present_days": 1,
                "wh_values": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": ASCENDING}},
    ]

    rows = list(db.employees.aggregate(pipeline))
    items = []
    for r in rows:
        wh_vals = [v for v in r.get("wh_values", []) if v is not None]
        avg_wh = half_up(sum(wh_vals) / len(wh_vals), 2) if wh_vals else None
        items.append(
            {
                "department": r["department"],
                "headcount": r["headcount"],
                "present_days": half_up(r["present_days"], 2),
                "avg_work_hours": avg_wh,
                "late_count": r["late_count"],
                "total_late_minutes": r["total_late_minutes"],
                "leave_count": r["leave_count"],
                "on_duty_count": r["on_duty_count"],
            }
        )

    return {"month": month, "items": items}


# ─────────────────────────────────────────────────────────────────────────────
# Analytics – late leaderboard
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    first_day, last_day = month_date_range(month)

    # Join attendance_logs with employees, filter by department if needed
    emp_match: Dict[str, Any] = {}
    if department:
        emp_match["department"] = department

    pipeline = [
        # Start from attendance_logs for the month with late_minutes > 0
        {
            "$match": {
                "date": {"$gte": first_day, "$lte": last_day},
                "late_minutes": {"$gt": 0},
            }
        },
        # Lookup employee info
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp_info",
            }
        },
        {"$unwind": "$emp_info"},
    ]

    if department:
        pipeline.append({"$match": {"emp_info.department": department}})

    pipeline += [
        {
            "$group": {
                "_id": "$emp_code",
                "name": {"$first": "$emp_info.name"},
                "department": {"$first": "$emp_info.department"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        # Standard competition ranking
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {},
                    }
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


# ─────────────────────────────────────────────────────────────────────────────
# Analytics – department trend
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    # Validate range
    if to < from_:
        raise HTTPException(422, detail="'to' must not be before 'from'")
    from_d = datetime.fromisoformat(from_)
    to_d = datetime.fromisoformat(to)
    if (to_d - from_d).days > 92:
        raise HTTPException(422, detail="Date range must not exceed 92 days")

    # Check department exists
    dept_count = db.employees.count_documents({"department": department})
    if dept_count == 0:
        raise HTTPException(404, detail="Department not found")

    # Use $densify to get one row per calendar day in [from_, to]
    pipeline = [
        # Match logs for the department's employees in date range
        {
            "$match": {
                "date": {"$gte": from_, "$lte": to},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp_info",
            }
        },
        {"$unwind": "$emp_info"},
        {"$match": {"emp_info.department": department}},
        {
            "$addFields": {
                "is_presence": {"$in": ["$status", list(PRESENCE_STATUSES)]},
                "half_day_val": {"$ifNull": ["$half_day", False]},
                "date_as_date": {"$dateFromString": {"dateString": "$date"}},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count_raw": {
                    "$sum": {
                        "$cond": [
                            "$is_presence",
                            {"$cond": ["$half_day_val", 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "date_as_date": {"$first": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "date_as_date": 1,
                "present_count_raw": 1,
                "late_count": 1,
            }
        },
        # Densify to fill gaps
        {
            "$densify": {
                "field": "date_as_date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.fromisoformat(from_).replace(tzinfo=timezone.utc),
                        datetime.fromisoformat(to).replace(tzinfo=timezone.utc) + timedelta(days=1),
                    ],
                },
            }
        },
        {
            "$addFields": {
                "date": {
                    "$ifNull": [
                        "$date",
                        {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": "$date_as_date",
                            }
                        },
                    ]
                },
                "present_count_raw": {"$ifNull": ["$present_count_raw", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$sort": {"date": ASCENDING}},
    ]

    # Execute to get per-day log counts
    log_rows = {r["date"]: r for r in db.attendance_logs.aggregate(pipeline)}

    # Build per-day headcount from employees
    # headcount(day) = employees with department=department and joined_on <= day
    # We do this per-day in Python since range is at most 92 days
    current = from_d.date()
    end = to_d.date()
    result_items = []

    while current <= end:
        date_str = current.isoformat()
        headcount = db.employees.count_documents(
            {"department": department, "joined_on": {"$lte": date_str}}
        )
        log_row = log_rows.get(date_str, {})
        present_count = log_row.get("present_count_raw", 0)
        late_count = log_row.get("late_count", 0)
        is_wd = current.weekday() < 5

        if is_wd and headcount > 0:
            attendance_rate = half_up(present_count / headcount, 4)
        else:
            attendance_rate = None

        result_items.append(
            {
                "date": date_str,
                "is_working_day": is_wd,
                "headcount": headcount,
                "present_count": present_count,
                "late_count": late_count,
                "attendance_rate": attendance_rate,
                "moving_avg_7d": None,  # computed below
            }
        )
        current += timedelta(days=1)

    # Compute moving_avg_7d: mean of non-null attendance_rate in window [max(0, i-6), i]
    for i, row in enumerate(result_items):
        window = result_items[max(0, i - 6) : i + 1]
        non_null = [w["attendance_rate"] for w in window if w["attendance_rate"] is not None]
        if non_null:
            row["moving_avg_7d"] = half_up(sum(non_null) / len(non_null), 4)
        else:
            row["moving_avg_7d"] = None

    return {"department": department, "items": result_items}


# ─────────────────────────────────────────────────────────────────────────────
# Admin – explain
# ─────────────────────────────────────────────────────────────────────────────