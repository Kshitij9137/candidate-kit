"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB
"""
import os
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone, date as date_cls
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Optional, List, Any

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger("uvicorn.error")

load_dotenv()

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=2000,
    connectTimeoutMS=2000,
    socketTimeoutMS=2000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        # Employees
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
        db.employees.create_index([("department", ASCENDING)], name="dept_idx")
        db.employees.create_index([("joined_on", ASCENDING)], name="joined_idx")
        # Attendance logs
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)],
            unique=True, name="emp_date_unique",
        )
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp_asc",
        )
        db.attendance_logs.create_index(
            [("date", ASCENDING), ("status", ASCENDING)], name="date_status",
        )
    except Exception as e:
        logger.warning(f"Index creation skipped at startup (MongoDB unreachable?): {e!r}")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def truncate_seconds(dt: datetime) -> datetime:
    if dt is None:
        return None
    return dt.replace(microsecond=0)


def epoch_ms(dt) -> Optional[int]:
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return int(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: Optional[int]) -> Optional[datetime]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


from typing import Annotated
from pydantic import AfterValidator

def _validate_date_str(v: str) -> str:
    if not isinstance(v, str) or len(v) != 10 or v[4] != "-" or v[7] != "-":
        raise ValueError("date must be YYYY-MM-DD")
    try:
        date_cls.fromisoformat(v)
    except ValueError:
        raise ValueError("date must be a valid calendar date")
    # Reject things like 2026-13-01 that fromisoformat might accept on some versions
    y, m, d = v.split("-")
    if not (1 <= int(m) <= 12) or not (1 <= int(d) <= 31):
        raise ValueError("date must be a valid calendar date")
    return v


def _require_date(v: str, name: str) -> str:
    """Validates a date string and raises HTTP 422 (not 500) if it fails."""
    try:
        return _validate_date_str(v)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"{name}: {e}")


DateStr = Annotated[str, AfterValidator(_validate_date_str)]


# --------------------------------------------------------------------------- #
# Rounding (half-up)
# --------------------------------------------------------------------------- #
def round_half_up(value: Optional[float], places: int) -> Optional[float]:
    if value is None:
        return None
    q = Decimal(10) ** -places
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Business rules R1 - R5
# --------------------------------------------------------------------------- #
def compute_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date; overnight shifts that start 'late' belong to previous day."""
    pi = truncate_seconds(to_ist(punch_in))
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    shift_start_min = sh * 60 + sm
    shift_end_min = eh * 60 + em
    d = pi.date()
    if shift_end_min <= shift_start_min:
        punch_min = pi.hour * 60 + pi.minute
        if punch_min < shift_end_min:
            d = d - timedelta(days=1)
    return d.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
    """R2: grace 10 min; late iff strictly more than 10:00 past shift_start."""
    pi = truncate_seconds(to_ist(punch_in))
    d = date_cls.fromisoformat(attendance_date)
    sh, sm = map(int, shift_start.split(":"))
    start = datetime(d.year, d.month, d.day, sh, sm, 0, 0, tzinfo=IST)
    diff = (pi - start).total_seconds()
    if diff > 600:
        return int(diff // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (out - in) seconds / 3600, half-up to 2 decimals."""
    pi = truncate_seconds(to_ist(punch_in))
    po = truncate_seconds(to_ist(punch_out))
    hours = (po - pi).total_seconds() / 3600.0
    return round_half_up(hours, 2)


def compute_overtime_minutes(punch_out: datetime, shift_end: str, attendance_date: str, shift_start: str) -> int:
    """R3: whole minutes from shift_end to punch_out, only when >= 30."""
    d = date_cls.fromisoformat(attendance_date)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    end_date = d
    if (eh * 60 + em) <= (sh * 60 + sm):
        end_date = d + timedelta(days=1)
    end = datetime(end_date.year, end_date.month, end_date.day, eh, em, 0, 0, tzinfo=IST)
    po = truncate_seconds(to_ist(punch_out))
    diff_min = int((po - end).total_seconds() // 60)
    return diff_min if diff_min >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def serialize_history(hist: List[dict]) -> List[dict]:
    out = []
    for h in hist or []:
        changes = {}
        for k, v in (h.get("changes") or {}).items():
            if k in ("punch_in", "punch_out"):
                changes[k] = {"from": epoch_ms(v.get("from")), "to": epoch_ms(v.get("to"))}
            else:
                changes[k] = {"from": v.get("from"), "to": v.get("to")}
        out.append({
            "at": epoch_ms(h["at"]),
            "by": h["by"],
            "reason": h["reason"],
            "changes": changes,
        })
    return out


def serialize_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": epoch_ms(doc.get("punch_in")),
        "punch_out": epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": serialize_history(doc.get("history", [])),
    }


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": epoch_ms(doc["created_at"]),
    }


