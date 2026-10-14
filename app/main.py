"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real env vars win over .env)
"""
from __future__ import annotations

import calendar
import math
import os
import re
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Optional

from bson import ObjectId, Timestamp, Decimal128, Binary, Regex
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Real environment variables already set in the process must win.
load_dotenv(override=False)

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "attendance_db")

client = MongoClient(
    MONGO_URI,
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

EMP_CODE_RE = re.compile(r"^EMP[0-9]{4,6}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SHIFT_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")

EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000

PRESENCE_STATUSES = frozenset({"PRESENT", "WFH", "ON_DUTY"})
ABSENCE_STATUSES = frozenset({"ABSENT", "LEAVE"})
ALL_STATUSES = PRESENCE_STATUSES | ABSENCE_STATUSES
PRESENCE_LIST = ["PRESENT", "WFH", "ON_DUTY"]
TRACKED_FIELDS = (
    "status",
    "punch_in",
    "punch_out",
    "work_hours",
    "late_minutes",
    "overtime_minutes",
    "half_day",
)

# MongoDB $dayOfWeek: 1=Sun .. 7=Sat. Mon-Fri = 2..6.
WEEKDAY_DOW = [2, 3, 4, 5, 6]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

_indexes_ready = False


# --------------------------------------------------------------------------- #
# Indexes
# --------------------------------------------------------------------------- #
def ensure_indexes() -> bool:
    global _indexes_ready
    if _indexes_ready:
        return True
    try:
        db.employees.create_index(
            [("emp_code", ASCENDING)], unique=True, name="uniq_emp_code"
        )
        db.employees.create_index([("department", ASCENDING)], name="idx_department")
        db.employees.create_index(
            [("department", ASCENDING), ("joined_on", ASCENDING)],
            name="idx_dept_joined",
        )
        db.employees.create_index([("joined_on", ASCENDING)], name="idx_joined_on")

        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)],
            unique=True,
            name="uniq_emp_date",
        )
        try:
            db.attendance_logs.drop_index("idx_date_emp")
        except Exception:
            pass
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)],
            name="idx_date_desc_emp",
        )
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", DESCENDING)],
            name="idx_emp_date_desc",
        )
        db.attendance_logs.create_index(
            [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
            name="idx_status_date_emp",
        )
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
            name="idx_emp_punch_in",
        )
        _indexes_ready = True
        return True
    except Exception:
        return False


@app.on_event("startup")
def startup() -> None:
    ensure_indexes()


def require_db() -> None:
    if not ensure_indexes():
        raise HTTPException(status_code=503, detail="database unavailable")


# --------------------------------------------------------------------------- #
# Time / rounding helpers
# --------------------------------------------------------------------------- #
def ms_to_dt_truncated(ms: int) -> datetime:
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def now_ms_truncated() -> int:
    return int(datetime.now(timezone.utc).timestamp()) * 1000


def dt_to_ms(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def half_up(value: Any, places: int) -> float | None:
    """Half-up rounding via Decimal (never banker's / float round / $round)."""
    if value is None:
        return None
    quant = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def parse_path_date(value: str) -> str:
    if not DATE_RE.match(value):
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="date is not a real calendar day")
    return value


def parse_month(month: str) -> tuple[str, str, str]:
    """Return (month, month_start, month_end) as YYYY-MM / YYYY-MM-DD strings."""
    if not MONTH_RE.match(month):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return month, f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"


def count_working_days(start: Date, end: Date) -> int:
    if start > end:
        return 0
    n = 0
    cur = start
    one = timedelta(days=1)
    while cur <= end:
        if cur.weekday() < 5:
            n += 1
        cur += one
    return n


def shift_bounds(
    attendance_date: Date, shift_start: str, shift_end: str
) -> tuple[datetime, datetime]:
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    start_dt = datetime(
        attendance_date.year,
        attendance_date.month,
        attendance_date.day,
        sh,
        sm,
        0,
        tzinfo=IST,
    )
    end_dt = datetime(
        attendance_date.year,
        attendance_date.month,
        attendance_date.day,
        eh,
        em,
        0,
        tzinfo=IST,
    )
    if shift_end <= shift_start:
        end_dt += timedelta(days=1)
    return start_dt, end_dt


