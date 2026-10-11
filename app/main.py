"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""

import os
import re
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from bson.json_util import dumps as bson_dumps
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    # Employees: unique business code and sorted department listing
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uniq_employee_code",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="department_employee_code",
    )

    # Attendance: one record per employee per date
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uniq_employee_attendance_date",
    )

    # Support attendance filters, sorting and analytics
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", DESCENDING)],
        name="attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING)],
        name="attendance_status_date",
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _as_ist_second(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST).replace(microsecond=0)


def _shift_date(
    punch_in: datetime,
    shift_start: str,
    shift_end: Optional[str] = None,
) -> date:
    local_punch_in = _as_ist_second(punch_in)
    result = local_punch_in.date()
    if (
        shift_end is not None
        and time.fromisoformat(shift_end) <= time.fromisoformat(shift_start)
        and local_punch_in.time() < time.fromisoformat(shift_end)
    ):
        result -= timedelta(days=1)
    return result


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: Optional[str] = None,
    attendance_date: Optional[str] = None,
) -> int:
    local_punch_in = _as_ist_second(punch_in)
    shift_date = (
        date.fromisoformat(attendance_date)
        if attendance_date is not None
        else _shift_date(punch_in, shift_start, shift_end)
    )
    shift_start_at = datetime.combine(
        shift_date,
        time.fromisoformat(shift_start),
        tzinfo=IST,
    )
    elapsed_seconds = (local_punch_in - shift_start_at).total_seconds()
    return int(elapsed_seconds // 60) if elapsed_seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    elapsed_seconds = (
        _as_ist_second(punch_out) - _as_ist_second(punch_in)
    ).total_seconds()
    hours = Decimal(str(elapsed_seconds)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    return work_hours is not None and Decimal(str(work_hours)) < Decimal("4.50")


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    date_str: str,
    shift_start: Optional[str] = None,
) -> int:
    shift_date = date.fromisoformat(date_str)
    shift_end_at = datetime.combine(
        shift_date,
        time.fromisoformat(shift_end),
        tzinfo=IST,
    )
    if (
        shift_start is not None
        and time.fromisoformat(shift_end) <= time.fromisoformat(shift_start)
    ):
        shift_end_at += timedelta(days=1)
    elapsed_seconds = (_as_ist_second(punch_out) - shift_end_at).total_seconds()
    return int(elapsed_seconds // 60) if elapsed_seconds >= 1800 else 0


PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}


def _parse_contract_date(value: str, field_name: str) -> date:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise HTTPException(422, f"{field_name} must be a valid YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, f"{field_name} must be a valid YYYY-MM-DD date") from exc


def _parse_month(value: str) -> tuple[str, date, date]:
    if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value) is None:
        raise HTTPException(422, "month must be a valid YYYY-MM value")
    start = date.fromisoformat(f"{value}-01")
    if start.month == 12:
        next_month = date(start.year + 1, 1, 1)
    else:
        next_month = date(start.year, start.month + 1, 1)
    return value, start, next_month


def _half_up(value: Decimal, places: str) -> float:
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _working_days(joined_on: date, start: date, end_exclusive: date) -> int:
    first = max(joined_on, start)
    if first >= end_exclusive:
        return 0
    return sum(
        1
        for offset in range((end_exclusive - first).days)
        if (first + timedelta(days=offset)).weekday() < 5
    )


