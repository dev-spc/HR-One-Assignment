"""Employee Attendance & Analytics API. Run: uvicorn app.main:app --port 8000"""
import json
import logging
import os
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import AfterValidator, BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # real environment variables win over .env
log = logging.getLogger("attendance")

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
DAY = timedelta(days=1)


class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


PRESENCE = [s.value for s in PresenceStatus]


def _iso_date(v: str) -> str:
    if date.fromisoformat(v).isoformat() != v:
        raise ValueError("date must be YYYY-MM-DD")
    return v


DateStr = Annotated[str, AfterValidator(_iso_date)]
MonthStr = Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
EpochMillis = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


class Clock:
    """Instant conversions (R1 second truncation) and half-up rounding (R4, R8)."""

    @staticmethod
    def from_ms(ms: int) -> datetime:
        return datetime.fromtimestamp(ms // 1000, UTC)

    @staticmethod
    def to_ms(dt: Optional[datetime]) -> Optional[int]:
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return (dt - EPOCH) // timedelta(milliseconds=1)

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC).replace(microsecond=0)

    @staticmethod
    def half_up(value: Decimal, places: int) -> float:
        return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))

    @staticmethod
    def weekdays(start: date, end: date) -> int:
        return sum((start + timedelta(i)).weekday() < 5 for i in range((end - start).days + 1))


class Shift:
    """An employee's IST shift window, including overnight shifts (R1)."""

    def __init__(self, emp: dict):
        self.start, self.end = emp["shift_start"], emp["shift_end"]
        self.overnight = self.end <= self.start

    @staticmethod
    def _at(day: str, hhmm: str) -> datetime:
        return datetime.fromisoformat(f"{day}T{hhmm}").replace(tzinfo=IST).astimezone(UTC)

    def start_at(self, day: str) -> datetime:
        return self._at(day, self.start)

    def end_at(self, day: str) -> datetime:
        return self._at(day, self.end) + (DAY if self.overnight else timedelta())

    def attendance_date(self, punch_in: datetime) -> str:
        local = punch_in.astimezone(IST)
        if self.overnight and local.time() < time.fromisoformat(self.end):
            local -= DAY
        return local.date().isoformat()


class Rules:
    """R2-R5: the only place derived fields are computed."""

    @staticmethod
    def late_minutes(punch_in: datetime, shift_start: datetime) -> int:
        secs = int((punch_in - shift_start).total_seconds())
        return secs // 60 if secs > 600 else 0

    @staticmethod
    def overtime_minutes(punch_out: datetime, shift_end: datetime) -> int:
        secs = int((punch_out - shift_end).total_seconds())
        return secs // 60 if secs >= 1800 else 0

    @staticmethod
    def work_hours(punch_in: datetime, punch_out: datetime) -> float:
        return Clock.half_up(Decimal(int((punch_out - punch_in).total_seconds())) / 3600, 2)

    @staticmethod
    def derive(shift: Shift, day: str, status: str, punch_in, punch_out) -> dict:
        d = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
        if status in PRESENCE and punch_in is not None:
            d["late_minutes"] = Rules.late_minutes(punch_in, shift.start_at(day))
            if punch_out is not None:
                d["work_hours"] = Rules.work_hours(punch_in, punch_out)
                d["overtime_minutes"] = Rules.overtime_minutes(punch_out, shift.end_at(day))
                d["half_day"] = d["work_hours"] < 4.5
        return d


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Status = None  # omitted = unchanged; an explicit null is rejected
    punch_in: EpochMillis = None
    punch_out: EpochMillis = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


class Serializer:
    """MongoDB documents to API bodies: instants as epoch ms, never _id."""

    EMPLOYEE_FIELDS = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")

    @staticmethod
    def employee(doc: dict) -> dict:
        return {k: doc[k] for k in Serializer.EMPLOYEE_FIELDS} | {"created_at": Clock.to_ms(doc["created_at"])}

    @staticmethod
    def record(doc: dict) -> dict:
        return {
            "emp_code": doc["emp_code"],
            "date": doc["date"],
            "status": doc["status"],
            "punch_in": Clock.to_ms(doc.get("punch_in")),
            "punch_out": Clock.to_ms(doc.get("punch_out")),
            "work_hours": doc.get("work_hours"),
            "late_minutes": doc.get("late_minutes") or 0,
            "overtime_minutes": doc.get("overtime_minutes") or 0,
            "half_day": doc.get("half_day") or False,
            "history": [Serializer._history(h) for h in doc.get("history") or []],
        }

    @staticmethod
    def _history(h: dict) -> dict:
        changes = {
            k: {"from": Clock.to_ms(v["from"]), "to": Clock.to_ms(v["to"])} if k in ("punch_in", "punch_out") else v
            for k, v in h["changes"].items()
        }
        return {"at": Clock.to_ms(h["at"]), "by": h["by"], "reason": h["reason"], "changes": changes}