def attendance_date_for_punch(
    punch_in: datetime, shift_start: str, shift_end: str
) -> str:
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if shift_end <= shift_start:
        eh, em = map(int, shift_end.split(":"))
        if (ist.hour, ist.minute, ist.second) < (eh, em, 0):
            day = day - timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start_dt: datetime) -> int:
    late_seconds = (punch_in - shift_start_dt).total_seconds()
    if late_seconds > 600:
        return int(late_seconds // 60)
    return 0


def compute_overtime_minutes(punch_out: datetime, shift_end_dt: datetime) -> int:
    minutes = int((punch_out - shift_end_dt).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(hours)


def normalize_stored_dt(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return as_utc(dt).replace(microsecond=0)


def recompute_derived(
    status: str,
    punch_in: datetime | None,
    punch_out: datetime | None,
    path_date: str,
    shift_start: str,
    shift_end: str,
) -> tuple[int, float | None, int, bool]:
    if status in ABSENCE_STATUSES:
        return 0, None, 0, False
    assert punch_in is not None
    att_date = Date.fromisoformat(path_date)
    shift_start_dt, shift_end_dt = shift_bounds(att_date, shift_start, shift_end)
    late_minutes = compute_late_minutes(punch_in, shift_start_dt)
    if punch_out is None:
        return late_minutes, None, 0, False
    work_hours = compute_work_hours(punch_in, punch_out)
    overtime_minutes = compute_overtime_minutes(punch_out, shift_end_dt)
    half_day = work_hours < 4.50
    return late_minutes, work_hours, overtime_minutes, half_day


def field_values_equal(key: str, a: Any, b: Any) -> bool:
    if key in ("punch_in", "punch_out"):
        return dt_to_ms(a) == dt_to_ms(b)
    return a == b


def to_jsonable(obj: Any) -> Any:
    """Make Mongo explain / BSON values JSON-serializable."""
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Timestamp):
        return {"time": obj.time, "inc": obj.inc}
    if isinstance(obj, Decimal128):
        return str(obj)
    if isinstance(obj, Regex):
        return str(obj.pattern)
    if isinstance(obj, (bytes, Binary)):
        return bytes(obj).hex()
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return str(obj)
    return obj


def _dow_expr(date_string_expr: Any) -> dict:
    return {
        "$dayOfWeek": {
            "$dateFromString": {"dateString": date_string_expr, "timezone": "UTC"}
        }
    }


def _present_credit_expr(status_ref: str, half_ref: str, date_ref: str) -> dict:
    """1 or 0.5 for presence on Mon-Fri; else 0. Uses stored half_day."""
    return {
        "$cond": [
            {
                "$and": [
                    {"$in": [status_ref, PRESENCE_LIST]},
                    {"$in": [_dow_expr(date_ref), WEEKDAY_DOW]},
                ]
            },
            {
                "$cond": [
                    {"$eq": [{"$ifNull": [half_ref, False]}, True]},
                    0.5,
                    1,
                ]
            },
            0,
        ]
    }


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #
def history_to_api(history: list | None) -> list:
    out = []
    for entry in history or []:
        changes = {}
        for key, ch in (entry.get("changes") or {}).items():
            fr, to = ch.get("from"), ch.get("to")
            if key in ("punch_in", "punch_out"):
                if isinstance(fr, datetime):
                    fr = dt_to_ms(fr)
                if isinstance(to, datetime):
                    to = dt_to_ms(to)
            changes[key] = {"from": fr, "to": to}
        out.append(
            {
                "at": dt_to_ms(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": changes,
            }
        )
    return out


def employee_to_api(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def attendance_to_api(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history_to_api(doc.get("history")),
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        if not EMP_CODE_RE.match(v):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if not EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift(cls, v: str) -> str:
        if not SHIFT_RE.match(v):
            raise ValueError("shift must be HH:MM (24-hour)")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        datetime.strptime(v, "%Y-%m-%d")
        return v

    @model_validator(mode="after")
    def shift_must_differ(self) -> EmployeeIn:
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


def _validate_epoch_ms(v: int | None) -> int | None:
    if v is None:
        return v
    if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
        raise ValueError("timestamp out of range or looks like seconds")
    return v


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v: int | None) -> int | None:
        return _validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in PRESENCE_STATUSES:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v: int | None) -> int | None:
        return _validate_epoch_ms(v)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def punches_cannot_be_null(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in ("punch_in", "punch_out"):
                if key in data and data[key] is None:
                    raise ValueError(f"{key} cannot be null")
        return data

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        if v is not None and v not in ALL_STATUSES:
            raise ValueError("invalid status")
        return v

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_punch(cls, v: int | None) -> int | None:
        return _validate_epoch_ms(v)


# --------------------------------------------------------------------------- #
# Query / pipeline builders (shared with /admin/explain)
# --------------------------------------------------------------------------- #
def build_attendance_query(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[dict[str, Any], list[tuple[str, int]], int, int]:
    if date_from is not None:
        parse_path_date(date_from)
    if date_to is not None:
        parse_path_date(date_to)
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(status_code=422, detail="invalid status")

    filt: dict[str, Any] = {}
    if emp_code is not None:
        filt["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        date_clause: dict[str, str] = {}
        if date_from is not None:
            date_clause["$gte"] = date_from
        if date_to is not None:
            date_clause["$lte"] = date_to
        filt["date"] = date_clause
    if status is not None:
        filt["status"] = status

    sort: list[tuple[str, int]] = [("date", DESCENDING), ("emp_code", ASCENDING)]
    skip = (page - 1) * page_size
    return filt, sort, skip, page_size


def build_employee_monthly_pipeline(
    emp_code: str, month_start: str, month_end: str
) -> list[dict[str, Any]]:
    return [
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
                    "$sum": _present_credit_expr(
                        "$status", "$half_day", "$date"
                    )
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
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


def build_department_summary_pipeline(
    month_start: str, month_end: str, department: str | None = None
) -> list[dict[str, Any]]:
    match_emp: dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
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
                "emps": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": [{"$type": "$logs"}, "missing"]},
                            0,
                            _present_credit_expr(
                                "$logs.status", "$logs.half_day", "$logs.date"
                            ),
                        ]
                    }
                },
                "work_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", PRESENCE_LIST]},
                                    {"$ne": ["$logs.work_hours", None]},
                                    {
                                        "$ne": [
                                            {"$type": "$logs.work_hours"},
                                            "missing",
                                        ]
                                    },
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_n": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", PRESENCE_LIST]},
                                    {"$ne": ["$logs.work_hours", None]},
                                    {
                                        "$ne": [
                                            {"$type": "$logs.work_hours"},
                                            "missing",
                                        ]
                                    },
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
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emps"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$eq": ["$work_n", 0]},
                        None,
                        {"$divide": ["$work_sum", "$work_n"]},
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(
    month_start: str,
    month_end: str,
    limit: int = 10,
    department: str | None = None,
) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
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
    if department is not None:
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
    return pipeline


