"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

Implements the contract in openapi.yaml over the collections described in DATA_MODEL.md.
All rules R1-R10 live in the helpers below.
"""
import calendar
import os
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# tz_aware=True: datetimes come back as aware UTC, so epoch conversion is unambiguous.
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH_MIN = 100000000000
EPOCH_MAX = 4102444800000

EpochMillis = Annotated[int, Field(strict=True, ge=EPOCH_MIN, le=EPOCH_MAX)]
StatusLiteral = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Startup: indexes (idempotent)
# --------------------------------------------------------------------------- #
def _ensure_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
    # unique (emp_code, date) is both the R6/one-record-per-day guarantee and the punch-in race guard.
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date"
    )
    # serves /attendance sort (date desc, emp_code asc) and every month-range aggregation.
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")


try:
    _ensure_indexes()
except PyMongoError:
    pass  # DB may be down at import time; /health reports it, indexes retry on next start.


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def _ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _trunc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.replace(microsecond=0)


def _from_ms(value: int) -> datetime:
    # R1: truncate instants to whole seconds at the boundary.
    return datetime.fromtimestamp(value // 1000, tz=timezone.utc).replace(microsecond=0)


def _round_half_up(value: float, places: int) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def _shift_instant(date_str: str, hhmm: str, day_offset: int = 0) -> datetime:
    h, m = map(int, hhmm.split(":"))
    d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=day_offset)
    return d.replace(hour=h, minute=m, tzinfo=IST)


def _is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def compute_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date of the punch, with the overnight exception."""
    ist = punch_in.astimezone(IST)
    if _is_overnight(shift_start, shift_end):
        eh, em = map(int, shift_end.split(":"))
        if ist.hour * 60 + ist.minute < eh * 60 + em:
            return (ist.date() - timedelta(days=1)).isoformat()
    return ist.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, att_date: str) -> int:
    """R2: grace 10 min; when late, floor minutes since shift start."""
    start = _shift_instant(att_date, shift_start)
    secs = int((punch_in - start).total_seconds())
    return secs // 60 if secs > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: exact seconds / 3600, rounded 2 dp half-up."""
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: str, att_date: str, overnight: bool) -> int:
    """R3: floor minutes past shift end, only when >= 30."""
    end = _shift_instant(att_date, shift_end, 1 if overnight else 0)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def recompute(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
              employee: dict, att_date: str) -> dict:
    """R2-R5 for the stored derived fields."""
    if status in ("ABSENT", "LEAVE"):
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, employee["shift_start"], att_date)
    if punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    work = compute_work_hours(punch_in, punch_out)
    overtime = compute_overtime(punch_out, employee["shift_end"], att_date,
                                _is_overnight(employee["shift_start"], employee["shift_end"]))
    return {"late_minutes": late, "work_hours": work, "overtime_minutes": overtime, "half_day": work < 4.50}


# --------------------------------------------------------------------------- #
# Serialization (BSON <-> API)
# --------------------------------------------------------------------------- #
def _history_out(entry: dict) -> dict:
    changes = {}
    for key, pair in (entry.get("changes") or {}).items():
        frm, to = pair.get("from"), pair.get("to")
        if key in ("punch_in", "punch_out"):
            frm, to = _ms(frm), _ms(to)
        changes[key] = {"from": frm, "to": to}
    return {"at": _ms(entry["at"]), "by": entry["by"], "reason": entry["reason"], "changes": changes}


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": _ms(doc["created_at"]),
    }


def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _ms(doc.get("punch_in")),
        "punch_out": _ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [_history_out(h) for h in (doc.get("history") or [])],
    }