class Store:
    """MongoDB connection, indexes and shared lookups."""

    def __init__(self):
        self.client = MongoClient(
            os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=3000
        )
        self.db = self.client[os.getenv("MONGO_DB", "attendance_db")]
        self.employees = self.db.employees
        self.logs = self.db.attendance_logs
        self.indexed = False

    def ping(self):
        self.db.command("ping")

    def ensure_indexes(self):
        if self.indexed:
            return
        self.employees.create_index([("emp_code", ASCENDING)], unique=True)
        self.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
        self.employees.create_index([("joined_on", ASCENDING)])
        self.logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        self.logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        self.indexed = True

    def employee(self, emp_code: str) -> dict:
        emp = self.employees.find_one({"emp_code": emp_code})
        if not emp:
            raise HTTPException(404, "employee not found")
        return emp

    @staticmethod
    def page(coll, query: dict, sort: list, page: int, page_size: int, out) -> dict:
        docs = coll.find(query).sort(sort).skip((page - 1) * page_size).limit(page_size)
        return {"items": [out(d) for d in docs], "total": coll.count_documents(query), "page": page, "page_size": page_size}


class Attendance:
    """Punch-in, punch-out and regularisation with race-safe writes."""

    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _instant(ms: Optional[int]) -> datetime:
        return Clock.from_ms(ms) if ms else Clock.now()

    @staticmethod
    def _check_window(punch_in: datetime, punch_out: datetime):
        if punch_out <= punch_in or punch_out - punch_in > DAY:
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    def punch_in(self, body: PunchInRequest) -> dict:
        emp = self.store.employee(body.emp_code)
        shift, ts = Shift(emp), self._instant(body.punched_at)
        day = shift.attendance_date(ts)
        doc = {
            "emp_code": emp["emp_code"], "date": day, "status": body.status.value, "punch_in": ts, "punch_out": None,
            **Rules.derive(shift, day, body.status.value, ts, None), "history": [],
        }
        try:
            self.store.logs.insert_one(doc)  # unique (emp_code, date) index decides the race
        except DuplicateKeyError:
            raise HTTPException(409, "already punched in for this date")
        return doc

    def punch_out(self, body: PunchOutRequest) -> dict:
        emp = self.store.employee(body.emp_code)
        ts = self._instant(body.punched_at)
        rec = self.store.logs.find_one({"emp_code": emp["emp_code"], "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
        if not rec:
            raise HTTPException(404, "no punch-in found")
        if rec.get("punch_out") is not None:
            raise HTTPException(409, "already punched out")
        self._check_window(rec["punch_in"], ts)
        derived = Rules.derive(Shift(emp), rec["date"], rec["status"], rec["punch_in"], ts)
        updated = self.store.logs.find_one_and_update(
            {"_id": rec["_id"], "punch_out": None}, {"$set": {"punch_out": ts, **derived}}, return_document=ReturnDocument.AFTER
        )
        if not updated:
            raise HTTPException(409, "already punched out")
        return updated

    def regularize(self, emp_code: str, day: str, body: RegularizeRequest) -> dict:
        emp = self.store.employee(emp_code)
        rec = self.store.logs.find_one({"emp_code": emp_code, "date": day})
        if not rec:
            raise HTTPException(404, "no record for that date")
        sent = body.model_fields_set
        status = body.status.value if "status" in sent else rec["status"]
        if status in PRESENCE:
            punch_in = Clock.from_ms(body.punch_in) if "punch_in" in sent else rec.get("punch_in")
            punch_out = Clock.from_ms(body.punch_out) if "punch_out" in sent else rec.get("punch_out")
            if punch_in is None:
                raise HTTPException(422, "a presence status requires punch_in")
            if Shift(emp).attendance_date(punch_in) != day:
                raise HTTPException(422, "punch_in must stay on the record's attendance date")
            if punch_out is not None:
                self._check_window(punch_in, punch_out)
        else:
            if "punch_in" in sent or "punch_out" in sent:
                raise HTTPException(422, f"{status} cannot have punch times")
            punch_in = punch_out = None
        new = {"status": status, "punch_in": punch_in, "punch_out": punch_out, **Rules.derive(Shift(emp), day, status, punch_in, punch_out)}
        old = Serializer.record(rec) | {"punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out")}
        changes = {k: {"from": old[k], "to": new[k]} for k in new if new[k] != old[k]}
        if not changes:
            raise HTTPException(422, "request changes nothing")
        entry = {"at": Clock.now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        # the guard makes the update fail if another correction landed in between
        guard = {
            "_id": rec["_id"], "status": rec["status"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
            "history": {"$size": len(rec["history"])} if "history" in rec else {"$exists": False},
        }
        updated = self.store.logs.find_one_and_update(guard, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
        if not updated:
            raise HTTPException(409, "record was modified concurrently")
        return updated

    @staticmethod
    def list_query(emp_code, date_from, date_to, status) -> tuple[dict, list]:
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not be after date_to")
        query = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {op: v for op, v in (("$gte", date_from), ("$lte", date_to)) if v}
        if status is not None:
            query["status"] = status.value
        # with emp_code fixed, sorting by date alone gives the same order and lets the (emp_code, date) index serve it
        sort = [("date", DESCENDING)] if emp_code is not None else [("date", DESCENDING), ("emp_code", ASCENDING)]
        return query, sort


class Analytics:
    """Aggregation pipelines. Builders return (collection, pipeline) and are shared with explain."""

    IS_PRESENT = {"$in": ["$status", PRESENCE]}
    IS_LATE = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}
    HAS_WORK_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
    DAY_VALUE = {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}
    ON_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]}
    PRESENT_WEEKDAY = {"$cond": [{"$and": [IS_PRESENT, ON_WEEKDAY]}, DAY_VALUE, 0]}

    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _count(cond) -> dict:
        return {"$sum": {"$cond": [cond, 1, 0]}}

    @staticmethod
    def month_range(month: str) -> tuple[str, str]:
        y, m = map(int, month.split("-"))
        return f"{month}-01", f"{month}-{monthrange(y, m)[1]:02d}"

    @staticmethod
    def trend_range(frm: str, to: str) -> tuple[datetime, datetime]:
        lo, hi = (datetime.fromisoformat(d).replace(tzinfo=UTC) for d in (frm, to))
        if hi < lo or (hi - lo).days + 1 > 92:
            raise HTTPException(422, "range must be ascending and at most 92 days")
        return lo, hi

    def _run(self, built) -> list:
        coll, pipeline = built
        return list(self.store.db[coll].aggregate(pipeline))

    def employee_monthly(self, emp_code: str, month: str):
        start, end = self.month_range(month)
        return "attendance_logs", [
            {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
            {"$group": {
                "_id": None,
                "present_days": {"$sum": self.PRESENT_WEEKDAY},
                "leave_days": self._count({"$eq": ["$status", "LEAVE"]}),
                "late_count": self._count(self.IS_LATE),
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }},
        ]

    def run_employee_monthly(self, emp: dict, month: str) -> dict:
        rows = self._run(self.employee_monthly(emp["emp_code"], month))
        r = rows[0] if rows else {}
        start, end = self.month_range(month)
        working = Clock.weekdays(max(date.fromisoformat(start), date.fromisoformat(emp["joined_on"])), date.fromisoformat(end))
        present = float(r.get("present_days", 0))
        return {
            "emp_code": emp["emp_code"], "month": month, "working_days": working, "present_days": present,
            "leave_days": r.get("leave_days", 0), "late_count": r.get("late_count", 0),
            "total_late_minutes": int(r.get("total_late_minutes", 0)),
            "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
            "attendance_pct": Clock.half_up(Decimal(present) / working * 100, 2) if working else None,
        }

    def department_summary(self, month: str, department: Optional[str]):
        start, end = self.month_range(month)
        match = {"joined_on": {"$lte": end}}  # R9: joined by month end, with or without logs
        if department is not None:
            match["department"] = department
        return "employees", [
            {"$match": match},
            {"$lookup": {
                "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "log",
                "pipeline": [
                    {"$match": {"date": {"$gte": start, "$lte": end}}},
                    {"$project": {"_id": 0, "date": 1, "status": 1, "work_hours": 1, "late_minutes": 1, "half_day": 1}},
                ],
            }},
            {"$unwind": {"path": "$log", "preserveNullAndEmptyArrays": True}},
            {"$replaceWith": {"$mergeObjects": ["$$ROOT", "$log"]}},
            {"$group": {
                "_id": "$department",
                "members": {"$addToSet": "$emp_code"},
                "present_days": {"$sum": self.PRESENT_WEEKDAY},
                "wh_sum": {"$sum": {"$cond": [self.HAS_WORK_HOURS, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
                "wh_n": self._count(self.HAS_WORK_HOURS),
                "late_count": self._count(self.IS_LATE),
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": self._count({"$eq": ["$status", "LEAVE"]}),
                "on_duty_count": self._count({"$eq": ["$status", "ON_DUTY"]}),
            }},
            {"$sort": {"_id": 1}},
        ]

    def run_department_summary(self, month: str, department: Optional[str]) -> dict:
        items = [
            {
                "department": r["_id"], "headcount": len(r["members"]), "present_days": float(r["present_days"]),
                "avg_work_hours": Clock.half_up(Decimal(int(r["wh_sum"])) / (100 * r["wh_n"]), 2) if r["wh_n"] else None,
                "late_count": r["late_count"], "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": r["leave_count"], "on_duty_count": r["on_duty_count"],
            }
            for r in self._run(self.department_summary(month, department))
        ]
        return {"month": month, "items": items}

    def late_leaderboard(self, month: str, limit: int, department: Optional[str]):
        start, end = self.month_range(month)
        pipeline = [
            {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},  # drops logs whose employee does not exist
        ]
        if department is not None:
            pipeline.append({"$match": {"emp.department": department}})
        pipeline += [
            # $rank = competition ranking (1, 2, 2, 4); sortBy must not break ties
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$project": {
                "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                "total_late_minutes": 1, "late_count": 1,
            }},
        ]
        return "attendance_logs", pipeline

    def run_late_leaderboard(self, month: str, limit: int, department: Optional[str]) -> dict:
        return {"month": month, "items": self._run(self.late_leaderboard(month, limit, department))}

    def department_trend(self, department: str, frm: str, to: str):
        lo, hi = self.trend_range(frm, to)
        # every employee emits one headcount event inside the range; every log emits one presence event
        join_event = {
            "day": {"$cond": [{"$lte": ["$joined_on", frm]}, frm, {"$cond": [{"$lte": ["$joined_on", to]}, "$joined_on", frm]}]},
            "hc": {"$cond": [{"$lte": ["$joined_on", to]}, 1, 0]}, "present": 0, "late": 0,
        }
        log_event = {
            "day": "$$l.date", "hc": 0,
            "present": {"$cond": [{"$in": ["$$l.status", PRESENCE]}, {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1]}, 0]},
            "late": {"$cond": [{"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}, 1, 0]},
        }
        rate = {"$floor": {"$add": [{"$divide": [{"$multiply": ["$present_count", 10000]}, "$headcount"]}, 0.5]}}
        window = {"documents": [-6, 0]}
        return "employees", [
            {"$match": {"department": department}},
            {"$lookup": {
                "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs",
                "pipeline": [
                    {"$match": {"date": {"$gte": frm, "$lte": to}}},
                    {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
                ],
            }},
            {"$project": {"events": {"$concatArrays": [[join_event], {"$map": {"input": "$logs", "as": "l", "in": log_event}}]}}},
            {"$unwind": "$events"},
            {"$group": {
                "_id": {"$dateFromString": {"dateString": "$events.day"}},
                "hc": {"$sum": "$events.hc"}, "present_count": {"$sum": "$events.present"}, "late_count": {"$sum": "$events.late"},
            }},
            {"$project": {"_id": 0, "day": "$_id", "hc": 1, "present_count": 1, "late_count": 1}},
            {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lo, hi + DAY]}}},
            {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"headcount": {"$sum": "$hc", "window": {"documents": ["unbounded", "current"]}}}}},
            {"$addFields": {
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
                "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]},
            }},
            # rate_e4 = attendance_rate in ten-thousandths, half-up, so the window maths stays exact
            {"$addFields": {"rate_e4": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, rate, None]}}},
            {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
                "win_sum": {"$sum": "$rate_e4", "window": window},
                "win_n": {"$sum": {"$cond": [{"$eq": ["$rate_e4", None]}, 0, 1]}, "window": window},
            }}},
            {"$sort": {"day": 1}},
            {"$project": {
                "_id": 0, "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}}, "is_working_day": 1,
                "headcount": 1, "present_count": 1, "late_count": 1, "rate_e4": 1, "win_sum": 1, "win_n": 1,
            }},
        ]

    def run_department_trend(self, department: str, frm: str, to: str) -> dict:
        items = [
            {
                "date": r["date"], "is_working_day": r["is_working_day"], "headcount": int(r["headcount"]),
                "present_count": float(r["present_count"]), "late_count": int(r["late_count"]),
                "attendance_rate": int(r["rate_e4"]) / 10000 if r["rate_e4"] is not None else None,
                "moving_avg_7d": Clock.half_up(Decimal(int(r["win_sum"])) / (10000 * r["win_n"]), 4) if r["win_n"] else None,
            }
            for r in self._run(self.department_trend(department, frm, to))
        ]
        return {"department": department, "items": items}