def _validate_emp_code(emp_code: str) -> None:
    if re.fullmatch(r"EMP\d{4,6}", emp_code) is None:
        raise HTTPException(422, "emp_code must match EMP followed by 4-6 digits")


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date") from exc
        return value

    @model_validator(mode="after")
    def validate_shift_range(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )
    punch_out: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="MongoDB is unavailable",
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    created_at = datetime.now(timezone.utc)
    doc["created_at"] = created_at
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = int(created_at.timestamp() * 1000)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = (
        datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc)
        if body.punched_at is not None
        else datetime.now(timezone.utc)
    ).replace(microsecond=0)
    attendance_date = _shift_date(
        ts,
        emp["shift_start"],
        emp["shift_end"],
    )
    d = attendance_date.isoformat()
    late_minutes = compute_late_minutes(
        ts,
        emp["shift_start"],
        emp["shift_end"],
        d,
    )
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": int(ts.timestamp() * 1000),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": doc["late_minutes"],
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = (
        datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc)
        if body.punched_at is not None
        else datetime.now(timezone.utc)
    ).replace(microsecond=0)
    candidate_query = {
        "emp_code": body.emp_code,
        "punch_in": {"$ne": None, "$lte": ts},
        "punch_out": None,
    }
    candidate = db.attendance_logs.find_one(
        candidate_query,
        sort=[("punch_in", DESCENDING)],
    )
    if candidate is None:
        closed_candidate = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$ne": None, "$lte": ts},
                "punch_out": {"$ne": None},
            },
            sort=[("punch_in", DESCENDING)],
        )
        if closed_candidate is not None:
            raise HTTPException(409, "attendance record was already punched out")
        raise HTTPException(404, "no eligible open punch-in found")

    punch_in_at = candidate["punch_in"]
    if _as_ist_second(ts) <= _as_ist_second(punch_in_at):
        raise HTTPException(422, "punched_at must be after punch_in")
    if (_as_ist_second(ts) - _as_ist_second(punch_in_at)).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punched_at must be within 24 hours of punch_in")

    work_hours = compute_work_hours(punch_in_at, ts)
    update = {
        "$set": {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": compute_overtime(
                ts,
                emp["shift_end"],
                candidate["date"],
                emp["shift_start"],
            ),
            "half_day": compute_half_day(work_hours),
        }
    }
    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        update,
        projection={"_id": 0},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record was already punched out")

    return {
        "emp_code": updated["emp_code"],
        "date": updated["date"],
        "status": updated["status"],
        "punch_in": int(_as_ist_second(updated["punch_in"]).timestamp() * 1000),
        "punch_out": int(_as_ist_second(updated["punch_out"]).timestamp() * 1000),
        "work_hours": updated["work_hours"],
        "late_minutes": updated.get("late_minutes", 0),
        "overtime_minutes": updated.get("overtime_minutes", 0),
        "half_day": updated.get("half_day", False),
        "history": updated.get("history", []),
    }


