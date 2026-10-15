"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
from typing import Any, Dict, List, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
import pymongo.errors

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Lifespan: Idempotent startup index creation
# --------------------------------------------------------------------------- #
def ensure_indexes(database):
    # employees collection
    database.employees.create_index([("emp_code", 1)], unique=True)
    database.employees.create_index([("department", 1), ("joined_on", 1)])
    database.employees.create_index([("joined_on", 1)])

    # attendance_logs collection
    database.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    database.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    database.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    database.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes(db)
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float | Decimal], decimals: int) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    pattern = Decimal("10") ** -decimals
    return float(d.quantize(pattern, rounding=ROUND_HALF_UP))


def to_epoch_ms(val: Any) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=UTC)
        return int(val.timestamp() * 1000)
    if isinstance(val, (int, float)):
        return int(val)
    return None


def truncate_to_second(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def ms_to_utc_dt(ms: int) -> datetime:
    # Truncate instant to whole seconds before calculation / storage (R1)
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=UTC)


def now_utc_dt() -> datetime:
    return truncate_to_second(datetime.now(tz=UTC))


def get_shift_datetimes_and_date(
    punch_in_ist: datetime, shift_start: str, shift_end: str
) -> tuple[str, datetime, datetime]:
    """
    R1: Attendance date is IST calendar date of punch_in.
    Overnight shifts (shift_end <= shift_start): punch-in earlier than shift_end
    belongs to the shift that started the previous day, and shift ends on next calendar day.
    """
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh, em) <= (sh, sm)
    punch_time = (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second)

    if is_overnight and punch_time < (eh, em, 0):
        att_date = (punch_in_ist.date() - timedelta(days=1)).isoformat()
        start_dt = datetime.combine(
            punch_in_ist.date() - timedelta(days=1), datetime.min.time(), tzinfo=IST
        ).replace(hour=sh, minute=sm)
        end_dt = datetime.combine(
            punch_in_ist.date(), datetime.min.time(), tzinfo=IST
        ).replace(hour=eh, minute=em)
    elif is_overnight:
        att_date = punch_in_ist.date().isoformat()
        start_dt = datetime.combine(
            punch_in_ist.date(), datetime.min.time(), tzinfo=IST
        ).replace(hour=sh, minute=sm)
        end_dt = datetime.combine(
            punch_in_ist.date() + timedelta(days=1), datetime.min.time(), tzinfo=IST
        ).replace(hour=eh, minute=em)
    else:
        att_date = punch_in_ist.date().isoformat()
        start_dt = datetime.combine(
            punch_in_ist.date(), datetime.min.time(), tzinfo=IST
        ).replace(hour=sh, minute=sm)
        end_dt = datetime.combine(
            punch_in_ist.date(), datetime.min.time(), tzinfo=IST
        ).replace(hour=eh, minute=em)

    return att_date, start_dt, end_dt


def get_shift_datetimes_for_date(
    att_date_str: str, shift_start: str, shift_end: str
) -> tuple[datetime, datetime]:
    d = date.fromisoformat(att_date_str)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh, em) <= (sh, sm)

    start_dt = datetime.combine(d, datetime.min.time(), tzinfo=IST).replace(
        hour=sh, minute=sm
    )
    if is_overnight:
        end_dt = datetime.combine(
            d + timedelta(days=1), datetime.min.time(), tzinfo=IST
        ).replace(hour=eh, minute=em)
    else:
        end_dt = datetime.combine(d, datetime.min.time(), tzinfo=IST).replace(
            hour=eh, minute=em
        )

    return start_dt, end_dt