class Endpoint(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


class Explainer:
    """explain(executionStats) of the exact query or pipeline an endpoint runs."""

    def __init__(self, store: Store, analytics: Analytics):
        self.store, self.analytics = store, analytics

    @staticmethod
    def _require(params: dict, *names):
        missing = [n for n in names if params.get(n) is None]
        if missing:
            raise HTTPException(422, [{"loc": ["query", n], "msg": "Field required", "type": "missing"} for n in missing])

    def explain(self, endpoint: Endpoint, p: dict) -> dict:
        if endpoint is Endpoint.attendance_list:
            query, sort = Attendance.list_query(p["emp_code"], p["date_from"], p["date_to"], p["status"])
            coll = "attendance_logs"
            cmd = {"find": coll, "filter": query, "sort": dict(sort), "skip": (p["page"] - 1) * p["page_size"], "limit": p["page_size"]}
        else:
            if endpoint is Endpoint.employee_monthly:
                self._require(p, "emp_code", "month")
                coll, pipeline = self.analytics.employee_monthly(p["emp_code"], p["month"])
            elif endpoint is Endpoint.department_summary:
                self._require(p, "month")
                coll, pipeline = self.analytics.department_summary(p["month"], p["department"])
            elif endpoint is Endpoint.late_leaderboard:
                self._require(p, "month")
                coll, pipeline = self.analytics.late_leaderboard(p["month"], p["limit"], p["department"])
            else:
                self._require(p, "department", "from", "to")
                coll, pipeline = self.analytics.department_trend(p["department"], p["from"], p["to"])
            cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}
        out = self.store.db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint.value, "collection": coll, "explain": json.loads(json_util.dumps(out))}