@app.patch("/attendance/{emp_code}/{attendance_date}")
def regularize_attendance(
    emp_code: str,
    attendance_date: date,
    body: RegularizeIn,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    current = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": attendance_date.isoformat()}
    )
    if current is None:
        raise HTTPException(404, "attendance record not found")

    old_history = current.get("history", [])
    old_status = current["status"]
    old_punch_in = current.get("punch_in")
    old_punch_out = current.get("punch_out")
    final_status = body.status if body.status is not None else old_status
    final_punch_in = (
        datetime.fromtimestamp(body.punch_in / 1000, tz=timezone.utc).replace(
            microsecond=0
        )
        if body.punch_in is not None
        else old_punch_in
    )
    final_punch_out = (
        datetime.fromtimestamp(body.punch_out / 1000, tz=timezone.utc).replace(
            microsecond=0
        )
        if body.punch_out is not None
        else old_punch_out
    )
    old_punch_in_key = (
        _as_ist_second(old_punch_in) if old_punch_in is not None else None
    )
    old_punch_out_key = (
        _as_ist_second(old_punch_out) if old_punch_out is not None else None
    )
    final_punch_in_key = (
        _as_ist_second(final_punch_in) if final_punch_in is not None else None
    )
    final_punch_out_key = (
        _as_ist_second(final_punch_out) if final_punch_out is not None else None
    )

    if final_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422,
                "ABSENT and LEAVE records cannot include punch times",
            )
        final_punch_in = None
        final_punch_out = None
    elif final_punch_in is None:
        raise HTTPException(422, "presence status requires punch_in")

    if final_punch_in is not None:
        if _shift_date(
            final_punch_in,
            emp["shift_start"],
            emp["shift_end"],
        ) != attendance_date:
            raise HTTPException(422, "punch_in must belong to the attendance date")
        if final_punch_out is not None:
            if _as_ist_second(final_punch_out) <= _as_ist_second(final_punch_in):
                raise HTTPException(422, "punch_out must be after punch_in")
            if (
                _as_ist_second(final_punch_out)
                - _as_ist_second(final_punch_in)
            ).total_seconds() > 24 * 3600:
                raise HTTPException(
                    422,
                    "punch_out must be within 24 hours of punch_in",
                )
    elif final_punch_out is not None:
        raise HTTPException(422, "punch_out requires punch_in")

    if (
        final_status == old_status
        and final_punch_in_key == old_punch_in_key
        and final_punch_out_key == old_punch_out_key
    ):
        raise HTTPException(422, "request makes no changes")

    if final_punch_in is None:
        late_minutes = 0
        work_hours = None
        overtime_minutes = 0
        half_day = False
    else:
        late_minutes = compute_late_minutes(
            final_punch_in,
            emp["shift_start"],
            emp["shift_end"],
            attendance_date.isoformat(),
        )
        work_hours = (
            compute_work_hours(final_punch_in, final_punch_out)
            if final_punch_out is not None
            else None
        )
        overtime_minutes = (
            compute_overtime(
                final_punch_out,
                emp["shift_end"],
                attendance_date.isoformat(),
                emp["shift_start"],
            )
            if final_punch_out is not None
            else 0
        )
        half_day = compute_half_day(work_hours)

    def history_value(value):
        return int(_as_ist_second(value).timestamp() * 1000) if value else None

    changes = {}
    if old_status != final_status:
        changes["status"] = {"from": old_status, "to": final_status}
    if old_punch_in_key != final_punch_in_key:
        changes["punch_in"] = {
            "from": old_punch_in,
            "to": final_punch_in,
        }
    if old_punch_out_key != final_punch_out_key:
        changes["punch_out"] = {
            "from": old_punch_out,
            "to": final_punch_out,
        }
    old_work_hours = current.get("work_hours")
    old_late_minutes = current.get("late_minutes", 0)
    old_overtime_minutes = current.get("overtime_minutes", 0)
    old_half_day = current.get("half_day", False)
    for field_name, old_value, new_value in (
        ("work_hours", old_work_hours, work_hours),
        ("late_minutes", old_late_minutes, late_minutes),
        ("overtime_minutes", old_overtime_minutes, overtime_minutes),
        ("half_day", old_half_day, half_day),
    ):
        if old_value != new_value:
            changes[field_name] = {"from": old_value, "to": new_value}

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    update = {
        "$set": {
            "status": final_status,
            "punch_in": final_punch_in,
            "punch_out": final_punch_out,
            "work_hours": work_hours,
            "late_minutes": late_minutes,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
        },
        "$push": {"history": history_entry},
    }
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": current["_id"],
            "$or": [
                {"history": old_history},
                {"history": {"$exists": False}},
            ],
        },
        update,
        projection={"_id": 0},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record was changed concurrently")

    def serialize_history_entry(entry):
        serialized_changes = {}
        for field_name, change in (entry.get("changes") or {}).items():
            item = dict(change)
            if field_name in {"punch_in", "punch_out"}:
                item["from"] = history_value(item.get("from"))
                item["to"] = history_value(item.get("to"))
            serialized_changes[field_name] = item
        return {
            "at": history_value(entry.get("at")),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": serialized_changes,
        }

    return {
        "emp_code": updated["emp_code"],
        "date": updated["date"],
        "status": updated["status"],
        "punch_in": history_value(updated.get("punch_in")),
        "punch_out": history_value(updated.get("punch_out")),
        "work_hours": updated.get("work_hours"),
        "late_minutes": updated.get("late_minutes", 0),
        "overtime_minutes": updated.get("overtime_minutes", 0),
        "half_day": updated.get("half_day", False),
        "history": [
            serialize_history_entry(entry)
            for entry in updated.get("history", [])
        ],
    }


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    def parse_date(value: Optional[str], field_name: str):
        if value is None:
            return None
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
                raise ValueError
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"{field_name} must be a valid YYYY-MM-DD date",
            ) from exc

    parsed_date_from = parse_date(date_from, "date_from")
    parsed_date_to = parse_date(date_to, "date_to")
    if (
        parsed_date_from is not None
        and parsed_date_to is not None
        and parsed_date_from > parsed_date_to
    ):
        raise HTTPException(
            status_code=422,
            detail="date_from must not be later than date_to",
        )

    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status

    def epoch_millis(value):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)

    def serialize_history(history):
        result = []
        for entry in history or []:
            changes = {}
            for field_name, change in (entry.get("changes") or {}).items():
                serialized_change = dict(change)
                if field_name in {"punch_in", "punch_out"}:
                    serialized_change["from"] = epoch_millis(
                        serialized_change.get("from")
                    )
                    serialized_change["to"] = epoch_millis(
                        serialized_change.get("to")
                    )
                changes[field_name] = serialized_change
            result.append(
                {
                    "at": epoch_millis(entry.get("at")),
                    "by": entry.get("by"),
                    "reason": entry.get("reason"),
                    "changes": changes,
                }
            )
        return result

    def serialize_record(doc):
        return {
            "emp_code": doc["emp_code"],
            "date": doc["date"],
            "status": doc["status"],
            "punch_in": epoch_millis(doc.get("punch_in")),
            "punch_out": epoch_millis(doc.get("punch_out")),
            "work_hours": doc.get("work_hours"),
            "late_minutes": doc.get("late_minutes", 0),
            "overtime_minutes": doc.get("overtime_minutes", 0),
            "half_day": doc.get("half_day", False),
            "history": serialize_history(doc.get("history", [])),
        }

    total = db.attendance_logs.count_documents(q)
    items = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_record(doc) for doc in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    _validate_emp_code(emp_code)
    month, month_start, next_month = _parse_month(month)
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")

    joined_on = _parse_contract_date(employee["joined_on"], "joined_on")
    working_days = _working_days(joined_on, month_start, next_month)
    attendance_start = max(joined_on, month_start).isoformat()
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": attendance_start, "$lt": next_month.isoformat()},
            }
        },
        {
            "$set": {
                "_attendance_date": {
                    "$dateFromString": {
                        "dateString": "$date",
                        "format": "%Y-%m-%d",
                        "timezone": "Asia/Kolkata",
                    }
                }
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
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$lt": [{"$dayOfWeek": "$_attendance_date"}, 7]},
                                    {"$gt": [{"$dayOfWeek": "$_attendance_date"}, 1]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]
    aggregate = next(iter(db.attendance_logs.aggregate(pipeline)), {})
    present_days = float(aggregate.get("present_days", 0))
    attendance_pct = (
        None
        if working_days == 0
        else _half_up(
            Decimal(str(present_days)) / Decimal(working_days) * Decimal("100"),
            "0.01",
        )
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(aggregate.get("leave_days", 0)),
        "late_count": int(aggregate.get("late_count", 0)),
        "total_late_minutes": int(aggregate.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(aggregate.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    month, month_start, next_month = _parse_month(month)
    month_end = (next_month - timedelta(days=1)).isoformat()
    employee_match = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department
    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code", "joined_on": "$joined_on"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start.isoformat()]},
                                    {"$lt": ["$date", next_month.isoformat()]},
                                    {"$gte": ["$date", "$$joined_on"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "attendance",
            }
        },
        {"$unwind": {"path": "$attendance", "preserveNullAndEmptyArrays": True}},
        {
            "$set": {
                "_attendance_date": {
                    "$cond": [
                        {"$ne": [{"$ifNull": ["$attendance.date", None]}, None]},
                        {
                            "$dateFromString": {
                                "dateString": "$attendance.date",
                                "format": "%Y-%m-%d",
                                "timezone": "Asia/Kolkata",
                            }
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": {"department": "$department", "emp_code": "$emp_code"},
                "headcount": {"$first": 1},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$cond": [
                                    {"$eq": ["$_attendance_date", None]},
                                    False,
                                    {
                                        "$and": [
                                            {"$in": ["$attendance.status", list(PRESENCE_STATUSES)]},
                                            {"$lt": [{"$dayOfWeek": "$_attendance_date"}, 7]},
                                            {"$gt": [{"$dayOfWeek": "$_attendance_date"}, 1]},
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$attendance.half_day", True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$attendance.status", list(PRESENCE_STATUSES)]},
                                    {"$ne": [{"$ifNull": ["$attendance.work_hours", None]}, None]},
                                ]
                            },
                            "$attendance.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$attendance.status", list(PRESENCE_STATUSES)]},
                                    {"$ne": [{"$ifNull": ["$attendance.work_hours", None]}, None]},
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
                            {"$gt": [{"$ifNull": ["$attendance.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$attendance.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$attendance.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$attendance.status", "ON_DUTY"]}, 1, 0]
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": "$headcount"},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]
    items = []
    for row in db.employees.aggregate(pipeline):
        count = int(row.get("work_hours_count", 0))
        avg = (
            None
            if count == 0
            else _half_up(
                Decimal(str(row.get("work_hours_sum", 0))) / Decimal(count),
                "0.01",
            )
        )
        items.append(
            {
                "department": row["_id"],
                "headcount": int(row.get("headcount", 0)),
                "present_days": float(row.get("present_days", 0)),
                "avg_work_hours": avg,
                "late_count": int(row.get("late_count", 0)),
                "total_late_minutes": int(row.get("total_late_minutes", 0)),
                "leave_count": int(row.get("leave_count", 0)),
                "on_duty_count": int(row.get("on_duty_count", 0)),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    month, month_start, next_month = _parse_month(month)
    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start.isoformat(), "$lt": next_month.isoformat()},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$employee.emp_code",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    ranked = []
    previous_total = None
    current_rank = 0
    for index, row in enumerate(db.attendance_logs.aggregate(pipeline), 1):
        total = int(row["total_late_minutes"])
        if total != previous_total:
            current_rank = index
            previous_total = total
        row["rank"] = current_rank
        row["total_late_minutes"] = total
        row["late_count"] = int(row["late_count"])
        ranked.append(row)
    return {"month": month, "items": [row for row in ranked if row["rank"] <= limit]}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start = _parse_contract_date(from_date, "from")
    end = _parse_contract_date(to_date, "to")
    if end < start:
        raise HTTPException(422, "to must not be earlier than from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range must not exceed 92 calendar days")
    if db.employees.find_one({"department": department}) is None:
        raise HTTPException(404, "department not found")

    start_dt = datetime.combine(start, time.min, tzinfo=IST)
    end_dt = datetime.combine(end + timedelta(days=1), time.min, tzinfo=IST)
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        {
            "$match": {
                "employee.department": department,
                "$expr": {"$gte": ["$date", "$employee.joined_on"]},
            }
        },
        {
            "$set": {
                "_day": {
                    "$dateFromString": {
                        "dateString": "$date",
                        "format": "%Y-%m-%d",
                        "timezone": "Asia/Kolkata",
                    }
                }
            }
        },
        {
            "$group": {
                "_id": "$_day",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENCE_STATUSES)]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
            }
        },
        {"$project": {"_id": 0, "day": "$_id", "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]}}},
        {
            "$set": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$sort": {"day": ASCENDING}},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for row in rows:
        current_day = row["day"].astimezone(IST).date() if row["day"].tzinfo else row["day"].date()
        if current_day < start or current_day > end:
            continue
        headcount = db.employees.count_documents(
            {"department": department, "joined_on": {"$lte": current_day.isoformat()}}
        )
        is_working_day = current_day.weekday() < 5
        present_count = float(row.get("present_count", 0))
        rate = (
            None
            if not is_working_day or headcount == 0
            else _half_up(
                Decimal(str(present_count)) / Decimal(headcount) * Decimal("100"),
                "0.0001",
            )
        )
        items.append(
            {
                "date": current_day.isoformat(),
                "is_working_day": is_working_day,
                "headcount": headcount,
                "present_count": present_count,
                "late_count": int(row.get("late_count", 0)),
                "attendance_rate": rate,
                "_rate": rate,
            }
        )
    for index, item in enumerate(items):
        window = [row["_rate"] for row in items[max(0, index - 6): index + 1] if row["_rate"] is not None]
        item["moving_avg_7d"] = (
            None
            if not window
            else _half_up(sum((Decimal(str(value)) for value in window), Decimal("0")) / Decimal(len(window)), "0.0001")
        )
    for item in items:
        item.pop("_rate")
    return {"department": department, "items": items}


EXPLAIN_ENDPOINTS = {
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
}


def _explain_aggregate(collection, pipeline):
    return _json_safe_explain(
        db.command(
        "explain",
        {
            "aggregate": collection.name,
            "pipeline": pipeline,
            "cursor": {},
        },
        verbosity="executionStats",
        )
    )


def _json_safe_explain(explain):
    return json.loads(bson_dumps(explain))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
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
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[
        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    ] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint not in EXPLAIN_ENDPOINTS:
        raise HTTPException(422, "unsupported endpoint")

    if endpoint == "attendance_list":
        parsed_from = (
            _parse_contract_date(date_from, "date_from")
            if date_from is not None
            else None
        )
        parsed_to = (
            _parse_contract_date(date_to, "date_to")
            if date_to is not None
            else None
        )
        if parsed_from is not None and parsed_to is not None and parsed_from > parsed_to:
            raise HTTPException(422, "date_from must not be later than date_to")
        query = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if date_from is not None or date_to is not None:
            query["date"] = {}
            if date_from is not None:
                query["date"]["$gte"] = date_from
            if date_to is not None:
                query["date"]["$lte"] = date_to
        if status is not None:
            query["status"] = status
        try:
            explain = _json_safe_explain(
                db.attendance_logs.find(query)
                .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
                .skip((page - 1) * page_size)
                .limit(page_size)
                .explain()
            )
        except PyMongoError as exc:
            raise HTTPException(503, "MongoDB explain failed") from exc
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    if endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(422, "emp_code and month are required")
        _validate_emp_code(emp_code)
        month, month_start, next_month = _parse_month(month)
        employee = db.employees.find_one({"emp_code": emp_code})
        if employee is None:
            raise HTTPException(404, "employee not found")
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": max(
                            _parse_contract_date(employee["joined_on"], "joined_on"),
                            month_start,
                        ).isoformat(),
                        "$lt": next_month.isoformat(),
                    },
                }
            }
        ]
        try:
            explain = _explain_aggregate(db.attendance_logs, pipeline)
        except PyMongoError as exc:
            raise HTTPException(503, "MongoDB explain failed") from exc
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_summary":
        if month is None:
            raise HTTPException(422, "month is required")
        month, month_start, next_month = _parse_month(month)
        match = {
            "joined_on": {
                "$lte": (next_month - timedelta(days=1)).isoformat()
            }
        }
        if department is not None:
            match["department"] = department
        pipeline = [
            {"$match": match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "attendance",
                }
            },
        ]
        try:
            explain = _explain_aggregate(db.employees, pipeline)
        except PyMongoError as exc:
            raise HTTPException(503, "MongoDB explain failed") from exc
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}

    if endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(422, "month is required")
        month, month_start, next_month = _parse_month(month)
        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": month_start.isoformat(),
                        "$lt": next_month.isoformat(),
                    },
                    "late_minutes": {"$gt": 0},
                }
            },
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
        ]
        if department is not None:
            pipeline.append({"$match": {"employee.department": department}})
        try:
            explain = _explain_aggregate(db.attendance_logs, pipeline)
        except PyMongoError as exc:
            raise HTTPException(503, "MongoDB explain failed") from exc
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if from_date is None or to_date is None or department is None:
        raise HTTPException(422, "department, from, and to are required")
    start = _parse_contract_date(from_date, "from")
    end = _parse_contract_date(to_date, "to")
    if end < start:
        raise HTTPException(422, "to must not be earlier than from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range must not exceed 92 calendar days")
    if db.employees.find_one({"department": department}) is None:
        raise HTTPException(404, "department not found")
    pipeline = [
        {
            "$match": {
                "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}
            }
        },
        {
            "$set": {
                "_day": {
                    "$dateFromString": {
                        "dateString": "$date",
                        "format": "%Y-%m-%d",
                        "timezone": "Asia/Kolkata",
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$match": {"employee.department": department}},
        {
            "$densify": {
                "field": "_day",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(start, time.min, tzinfo=IST),
                        datetime.combine(end + timedelta(days=1), time.min, tzinfo=IST),
                    ],
                },
            }
        },
    ]
    try:
        explain = _explain_aggregate(db.attendance_logs, pipeline)
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB explain failed") from exc
    return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