def _plain(value):
    """Explain output -> plain JSON (datetimes to epoch ms, unknown types to str)."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, datetime):
        return _ms(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def _check_joined_on(cls, v: str) -> str:
        date.fromisoformat(v)
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[StatusLiteral] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Shared query builders (used by endpoints AND /admin/explain)
# --------------------------------------------------------------------------- #
def _month_bounds(month: str) -> tuple[str, str]:
    year, mon = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def _attendance_filter(emp_code, date_from, date_to, status) -> dict:
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        rng = {}
        if date_from:
            rng["$gte"] = date_from
        if date_to:
            rng["$lte"] = date_to
        q["date"] = rng
    if status:
        q["status"] = status
    return q


def _month_agg_project(with_dept: bool) -> dict:
    project = {
        "status": 1,
        "late": {"$ifNull": ["$late_minutes", 0]},
        "ot": {"$ifNull": ["$overtime_minutes", 0]},
        "half": {"$ifNull": ["$half_day", False]},
        "wh": {"$ifNull": ["$work_hours", None]},
        "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
    }
    if with_dept:
        project["dept"] = "$emp.department"
    return {"$project": project}


_PRESENT_SUM = {"$sum": {"$cond": [
    {"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$in": ["$dow", [2, 3, 4, 5, 6]]}]},
    {"$cond": [{"$eq": ["$half", True]}, 0.5, 1]},
    0,
]}}

# trend counts present on any weekday or weekend (R7 only restricts monthly present_days).
_PRESENT_ANY_SUM = {"$sum": {"$cond": [
    {"$in": ["$status", list(PRESENCE)]},
    {"$cond": [{"$eq": ["$half", True]}, 0.5, 1]},
    0,
]}}


def _monthly_pipeline(emp_code: str, ms: str, me: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": ms, "$lte": me}}},
        _month_agg_project(with_dept=False),
        {"$group": {
            "_id": None,
            "present": _PRESENT_SUM,
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late", 0]}, 1, 0]}},
            "total_late": {"$sum": "$late"},
            "total_ot": {"$sum": "$ot"},
        }},
    ]


def _summary_attendance_pipeline(ms: str, me: str, department: Optional[str]) -> list:
    work_present = {"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$ne": ["$wh", None]}]}
    pipeline = [
        {"$match": {"date": {"$gte": ms, "$lte": me}}},
        {"$lookup": {"from": "employees", "localField": "emp_code",
                     "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        _month_agg_project(with_dept=True),
        {"$group": {
            "_id": "$dept",
            "present": _PRESENT_SUM,
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late", 0]}, 1, 0]}},
            "total_late": {"$sum": "$late"},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            "wh_sum": {"$sum": {"$cond": [work_present, "$wh", 0]}},
            "wh_count": {"$sum": {"$cond": [work_present, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]
    return pipeline


def _leaderboard_pipeline(ms: str, me: str, department: Optional[str]) -> list:
    pipeline = [
        {"$match": {"date": {"$gte": ms, "$lte": me}}},
        {"$group": {
            "_id": "$emp_code",
            "total": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$match": {"total": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id",
                     "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # drops logs whose employee does not exist
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$project": {"emp_code": "$_id", "total": 1, "late_count": 1,
                      "name": "$emp.name", "department": "$emp.department"}},
        {"$sort": {"total": -1, "emp_code": 1}},
    ]
    return pipeline


def _trend_pipeline(department: str, frm: str, to: str, from_dt: datetime, to_dt: datetime) -> list:
    # $densify's upper bound is exclusive, so extend one day past `to` and drop it after filling.
    upper = to_dt + timedelta(days=1)
    return [
        {"$match": {"date": {"$gte": frm, "$lte": to}}},
        {"$lookup": {"from": "employees", "localField": "emp_code",
                     "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$project": {"date": 1, "status": 1, "late": {"$ifNull": ["$late_minutes", 0]},
                      "half": {"$ifNull": ["$half_day", False]}}},
        {"$group": {
            "_id": "$date",
            "present_count": _PRESENT_ANY_SUM,
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late", 0]}, 1, 0]}},
        }},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$sort": {"d": 1}},
        # gap-fill one document per calendar day, weekends and empty days included.
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [from_dt, upper]}}},
        {"$match": {"d": {"$lt": upper}}},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$sort": {"d": 1}},
    ]


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [employee_out(d) for d in items], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "unknown employee")
    ts = _from_ms(body.punched_at) if body.punched_at else _trunc(datetime.now(timezone.utc))
    att_date = compute_attendance_date(ts, employee["shift_start"], employee["shift_end"])
    derived = recompute(body.status, ts, None, employee, att_date)
    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": derived["work_hours"],
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": derived["overtime_minutes"],
        "half_day": derived["half_day"],
        "history": [],
    }
    # Uniqueness is enforced by the (emp_code, date) unique index: the loser throws DuplicateKeyError.
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "unknown employee")
    ts = _from_ms(body.punched_at) if body.punched_at else _trunc(datetime.now(timezone.utc))
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        latest = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
            sort=[("punch_in", DESCENDING)],
        )
        if latest is None:
            raise HTTPException(404, "no punch-in found")
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")
    delta = (ts - rec["punch_in"]).total_seconds()
    if delta <= 0 or delta > 86400:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    derived = recompute(rec["status"], rec["punch_in"], ts, employee, rec["date"])
    # filter on punch_out == null makes two simultaneous punch-outs yield exactly one 200.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record is already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusLiteral] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    for value in (date_from, date_to):
        if value:
            date.fromisoformat(value)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    q = _attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    items = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [record_out(d) for d in items], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(
    body: RegularizeRequest,
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "unknown employee")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no record for that date")

    supplied = body.model_fields_set
    new_status = body.status if "status" in supplied else rec["status"]
    old_pin, old_pout = rec.get("punch_in"), rec.get("punch_out")

    if new_status in ("ABSENT", "LEAVE"):
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(422, "cannot supply punch times with ABSENT/LEAVE")
        fin_pin = fin_pout = None
    else:
        fin_pin = _from_ms(body.punch_in) if "punch_in" in supplied else old_pin
        if fin_pin is None:
            raise HTTPException(422, "presence status requires a punch_in")
        fin_pin = _trunc(fin_pin)
        fin_pout = _from_ms(body.punch_out) if "punch_out" in supplied else old_pout
        fin_pout = _trunc(fin_pout) if fin_pout is not None else None
        if compute_attendance_date(fin_pin, employee["shift_start"], employee["shift_end"]) != date:
            raise HTTPException(422, "punch_in must fall on the record's attendance date")
        if fin_pout is not None:
            delta = (fin_pout - fin_pin).total_seconds()
            if delta <= 0 or delta > 86400:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    derived = recompute(new_status, fin_pin, fin_pout, employee, date)
    old = {
        "status": rec["status"], "punch_in": old_pin, "punch_out": old_pout,
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False)),
    }
    new = {"status": new_status, "punch_in": fin_pin, "punch_out": fin_pout, **derived}
    changes = {k: {"from": old[k], "to": new[k]} for k in old if old[k] != new[k]}
    if not changes:
        raise HTTPException(422, "the request changes nothing")

    entry = {"at": _trunc(datetime.now(timezone.utc)), "by": body.regularized_by,
             "reason": body.reason, "changes": changes}
    # $push (never $set) for history: concurrent corrections append rather than overwrite each other.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"]},
        {"$set": {"status": new_status, "punch_in": fin_pin, "punch_out": fin_pout, **derived},
         "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record disappeared")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
def _working_days_between(start: date, end: date) -> int:
    if start > end:
        return 0
    days, cur = 0, start
    while cur <= end:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "unknown employee")
    ms, me = _month_bounds(month)
    rows = list(db.attendance_logs.aggregate(_monthly_pipeline(emp_code, ms, me)))
    agg = rows[0] if rows else {}
    present = agg.get("present", 0)
    month_start = date.fromisoformat(ms)
    month_end = date.fromisoformat(me)
    joined = date.fromisoformat(employee["joined_on"])
    working_days = _working_days_between(max(joined, month_start), month_end)
    pct = _round_half_up(present / working_days * 100, 2) if working_days > 0 else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present,
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late", 0),
        "total_overtime_minutes": agg.get("total_ot", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    ms, me = _month_bounds(month)
    head_q = {"joined_on": {"$lte": me}}  # R9: joined on or before end of period
    if department:
        head_q["department"] = department
    headcount = {row["_id"]: row["headcount"]
                 for row in db.employees.aggregate([{"$match": head_q},
                                                    {"$group": {"_id": "$department", "headcount": {"$sum": 1}}}])}
    rows = list(db.attendance_logs.aggregate(_summary_attendance_pipeline(ms, me, department)))
    by_dept = {row["_id"]: row for row in rows}
    items = []
    for dept in sorted(headcount):
        if headcount[dept] <= 0:
            continue
        row = by_dept.get(dept, {})
        wh_count = row.get("wh_count", 0)
        items.append({
            "department": dept,
            "headcount": headcount[dept],
            "present_days": row.get("present", 0),
            "avg_work_hours": _round_half_up(row["wh_sum"] / wh_count, 2) if wh_count else None,
            "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("total_late", 0),
            "leave_count": row.get("leave_count", 0),
            "on_duty_count": row.get("on_duty_count", 0),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    ms, me = _month_bounds(month)
    rows = list(db.attendance_logs.aggregate(_leaderboard_pipeline(ms, me, department)))
    items, prev_total, rank = [], None, 0
    for idx, row in enumerate(rows):
        if row["total"] != prev_total:
            rank, prev_total = idx + 1, row["total"]  # standard competition ranking
        if rank > limit:
            break
        items.append({
            "rank": rank,
            "emp_code": row["emp_code"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": int(row["total"]),
            "late_count": int(row["late_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    employees = list(db.employees.find({"department": department}, {"joined_on": 1, "_id": 0}))
    if not employees:
        raise HTTPException(404, "unknown department")
    start, end = date.fromisoformat(from_), date.fromisoformat(to)
    if to < from_:
        raise HTTPException(422, "to must be >= from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    from_dt = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    to_dt = datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc)
    rows = list(db.attendance_logs.aggregate(_trend_pipeline(department, from_, to, from_dt, to_dt)))

    joined = sorted(e["joined_on"] for e in employees)
    items = []
    for row in rows:
        day = row["date"]
        headcount = bisect_right(joined, day)  # R9: joined_on <= that day
        is_working = date.fromisoformat(day).weekday() < 5
        present = row.get("present_count", 0)
        rate = _round_half_up(present / headcount, 4) if (is_working and headcount > 0) else None
        items.append({
            "date": day,
            "is_working_day": is_working,
            "headcount": headcount,
            "present_count": present,
            "late_count": row.get("late_count", 0),
            "attendance_rate": rate,
            "moving_avg_7d": None,
        })
    rates = [it["attendance_rate"] for it in items]
    for i, it in enumerate(items):
        window = [r for r in rates[max(0, i - 6): i + 1] if r is not None]
        it["moving_avg_7d"] = _round_half_up(sum(window) / len(window), 4) if window else None
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def _explain_find(collection: str, filt: dict, sort, skip: int, limit: int) -> dict:
    command = {"find": collection, "filter": filt, "skip": skip, "limit": limit}
    if sort:
        command["sort"] = dict(sort)  # explain wants an object, not a list of pairs
    return db.command("explain", command, verbosity="executionStats")


def _explain_agg(collection: str, pipeline: list) -> dict:
    return db.command("explain", {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
                      verbosity="executionStats")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"] = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusLiteral] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        filt = _attendance_filter(emp_code, date_from, date_to, status)
        explain = _explain_find("attendance_logs", filt,
                                [("date", DESCENDING), ("emp_code", ASCENDING)],
                                (page - 1) * page_size, page_size)
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        ms, me = _month_bounds(month)
        explain = _explain_agg("attendance_logs", _monthly_pipeline(emp_code, ms, me))
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        ms, me = _month_bounds(month)
        explain = _explain_agg("attendance_logs", _summary_attendance_pipeline(ms, me, department))
        collection = "attendance_logs"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        ms, me = _month_bounds(month)
        explain = _explain_agg("attendance_logs", _leaderboard_pipeline(ms, me, department))
        collection = "attendance_logs"
    else:  # department_trend
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from and to are required")
        from_dt = datetime.combine(date.fromisoformat(from_), datetime.min.time(), tzinfo=timezone.utc)
        to_dt = datetime.combine(date.fromisoformat(to), datetime.min.time(), tzinfo=timezone.utc)
        explain = _explain_agg("attendance_logs",
                               _trend_pipeline(department, from_, to, from_dt, to_dt))
        collection = "attendance_logs"
    return {"endpoint": endpoint, "collection": collection, "explain": _plain(explain)}