store = Store()
attendance = Attendance(store)
analytics = Analytics(store)
explainer = Explainer(store, analytics)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        store.ensure_indexes()
    except PyMongoError as e:
        log.warning("indexes not created at startup (%s); /health will retry", e)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.get("/health")
def health():
    try:
        store.ping()
        store.ensure_indexes()
    except PyMongoError as e:
        raise HTTPException(503, f"mongodb unavailable: {e}")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump() | {"created_at": Clock.now()}
    try:
        store.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return Serializer.employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {"department": department} if department is not None else {}
    return Store.page(store.employees, query, [("emp_code", ASCENDING)], page, page_size, Serializer.employee)


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    return Serializer.record(attendance.punch_in(body))


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    return Serializer.record(attendance.punch_out(body))


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None, date_from: Optional[DateStr] = None, date_to: Optional[DateStr] = None,
    status: Optional[Status] = None, page: Page = 1, page_size: PageSize = 20,
):
    query, sort = Attendance.list_query(emp_code, date_from, date_to, status)
    return Store.page(store.logs, query, sort, page, page_size, Serializer.record)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: DateStr, body: RegularizeRequest):
    return Serializer.record(attendance.regularize(emp_code, date, body))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthStr):
    return analytics.run_employee_monthly(store.employee(emp_code), month)


@app.get("/analytics/departments/summary")
def department_summary(month: MonthStr, department: Optional[str] = None):
    return analytics.run_department_summary(month, department)


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthStr, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None):
    return analytics.run_late_leaderboard(month, limit, department)


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[DateStr, Query(alias="from")], to: DateStr):
    if not store.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")
    return analytics.run_department_trend(department, from_, to)


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Endpoint, emp_code: Optional[str] = None, month: Optional[MonthStr] = None, department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10, date_from: Optional[DateStr] = None, date_to: Optional[DateStr] = None,
    status: Optional[Status] = None, from_: Annotated[Optional[DateStr], Query(alias="from")] = None,
    to: Optional[DateStr] = None, page: Page = 1, page_size: PageSize = 20,
):
    params = {
        "emp_code": emp_code, "month": month, "department": department, "limit": limit, "date_from": date_from,
        "date_to": date_to, "status": status, "from": from_, "to": to, "page": page, "page_size": page_size,
    }
    return explainer.explain(endpoint, params)