# --------------------------------------------------------------------------- #
# Enums & models
# --------------------------------------------------------------------------- #
class StatusEnum(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


def _validate_epoch(v):
    """Rejects strings, floats, bools, out-of-range ints. Called in mode='before'."""
    if v is None:
        return v
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("epoch milliseconds must be an integer")
    if v < EPOCH_MIN or v > EPOCH_MAX:
        raise ValueError("epoch milliseconds out of range")
    return v


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: DateStr

    @model_validator(mode="after")
    def _v_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: PresenceStatus = PresenceStatus.PRESENT

    @field_validator("punched_at", mode="before")
    @classmethod
    def _v(cls, v):
        return _validate_epoch(v)


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def _v(cls, v):
        return _validate_epoch(v)


class RegularizeIn(BaseModel):
    status: Optional[StatusEnum] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def _v(cls, v):
        return _validate_epoch(v)


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    )
    return {
        "items": [serialize_employee(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = from_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc)
    ts = truncate_seconds(ts)

    att_date = compute_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(ts, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status.value,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = from_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc)
    ts = truncate_seconds(ts)

    # Most recent record with punch_in <= ts, regardless of punch_out state.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(status_code=404, detail="No punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record already punched out")

    pi = rec["punch_in"]
    if pi.tzinfo is None:
        pi = pi.replace(tzinfo=timezone.utc)

    diff = (ts - pi).total_seconds()
    if diff <= 0 or diff > 24 * 3600:
        raise HTTPException(status_code=422, detail="Invalid punch_out (must be after punch_in and within 24h)")

    work_hours = compute_work_hours(pi, ts)
    overtime = compute_overtime_minutes(ts, emp["shift_end"], rec["date"], emp["shift_start"])
    half_day = compute_half_day(work_hours)

    # Atomic update — only succeeds if still open (race-safe).
    result = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": overtime,
            "half_day": half_day,
        }},
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=409, detail="Record already punched out")

    updated = db.attendance_logs.find_one({"_id": rec["_id"]})
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[StatusEnum] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        rng: dict = {}
        if date_from:
            rng["$gte"] = date_from
        if date_to:
            rng["$lte"] = date_to
        q["date"] = rng
    if status:
        q["status"] = status.value

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [serialize_attendance(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize(
    emp_code: str = Path(...),
    date: DateStr = Path(...),
    body: RegularizeIn = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Record not found")

    # ---- Determine effective new state -----------------------------------
    eff_status = body.status.value if body.status else rec["status"]

    if eff_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Cannot supply punch times with ABSENT/LEAVE")
        eff_pi_ms = None
        eff_po_ms = None
    else:
        # Always canonicalise to a whole second: convert -> truncate -> re-encode.
        raw_pi = body.punch_in if body.punch_in is not None else epoch_ms(rec.get("punch_in"))
        if raw_pi is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")
        pi_dt = truncate_seconds(from_epoch_ms(raw_pi))
        eff_pi_ms = epoch_ms(pi_dt)  # canonical ms, used for storage and history

        if compute_attendance_date(pi_dt, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(status_code=422, detail="punch_in must remain on the record's date")

        raw_po = body.punch_out if body.punch_out is not None else epoch_ms(rec.get("punch_out"))
        if raw_po is not None:
            po_dt = truncate_seconds(from_epoch_ms(raw_po))
            eff_po_ms = epoch_ms(po_dt)  # canonical ms, used for storage and history
            d = (po_dt - pi_dt).total_seconds()
            if d <= 0 or d > 24 * 3600:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24h")
        else:
            eff_po_ms = None

    # ---- Recompute derived fields ---------------------------------------
    if eff_status in ("ABSENT", "LEAVE"):
        new_late, new_wh, new_ot, new_hd = 0, None, 0, False
    else:
        pi_dt = truncate_seconds(from_epoch_ms(eff_pi_ms))
        new_late = compute_late_minutes(pi_dt, emp["shift_start"], date)
        if eff_po_ms is not None:
            po_dt = truncate_seconds(from_epoch_ms(eff_po_ms))
            new_wh = compute_work_hours(pi_dt, po_dt)
            new_ot = compute_overtime_minutes(po_dt, emp["shift_end"], date, emp["shift_start"])
            new_hd = compute_half_day(new_wh)
        else:
            new_wh, new_ot, new_hd = None, 0, False

    # ---- Diff against stored values -------------------------------------
    old_status = rec["status"]
    old_pi_ms = epoch_ms(rec.get("punch_in"))
    old_po_ms = epoch_ms(rec.get("punch_out"))
    old_wh = rec.get("work_hours")
    old_late = rec.get("late_minutes", 0)
    old_ot = rec.get("overtime_minutes", 0)
    old_hd = bool(rec.get("half_day", False))

    changes: dict = {}
    def _c(field, old, new):
        if old != new:
            changes[field] = {"from": old, "to": new}

    _c("status", old_status, eff_status)
    _c("punch_in", old_pi_ms, eff_pi_ms)
    _c("punch_out", old_po_ms, eff_po_ms)
    _c("work_hours", old_wh, new_wh)
    _c("late_minutes", old_late, new_late)
    _c("overtime_minutes", old_ot, new_ot)
    _c("half_day", old_hd, new_hd)

    if not changes:
        raise HTTPException(status_code=422, detail="No changes")

    # ---- Build history entry (BSON dates for punch times) ---------------
    hist_changes: dict = {}
    for k, v in changes.items():
        if k in ("punch_in", "punch_out"):
            hist_changes[k] = {
                "from": from_epoch_ms(v["from"]) if v["from"] is not None else None,
                "to": from_epoch_ms(v["to"]) if v["to"] is not None else None,
            }
        else:
            hist_changes[k] = {"from": v["from"], "to": v["to"]}

    hist_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": hist_changes,
    }

    # ---- Optimistic update ----------------------------------------------
    result = db.attendance_logs.update_one(
        {
            "_id": rec["_id"],
            "status": old_status,
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
        },
        {
            "$set": {
                "status": eff_status,
                "punch_in": from_epoch_ms(eff_pi_ms),
                "punch_out": from_epoch_ms(eff_po_ms),
                "work_hours": new_wh,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_hd,
            },
            "$push": {"history": hist_entry},
        },
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=409, detail="Concurrent modification")

    updated = db.attendance_logs.find_one({"_id": rec["_id"]})
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Analytics — helpers
# --------------------------------------------------------------------------- #
def _validate_date_range(d_from: date_cls, d_to: date_cls) -> None:
    """Raises 422 for reversed or > 92-day ranges. Shared by trend + explain."""
    if d_to < d_from:
        raise HTTPException(status_code=422, detail="to must be >= from")
    if (d_to - d_from).days > 91:
        raise HTTPException(status_code=422, detail="Range must not exceed 92 days")


def _month_bounds(month: str):
    try:
        y, m = map(int, month.split("-"))
        first = date_cls(y, m, 1)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail=f"invalid month: {month}")
    if m == 12:
        last = date_cls(y, 12, 31)
    else:
        last = date_cls(y, m + 1, 1) - timedelta(days=1)
    return first, last


def _count_working_days(start: date_cls, end: date_cls) -> int:
    n = 0
    d = start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


# --------------------------------------------------------------------------- #
# Analytics — employee monthly
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    first, last = _month_bounds(month)
    joined = date_cls.fromisoformat(emp["joined_on"])
    wd_start = max(first, joined)
    working_days = _count_working_days(wd_start, last) if wd_start <= last else 0

    date_from, date_to = first.isoformat(), last.isoformat()

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": date_from, "$lte": date_to}}},
        {"$addFields": {
            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
        }},
        {"$group": {
            "_id": None,
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$gte": ["$dow", 2]},
                        {"$lte": ["$dow", 6]},
                    ]},
                    {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                    0,
                ]
            }},
        }},
    ]
    res = list(db.attendance_logs.aggregate(pipeline))
    r = res[0] if res else {
        "leave_days": 0, "late_count": 0, "total_late_minutes": 0,
        "total_overtime_minutes": 0, "present_days": 0,
    }

    present_days = float(r["present_days"])
    attendance_pct = None if working_days == 0 else round_half_up(present_days / working_days * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(r["leave_days"]),
        "late_count": int(r["late_count"]),
        "total_late_minutes": int(r["total_late_minutes"]),
        "total_overtime_minutes": int(r["total_overtime_minutes"]),
        "attendance_pct": attendance_pct,
    }