def build_department_trend_pipeline(
    department: str, from_date: str, to_date: str
) -> list[dict[str, Any]]:
    """
    One row per calendar day via $densify.
    attendance_rate is half-up to 4dp in-pipeline (floor(x*1e4+0.5)/1e4);
    moving_avg_7d averages those already-rounded rates.
    """
    start = Date.fromisoformat(from_date)
    end = Date.fromisoformat(to_date)
    # Naive UTC midnights — $densify is happier without tz-aware bounds.
    from_dt = datetime(start.year, start.month, start.day)
    to_excl = datetime(end.year, end.month, end.day) + timedelta(days=1)

    # Half-up 4dp: floor(x * 10000 + 0.5) / 10000
    def round4(expr: Any) -> dict:
        return {
            "$divide": [
                {
                    "$floor": {
                        "$add": [{"$multiply": [expr, 10000]}, 0.5]
                    }
                },
                10000,
            ]
        }

    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "date": {"$literal": from_dt}}},
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_excl],
                },
            }
        },
        {
            "$set": {
                "date_str": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date",
                        "timezone": "UTC",
                    }
                },
                "is_working_day": {
                    "$in": [{"$dayOfWeek": "$date"}, WEEKDAY_DOW]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d"]},
                                ]
                            }
                        }
                    },
                    {"$count": "n"},
                ],
                "as": "_hc",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d": "$date_str"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "_e",
                        }
                    },
                    {"$unwind": "$_e"},
                    {"$match": {"_e.department": department}},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", PRESENCE_LIST]},
                                        {
                                            "$cond": [
                                                {
                                                    "$eq": [
                                                        {
                                                            "$ifNull": [
                                                                "$half_day",
                                                                False,
                                                            ]
                                                        },
                                                        True,
                                                    ]
                                                },
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                {"$ifNull": ["$late_minutes", 0]},
                                                0,
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "_stats",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [{"$arrayElemAt": ["$_hc.n", 0]}, 0]
                },
                "present_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$_stats.present_count", 0]},
                        0,
                    ]
                },
                "late_count": {
                    "$ifNull": [{"$arrayElemAt": ["$_stats.late_count", 0]}, 0]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$or": [
                                {"$eq": ["$is_working_day", False]},
                                {"$lte": ["$headcount", 0]},
                            ]
                        },
                        None,
                        round4({"$divide": ["$present_count", "$headcount"]}),
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date_str",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": [{"$type": "$moving_avg_7d_raw"}, "missing"]},
                        None,
                        {
                            "$cond": [
                                {"$eq": ["$moving_avg_7d_raw", None]},
                                None,
                                round4("$moving_avg_7d_raw"),
                            ]
                        },
                    ]
                },
            }
        },
    ]