def compute_late_minutes(punch_in_dt: datetime, shift_start_dt: datetime) -> int:
    """
    R2: Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 (> 600s) after shift_start;
    then late_minutes = whole minutes (floor) since shift_start.
    """
    diff_sec = (punch_in_dt - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    sec = (punch_out_dt - punch_in_dt).total_seconds()
    val = Decimal(str(sec)) / Decimal("3600")
    return float(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out_dt: datetime, shift_end_dt: datetime) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0.
    """
    diff_sec = (punch_out_dt - shift_end_dt).total_seconds()
    if diff_sec >= 1800:
        return int(diff_sec // 60)
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: Half day is true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def compute_working_days(month_str: str, joined_on_str: str) -> int:
    """
    R7: Monday - Friday, no holiday calendar; counted from joined_on for mid-month joiners.
    """
    year, m = map(int, month_str.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    m_start = date(year, m, 1)
    m_end = date(year, m, last_day)

    joined = date.fromisoformat(joined_on_str)
    if joined > m_end:
        return 0

    start_d = max(m_start, joined)
    count = 0
    curr = start_d
    while curr <= m_end:
        if curr.weekday() < 5:  # Mon-Fri
            count += 1
        curr += timedelta(days=1)
    return count


def format_history_entry(entry: dict) -> dict:
    changes = {}
    for k, v in entry.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if k in ("punch_in", "punch_out"):
            changes[k] = {
                "from": to_epoch_ms(from_val),
                "to": to_epoch_ms(to_val),
            }
        else:
            changes[k] = {
                "from": from_val,
                "to": to_val,
            }
    return {
        "at": to_epoch_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def format_attendance_doc(doc: dict) -> dict:
    history = [format_history_entry(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


def format_employee_doc(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Request & Response Models
# --------------------------------------------------------------------------- #
StatusType = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusType = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_date(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("invalid date format for joined_on")
        return v

    @model_validator(mode="after")
    def validate_shift_times(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        None, ge=100000000000, le=4102444800000
    )
    status: PresenceStatusType = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        None, ge=100000000000, le=4102444800000
    )


class RegularizeIn(BaseModel):
    status: Optional[StatusType] = None
    punch_in: Optional[StrictInt] = Field(
        None, ge=100000000000, le=4102444800000
    )
    punch_out: Optional[StrictInt] = Field(
        None, ge=100000000000, le=4102444800000
    )
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="MongoDB unavailable",
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    created_dt = now_utc_dt()
    doc["created_at"] = created_dt

    try:
        db.employees.insert_one(doc)
    except pymongo.errors.DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    punch_utc = (
        ms_to_utc_dt(body.punched_at) if body.punched_at is not None else now_utc_dt()
    )
    punch_ist = punch_utc.astimezone(IST)

    att_date, shift_start_dt, _ = get_shift_datetimes_and_date(
        punch_ist, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    )

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_ist, shift_start_dt),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except pymongo.errors.DuplicateKeyError:
        raise HTTPException(
            409, "already punched in for this date"
        )

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    punch_out_utc = (
        ms_to_utc_dt(body.punched_at) if body.punched_at is not None else now_utc_dt()
    )
    punch_out_ist = punch_out_utc.astimezone(IST)

    # Find the most recent record whose punch_in is at or before punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", -1)],
    )

    if not record:
        raise HTTPException(
            404, "no punch-in found for employee"
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            409, "that record is already punched out"
        )

    record_punch_in = record["punch_in"]
    if record_punch_in.tzinfo is None:
        record_punch_in = record_punch_in.replace(tzinfo=UTC)

    if punch_out_utc <= record_punch_in:
        raise HTTPException(
            422,
            "punched_at must be strictly after punch_in",
        )

    diff_sec = (punch_out_utc - record_punch_in).total_seconds()
    if diff_sec > 24 * 3600:
        raise HTTPException(
            422,
            "punch_out cannot be more than 24 hours after punch_in",
        )

    _, shift_end_dt = get_shift_datetimes_for_date(
        record["date"],
        emp.get("shift_start", "09:30"),
        emp.get("shift_end", "18:30"),
    )

    work_hours = compute_work_hours(record_punch_in, punch_out_utc)
    overtime_min = compute_overtime(punch_out_ist, shift_end_dt)
    half_day = compute_half_day(work_hours)

    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(
            409, "that record is already punched out"
        )

    return format_attendance_doc(updated_doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusType] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            422, "date_from cannot be greater than date_to"
        )

    q = {}
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

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise HTTPException(
            422, "Invalid attendance date format"
        )

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(
            404, "No attendance record found for date"
        )

    # Rule: setting status ABSENT or LEAVE clears both punch times,
    # so supplying punch_in or punch_out with it is 422
    if body.status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422,
                "punch_in or punch_out cannot be supplied when setting ABSENT or LEAVE",
            )

    curr_status = record["status"]
    effective_status = body.status if body.status is not None else curr_status

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")
    shift_start_dt, shift_end_dt = get_shift_datetimes_for_date(
        date, shift_start, shift_end
    )

    if effective_status in ("ABSENT", "LEAVE"):
        new_status = effective_status
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_status = effective_status

        # Effective punch_in
        if body.punch_in is not None:
            new_punch_in = ms_to_utc_dt(body.punch_in)
        else:
            new_punch_in = record.get("punch_in")

        if new_punch_in is None:
            raise HTTPException(
                422,
                "presence status requires a punch_in",
            )

        if new_punch_in.tzinfo is None:
            new_punch_in = new_punch_in.replace(tzinfo=UTC)

        # Check punch_in must stay on record's attendance date (R1)
        calc_date, _, _ = get_shift_datetimes_and_date(
            new_punch_in.astimezone(IST), shift_start, shift_end
        )
        if calc_date != date:
            raise HTTPException(
                422,
                "punch_in does not belong to the record's attendance date",
            )

        # Effective punch_out
        if body.punch_out is not None:
            new_punch_out = ms_to_utc_dt(body.punch_out)
        else:
            new_punch_out = record.get("punch_out")

        if new_punch_out is not None:
            if new_punch_out.tzinfo is None:
                new_punch_out = new_punch_out.replace(tzinfo=UTC)

            if new_punch_out <= new_punch_in:
                raise HTTPException(
                    422,
                    "punch_out must be strictly after punch_in",
                )
            if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(
                    422,
                    "punch_out must be within 24 hours of punch_in",
                )

            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(
                new_punch_out.astimezone(IST), shift_end_dt
            )
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

        new_late_minutes = compute_late_minutes(
            new_punch_in.astimezone(IST), shift_start_dt
        )

    # Compute changes
    changes = {}
    if curr_status != new_status:
        changes["status"] = {"from": curr_status, "to": new_status}

    def dt_eq(d1, d2):
        if d1 is None and d2 is None:
            return True
        if d1 is None or d2 is None:
            return False
        return to_epoch_ms(d1) == to_epoch_ms(d2)

    old_pin = record.get("punch_in")
    if not dt_eq(old_pin, new_punch_in):
        changes["punch_in"] = {"from": old_pin, "to": new_punch_in}

    old_pout = record.get("punch_out")
    if not dt_eq(old_pout, new_punch_out):
        changes["punch_out"] = {"from": old_pout, "to": new_punch_out}

    old_wh = record.get("work_hours")
    if old_wh != new_work_hours:
        changes["work_hours"] = {"from": old_wh, "to": new_work_hours}

    old_late = record.get("late_minutes", 0)
    if old_late != new_late_minutes:
        changes["late_minutes"] = {"from": old_late, "to": new_late_minutes}

    old_ot = record.get("overtime_minutes", 0)
    if old_ot != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_overtime_minutes}

    old_hd = bool(record.get("half_day", False))
    if old_hd != new_half_day:
        changes["half_day"] = {"from": old_hd, "to": new_half_day}

    if not changes:
        raise HTTPException(
            422, "a request that changes nothing is 422"
        )

    history_entry = {
        "at": now_utc_dt(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency check on history length
    existing_history = record.get("history", [])
    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {
                "$eq": [
                    {"$size": {"$ifNull": ["$history", []]}},
                    len(existing_history),
                ]
            },
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(
            409,
            "Record was modified concurrently; please retry",
        )

    return format_attendance_doc(updated_doc)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    working_days = compute_working_days(month, emp["joined_on"])

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            {
                                                "$isoDayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            5,
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        stats = res[0]
        present_days = float(stats["present_days"])
        leave_days = int(stats["leave_days"])
        late_count = int(stats["late_count"])
        total_late = int(stats["total_late_minutes"])
        total_ot = int(stats["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    match_filter = {"joined_on": {"$lte": month_end}}
    if department:
        match_filter["department"] = department

    pipeline = [
        {"$match": match_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ifNull": ["$logs", False]},
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            {
                                                "$isoDayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$logs.date"
                                                    }
                                                }
                                            },
                                            5,
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ifNull": ["$logs", False]},
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ifNull": ["$logs", False]},
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ifNull": ["$logs", False]},
                                    {"$gt": ["$logs.late_minutes", 0]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ifNull": ["$logs", False]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ifNull": ["$logs", False]},
                                    {"$eq": ["$logs.status", "LEAVE"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ifNull": ["$logs", False]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emp_codes"},
                "present_days": "$present_days",
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {
                            "$round": [
                                {
                                    "$divide": [
                                        "$work_hours_sum",
                                        "$work_hours_count",
                                    ]
                                },
                                2,
                            ]
                        },
                        None,
                    ]
                },
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count",
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    # Check department exists
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(
            404, f"Department '{department}' not found"
        )

    try:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(
            422, "Invalid date format"
        )

    if d_to < d_from:
        raise HTTPException(
            422,
            "'to' date cannot be earlier than 'from' date",
        )

    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(
            422,
            "Date range cannot exceed 92 days",
        )

    from_dt = datetime.combine(d_from, datetime.min.time(), tzinfo=timezone.utc)
    to_dt_plus1 = datetime.combine(
        d_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
    )

    dept_emps = list(
        db.employees.find({"department": department}, {"emp_code": 1})
    )
    emp_codes = [e["emp_code"] for e in dept_emps]

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$in": [
                                    "$status",
                                    ["PRESENT", "WFH", "ON_DUTY"],
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "d": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "d",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus1],
                },
            }
        },
        {
            "$project": {
                "date": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$d"}
                },
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {
                    "$lte": [{"$isoDayOfWeek": "$d"}, 5]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "headcount"},
                ],
                "as": "hc",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$first": "$hc.headcount"}, 0]}
            }
        },
        {"$project": {"hc": 0}},
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {
                                    "$divide": [
                                        "$present_count",
                                        "$headcount",
                                    ]
                                },
                                4,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {"$sort": {"date": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$round": ["$moving_avg_7d", 4]},
                        None,
                    ]
                },
            }
        },
    ]

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusType] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                422,
                "date_from cannot be greater than date_to",
            )
        q = {}
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
        skip = (page - 1) * page_size
        cmd = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        res = db.command(cmd)
        clean_explain = json_util.loads(json_util.dumps(res))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_explain,
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                422,
                "emp_code and month are required",
            )
        year, m = map(int, month.split("-"))
        last_day = calendar.monthrange(year, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": month_start, "$lte": month_end},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$lte": [
                                                {
                                                    "$isoDayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$date"
                                                        }
                                                    }
                                                },
                                                5,
                                            ]
                                        },
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$half_day", True]},
                                        0.5,
                                        1.0,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                }
            },
        ]
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(cmd)
        clean_explain = json_util.loads(json_util.dumps(res))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_explain,
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                422, "month is required"
            )
        year, m = map(int, month.split("-"))
        last_day = calendar.monthrange(year, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"

        match_filter = {"joined_on": {"$lte": month_end}}
        if department:
            match_filter["department"] = department

        pipeline = [
            {"$match": match_filter},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", month_start]},
                                        {"$lte": ["$date", month_end]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {
                "$group": {
                    "_id": "$department",
                    "emp_codes": {"$addToSet": "$emp_code"},
                }
            },
        ]
        cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(cmd)
        clean_explain = json_util.loads(json_util.dumps(res))
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": clean_explain,
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                422, "month is required"
            )
        year, m = map(int, month.split("-"))
        last_day = calendar.monthrange(year, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": month_start, "$lte": month_end},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})
        pipeline.extend(
            [
                {
                    "$setWindowFields": {
                        "sortBy": {"total_late_minutes": -1},
                        "output": {"rank": {"$rank": {}}},
                    }
                },
                {"$match": {"rank": {"$lte": limit}}},
            ]
        )
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(cmd)
        clean_explain = json_util.loads(json_util.dumps(res))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_explain,
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                422,
                "department, from and to are required",
            )
        try:
            d_from = date.fromisoformat(from_date)
            d_to = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(
                422, "Invalid date format"
            )
        if d_to < d_from or (d_to - d_from).days + 1 > 92:
            raise HTTPException(
                422, "Invalid date range"
            )

        from_dt = datetime.combine(
            d_from, datetime.min.time(), tzinfo=timezone.utc
        )
        to_dt_plus1 = datetime.combine(
            d_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
        )
        dept_emps = list(
            db.employees.find({"department": department}, {"emp_code": 1})
        )
        emp_codes = [e["emp_code"] for e in dept_emps]

        pipeline = [
            {
                "$match": {
                    "emp_code": {"$in": emp_codes},
                    "date": {"$gte": from_date, "$lte": to_date},
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$in": [
                                        "$status",
                                        ["PRESENT", "WFH", "ON_DUTY"],
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$half_day", True]},
                                        0.5,
                                        1.0,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "d": {"$dateFromString": {"dateString": "$_id"}},
                    "present_count": 1,
                }
            },
            {
                "$densify": {
                    "field": "d",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [from_dt, to_dt_plus1],
                    },
                }
            },
        ]
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(cmd)
        clean_explain = json_util.loads(json_util.dumps(res))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_explain,
        }