# --------------------------------------------------------------------------- #
# Analytics — department summary
# --------------------------------------------------------------------------- #
def _dept_summary_pipeline(department: Optional[str], date_from: str, date_to: str):
    emp_match: dict = {"joined_on": {"$lte": date_to}}
    if department:
        emp_match["department"] = department

    wh_match = {
        "$and": [
            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
        ]
    }
    present_match = {
        "$and": [
            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
            {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
        ]
    }

    return [
        # Start with employees so zero-log employees are counted.
        {"$match": emp_match},
        # For each employee, compute a single stats document (never a log array).
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ec": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$eq": ["$emp_code", "$$ec"]},
                    "date": {"$gte": date_from, "$lte": date_to},
                }},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [
                        present_match,
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                        0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [
                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0,
                    ]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [
                        {"$eq": ["$status", "LEAVE"]}, 1, 0,
                    ]}},
                    "on_duty_count": {"$sum": {"$cond": [
                        {"$eq": ["$status", "ON_DUTY"]}, 1, 0,
                    ]}},
                    "wh_sum": {"$sum": {"$cond": [wh_match, "$work_hours", 0]}},
                    "wh_count": {"$sum": {"$cond": [wh_match, 1, 0]}},
                }},
            ],
            "as": "stats",
        }},
        # Flatten the 0/1-element array from the lookup.
        {"$addFields": {
            "stats": {"$ifNull": [
                {"$arrayElemAt": ["$stats", 0]},
                {
                    "present_days": 0, "late_count": 0, "total_late_minutes": 0,
                    "leave_count": 0, "on_duty_count": 0, "wh_sum": 0, "wh_count": 0,
                },
            ]},
        }},
        # Sum per-employee stats into per-department stats.
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$stats.present_days"},
            "late_count": {"$sum": "$stats.late_count"},
            "total_late_minutes": {"$sum": "$stats.total_late_minutes"},
            "leave_count": {"$sum": "$stats.leave_count"},
            "on_duty_count": {"$sum": "$stats.on_duty_count"},
            "wh_sum": {"$sum": "$stats.wh_sum"},
            "wh_count": {"$sum": "$stats.wh_count"},
        }},
        {"$addFields": {
            "avg_work_hours": {"$cond": [
                {"$gt": ["$wh_count", 0]},
                {"$divide": ["$wh_sum", "$wh_count"]},
                None,
            ]},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "avg_work_hours": 1, "late_count": 1, "total_late_minutes": 1,
            "leave_count": 1, "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    first, last = _month_bounds(month)
    date_from, date_to = first.isoformat(), last.isoformat()
    pipeline = _dept_summary_pipeline(department, date_from, date_to)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": float(r["present_days"]),
            "avg_work_hours": round_half_up(r["avg_work_hours"], 2) if r["avg_work_hours"] is not None else None,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics — leaderboard
# --------------------------------------------------------------------------- #
def _leaderboard_pipeline(month: str, limit: int, department: Optional[str]):
    first, last = _month_bounds(month)
    date_from, date_to = first.isoformat(), last.isoformat()

    pipeline = [
        {"$match": {
            "date": {"$gte": date_from, "$lte": date_to},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = _leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    for it in items:
        it["rank"] = int(it["rank"])
        it["total_late_minutes"] = int(it["total_late_minutes"])
        it["late_count"] = int(it["late_count"])
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics — department trend
# --------------------------------------------------------------------------- #
def _trend_pipeline(department: str, from_: str, to: str, emp_codes: list, joined_dates: list,
                    from_dt: datetime, to_dt: datetime):
    densify_upper = to_dt + timedelta(days=1)
    return [
        {"$match": {
            "emp_code": {"$in": emp_codes},
            "date": {"$gte": from_, "$lte": to},
        }},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0,
            ]}},
        }},
        {"$addFields": {"date_obj": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {
            "field": "date_obj",
            "range": {"step": 1, "unit": "day", "bounds": [from_dt, densify_upper]},
        }},
        {"$addFields": {
            "date": {"$dateToString": {"date": "$date_obj", "format": "%Y-%m-%d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$sort": {"date_obj": 1}},
        {"$addFields": {
            "is_working_day": {"$let": {
                "vars": {"dow": {"$dayOfWeek": "$date_obj"}},
                "in": {"$and": [{"$gte": ["$$dow", 2]}, {"$lte": ["$$dow", 6]}]},
            }},
            "headcount": {"$size": {"$filter": {
                "input": {"$literal": joined_dates},
                "as": "j",
                "cond": {"$lte": ["$$j", "$date"]},
            }}},
        }},
        {"$addFields": {
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]},
                None,
            ]},
        }},
        {"$setWindowFields": {
            "sortBy": {"date_obj": 1},
            "output": {"moving_avg_7d": {
                "$avg": "$attendance_rate",
                "window": {"documents": [-6, 0]},
            }},
        }},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1,
        }},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    from_ = _require_date(from_, "from")
    to = _require_date(to, "to")
    d_from = date_cls.fromisoformat(from_)
    d_to = date_cls.fromisoformat(to)
    _validate_date_range(d_from, d_to)

    emps = list(db.employees.find(
        {"department": department},
        {"_id": 0, "emp_code": 1, "joined_on": 1},
    ))
    if not emps:
        raise HTTPException(status_code=404, detail="Department not found")

    emp_codes = [e["emp_code"] for e in emps]
    joined_dates = [e["joined_on"] for e in emps]
    from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    to_dt = datetime(d_to.year, d_to.month, d_to.day, tzinfo=timezone.utc)

    pipeline = _trend_pipeline(department, from_, to, emp_codes, joined_dates, from_dt, to_dt)
    items = list(db.attendance_logs.aggregate(pipeline))

    for it in items:
        it["headcount"] = int(it["headcount"])
        it["present_count"] = float(it["present_count"])
        it["late_count"] = int(it["late_count"])
        if it["attendance_rate"] is not None:
            it["attendance_rate"] = round_half_up(it["attendance_rate"], 4)
        if it["moving_avg_7d"] is not None:
            it["moving_avg_7d"] = round_half_up(it["moving_avg_7d"], 4)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