def explain_aggregate(collection: str, pipeline: list[dict[str, Any]]) -> dict:
    raw = db.command(
        {
            "explain": {
                "aggregate": collection,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
    )
    return to_jsonable(raw)


# --------------------------------------------------------------------------- #
# Endpoints — system / employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        ensure_indexes()
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    require_db()
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0),
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return employee_to_api(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    require_db()
    q: dict = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [employee_to_api(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance write paths
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    require_db()
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    ms = body.punched_at if body.punched_at is not None else now_ms_truncated()
    punch_in_dt = ms_to_dt_truncated(ms)
    date_str = attendance_date_for_punch(
        punch_in_dt, emp["shift_start"], emp["shift_end"]
    )
    late_minutes, _wh, _ot, _hd = recompute_derived(
        body.status,
        punch_in_dt,
        None,
        date_str,
        emp["shift_start"],
        emp["shift_end"],
    )
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punch_in_dt,
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
        raise HTTPException(
            status_code=409, detail="already punched in for this date"
        )
    return attendance_to_api(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    require_db()
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    ms = body.punched_at if body.punched_at is not None else now_ms_truncated()
    punched_at = ms_to_dt_truncated(ms)

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punched_at},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")

    punch_in_dt = normalize_stored_dt(record["punch_in"])
    if punched_at <= punch_in_dt or punched_at > punch_in_dt + timedelta(hours=24):
        raise HTTPException(
            status_code=422,
            detail="punched_at must be after punch_in and within 24 hours",
        )

    _late, work_hours, overtime_minutes, half_day = recompute_derived(
        record["status"],
        punch_in_dt,
        punched_at,
        record["date"],
        emp["shift_start"],
        emp["shift_end"],
    )
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="already punched out")
    return attendance_to_api(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: Annotated[str, Path(description="Attendance date YYYY-MM-DD")],
    body: RegularizeIn,
):
    require_db()
    path_date = parse_path_date(date)

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": path_date})
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    fields_set = body.model_fields_set
    final_status = body.status if "status" in fields_set else record["status"]

    if final_status in ABSENCE_STATUSES:
        if "punch_in" in fields_set or "punch_out" in fields_set:
            raise HTTPException(
                status_code=422,
                detail="punch_in/punch_out cannot be supplied with ABSENT or LEAVE",
            )
        final_punch_in: datetime | None = None
        final_punch_out: datetime | None = None
    else:
        if "punch_in" in fields_set:
            final_punch_in = ms_to_dt_truncated(body.punch_in)  # type: ignore[arg-type]
        else:
            final_punch_in = normalize_stored_dt(record.get("punch_in"))
        if "punch_out" in fields_set:
            final_punch_out = ms_to_dt_truncated(body.punch_out)  # type: ignore[arg-type]
        else:
            final_punch_out = normalize_stored_dt(record.get("punch_out"))
        if final_punch_in is None:
            raise HTTPException(
                status_code=422, detail="presence status requires a punch_in"
            )
        computed = attendance_date_for_punch(
            final_punch_in, emp["shift_start"], emp["shift_end"]
        )
        if computed != path_date:
            raise HTTPException(
                status_code=422,
                detail="punch_in does not belong to this attendance date",
            )
        if final_punch_out is not None and (
            final_punch_out <= final_punch_in
            or final_punch_out > final_punch_in + timedelta(hours=24)
        ):
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in and within 24 hours",
            )

    late_minutes, work_hours, overtime_minutes, half_day = recompute_derived(
        final_status,
        final_punch_in,
        final_punch_out,
        path_date,
        emp["shift_start"],
        emp["shift_end"],
    )

    before = {
        "status": record["status"],
        "punch_in": normalize_stored_dt(record.get("punch_in")),
        "punch_out": normalize_stored_dt(record.get("punch_out")),
        "work_hours": record.get("work_hours"),
        "late_minutes": int(record.get("late_minutes") or 0),
        "overtime_minutes": int(record.get("overtime_minutes") or 0),
        "half_day": bool(record.get("half_day", False)),
    }
    after = {
        "status": final_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }
    changes: dict[str, dict[str, Any]] = {}
    for key in TRACKED_FIELDS:
        if not field_values_equal(key, before[key], after[key]):
            changes[key] = {"from": before[key], "to": after[key]}
    if not changes:
        raise HTTPException(status_code=422, detail="no changes")

    history_list = record.get("history")
    hist_n = len(history_list) if history_list is not None else 0
    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    filt: dict[str, Any] = {
        "_id": record["_id"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
    }
    if hist_n == 0:
        filt["$or"] = [
            {"history": {"$size": 0}},
            {"history": {"$exists": False}},
        ]
    else:
        filt["history"] = {"$size": hist_n}

    updated = db.attendance_logs.find_one_and_update(
        filt,
        {
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
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="concurrent modification")
    return attendance_to_api(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    require_db()
    filt, sort, skip, limit = build_attendance_query(
        emp_code=emp_code,
        date_from=date_from,
        date_to=date_to,
        status=status,
        page=page,
        page_size=page_size,
    )
    total = db.attendance_logs.count_documents(filt)
    cursor = db.attendance_logs.find(filt).sort(sort).skip(skip).limit(limit)
    items = [attendance_to_api(doc) for doc in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: Annotated[str, Query()],
):
    require_db()
    month, month_start, month_end = parse_month(month)

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    joined = Date.fromisoformat(emp["joined_on"])
    m_start = Date.fromisoformat(month_start)
    m_end = Date.fromisoformat(month_end)
    wd_start = max(m_start, joined)
    working_days = count_working_days(wd_start, m_end)

    pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)
    rows = list(db.attendance_logs.aggregate(pipeline))
    if rows:
        agg = rows[0]
        present_days = float(agg.get("present_days") or 0)
        leave_days = int(agg.get("leave_days") or 0)
        late_count = int(agg.get("late_count") or 0)
        total_late = int(agg.get("total_late_minutes") or 0)
        total_ot = int(agg.get("total_overtime_minutes") or 0)
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

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
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: Annotated[str, Query()],
    department: Optional[str] = None,
):
    require_db()
    month, month_start, month_end = parse_month(month)
    pipeline = build_department_summary_pipeline(
        month_start, month_end, department
    )
    items = []
    for row in db.employees.aggregate(pipeline):
        items.append(
            {
                "department": row["department"],
                "headcount": int(row["headcount"]),
                "present_days": float(row.get("present_days") or 0),
                "avg_work_hours": half_up(row.get("avg_work_hours"), 2),
                "late_count": int(row.get("late_count") or 0),
                "total_late_minutes": int(row.get("total_late_minutes") or 0),
                "leave_count": int(row.get("leave_count") or 0),
                "on_duty_count": int(row.get("on_duty_count") or 0),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Annotated[str, Query()],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    require_db()
    month, month_start, month_end = parse_month(month)
    pipeline = build_late_leaderboard_pipeline(
        month_start, month_end, limit, department
    )
    items = []
    for row in db.attendance_logs.aggregate(pipeline):
        items.append(
            {
                "rank": int(row["rank"]),
                "emp_code": row["emp_code"],
                "name": row["name"],
                "department": row["department"],
                "total_late_minutes": int(row["total_late_minutes"]),
                "late_count": int(row["late_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from")],
    to: Annotated[str, Query()],
):
    require_db()
    from_date = parse_path_date(from_)
    to_date = parse_path_date(to)
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to must be >= from")
    start = Date.fromisoformat(from_date)
    end = Date.fromisoformat(to_date)
    if (end - start).days > 92:
        raise HTTPException(status_code=422, detail="range exceeds 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    items = []
    for row in db.employees.aggregate(pipeline):
        items.append(
            {
                "date": row["date"],
                "is_working_day": bool(row["is_working_day"]),
                "headcount": int(row["headcount"]),
                "present_count": float(row["present_count"]),
                "late_count": int(row["late_count"]),
                "attendance_rate": row.get("attendance_rate"),
                "moving_avg_7d": row.get("moving_avg_7d"),
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Annotated[
        str,
        Path(
            pattern="^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"
        ),
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    require_db()

    if endpoint == "attendance_list":
        filt, sort, skip, lim = build_attendance_query(
            emp_code=emp_code,
            date_from=date_from,
            date_to=date_to,
            status=status,
            page=page,
            page_size=page_size,
        )
        raw = db.command(
            {
                "explain": {
                    "find": "attendance_logs",
                    "filter": filt,
                    "sort": dict(sort),
                    "skip": skip,
                    "limit": lim,
                },
                "verbosity": "executionStats",
            }
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": to_jsonable(raw),
        }

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422, detail="emp_code and month are required"
            )
        _m, month_start, month_end = parse_month(month)
        pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_aggregate("attendance_logs", pipeline),
        }

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        _m, month_start, month_end = parse_month(month)
        pipeline = build_department_summary_pipeline(
            month_start, month_end, department
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_aggregate("employees", pipeline),
        }

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        _m, month_start, month_end = parse_month(month)
        pipeline = build_late_leaderboard_pipeline(
            month_start, month_end, limit, department
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_aggregate("attendance_logs", pipeline),
        }

    if endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(
                status_code=422,
                detail="department, from and to are required",
            )
        from_date = parse_path_date(from_)
        to_date = parse_path_date(to)
        if to_date < from_date:
            raise HTTPException(status_code=422, detail="to must be >= from")
        if (Date.fromisoformat(to_date) - Date.fromisoformat(from_date)).days > 92:
            raise HTTPException(status_code=422, detail="range exceeds 92 days")
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_aggregate("employees", pipeline),
        }

    raise HTTPException(status_code=422, detail="unknown endpoint")