def _explain(query: dict) -> dict:
    return db.command(query)


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[StatusEnum] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            rng: dict = {}
            if date_from:
                rng["$gte"] = date_from
            if date_to:
                rng["$lte"] = date_to
            q["date"] = rng
        if status:
            q["status"] = status.value
        raw = _explain({
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": json.loads(json_util.dumps(raw))}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month required")
        first, last = _month_bounds(month)
        date_from_, date_to_ = first.isoformat(), last.isoformat()
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": date_from_, "$lte": date_to_}}},
            {"$addFields": {"dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
            {"$group": {"_id": None,
                        "present_days": {"$sum": {"$cond": [
                            {"$and": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]},
                            ]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                            0,
                        ]}}}},
        ]
        raw = _explain({
            "explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": json.loads(json_util.dumps(raw))}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month required")
        first, last = _month_bounds(month)
        pipeline = _dept_summary_pipeline(department, first.isoformat(), last.isoformat())
        raw = _explain({
            "explain": {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "employees",
                "explain": json.loads(json_util.dumps(raw))}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month required")
        pipeline = _leaderboard_pipeline(month, limit, department)
        raw = _explain({
            "explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": json.loads(json_util.dumps(raw))}

    if endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(status_code=422, detail="department, from and to required")
        from_ = _require_date(from_, "from")
        to = _require_date(to, "to")
        d_from = date_cls.fromisoformat(from_)
        d_to = date_cls.fromisoformat(to)
        _validate_date_range(d_from, d_to)
        emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
        emp_codes = [e["emp_code"] for e in emps]
        joined_dates = [e["joined_on"] for e in emps]
        from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
        to_dt = datetime(d_to.year, d_to.month, d_to.day, tzinfo=timezone.utc)
        pipeline = _trend_pipeline(department, from_, to, emp_codes, joined_dates, from_dt, to_dt)
        raw = _explain({
            "explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": json.loads(json_util.dumps(raw))}

    raise HTTPException(status_code=422, detail="Unknown endpoint")