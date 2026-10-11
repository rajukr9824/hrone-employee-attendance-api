from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as main


def _employee(code, name, department, joined_on):
    return {
        "emp_code": code,
        "name": name,
        "department": department,
        "joined_on": joined_on,
        "shift_start": "09:30",
        "shift_end": "18:30",
    }


def _log(code, day, status="PRESENT", late=0, overtime=0, hours=8.0, half_day=False):
    return {
        "emp_code": code,
        "date": day,
        "status": status,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "work_hours": hours,
        "half_day": half_day,
    }


class AnalyticsCollection:
    def __init__(self, documents, kind):
        self.documents = documents
        self.kind = kind

    def find_one(self, query):
        for document in self.documents:
            if all(document.get(key) == value for key, value in query.items()):
                return document
        return None

    def count_documents(self, query):
        return sum(
            all(
                document.get(key) == value
                or (
                    isinstance(value, dict)
                    and (
                        ("$lte" in value and document.get(key) <= value["$lte"])
                        or ("$gte" in value and document.get(key) >= value["$gte"])
                    )
                )
                for key, value in query.items()
            )
            for document in self.documents
        )

    def aggregate(self, pipeline):
        if self.kind == "employees":
            return self._summary()
        first_match = next(stage["$match"] for stage in pipeline if "$match" in stage)
        if "$densify" in {key for stage in pipeline for key in stage}:
            self.department = next(
                stage["$match"]["employee.department"]
                for stage in pipeline
                if "$match" in stage and "employee.department" in stage["$match"]
            )
            return self._trend(first_match)
        if "late_minutes" in first_match.get("$match", first_match):
            return self._leaderboard(first_match)
        return self._monthly(first_match)

    def _monthly(self, match):
        code = match["emp_code"]
        bounds = match["date"]
        rows = [
            row
            for row in self.documents
            if row["emp_code"] == code
            and bounds["$gte"] <= row["date"] < bounds["$lt"]
        ]
        present = leave = late_count = late = overtime = 0
        for row in rows:
            day = date.fromisoformat(row["date"])
            if row["status"] in main.PRESENCE_STATUSES and day.weekday() < 5:
                present += 0.5 if row.get("half_day") else 1.0
            leave += row["status"] == "LEAVE"
            late_count += row.get("late_minutes", 0) > 0
            late += row.get("late_minutes", 0)
            overtime += row.get("overtime_minutes", 0)
        return [
            {
                "present_days": present,
                "leave_days": leave,
                "late_count": late_count,
                "total_late_minutes": late,
                "total_overtime_minutes": overtime,
            }
        ] if rows else []

    def _leaderboard(self, match):
        bounds = match["date"]
        grouped = {}
        for row in self.documents:
            if bounds["$gte"] <= row["date"] < bounds["$lt"] and row.get("late_minutes", 0) > 0:
                item = grouped.setdefault(row["emp_code"], [0, 0])
                item[0] += row["late_minutes"]
                item[1] += 1
        employees = {row["emp_code"]: row for row in self._db.employees.documents}
        result = []
        for code, (total, count) in grouped.items():
            employee = employees.get(code)
            if employee is not None:
                result.append(
                    {
                        "emp_code": code,
                        "name": employee["name"],
                        "department": employee["department"],
                        "total_late_minutes": total,
                        "late_count": count,
                    }
                )
        result.sort(key=lambda row: (-row["total_late_minutes"], row["emp_code"]))
        return result

    def _trend(self, match):
        bounds = match["date"]
        department = self.department
        # The route's aggregation is validated by the endpoint; this fake returns
        # the requested date series to exercise response calculations.
        start = date.fromisoformat(bounds["$gte"])
        end = date.fromisoformat(bounds["$lte"])
        employees = [
            row for row in self._db.employees.documents if row["department"] == self.department
        ]
        rows = []
        current = start
        while current <= end:
            day = current.isoformat()
            logs = [
                row for row in self.documents
                if row["date"] == day and row["emp_code"] in {e["emp_code"] for e in employees}
            ]
            rows.append(
                {
                    "day": datetime.combine(current, datetime.min.time(), tzinfo=timezone.utc),
                    "present_count": sum(
                        0.5 if row.get("half_day") else 1.0
                        for row in logs
                        if row["status"] in main.PRESENCE_STATUSES
                    ),
                    "late_count": sum(row.get("late_minutes", 0) > 0 for row in logs),
                }
            )
            current += timedelta(days=1)
        return rows

    def _summary(self):
        result = []
        for department in sorted({row["department"] for row in self._db.employees.documents}):
            employees = [
                row for row in self._db.employees.documents if row["department"] == department
            ]
            rows = [
                row
                for row in self._db.attendance_logs.documents
                if row["emp_code"] in {e["emp_code"] for e in employees}
            ]
            result.append(
                {
                    "_id": department,
                    "headcount": len(employees),
                    "present_days": sum(
                        0.5 if row.get("half_day") else 1.0
                        for row in rows
                        if row["status"] in main.PRESENCE_STATUSES
                        and date.fromisoformat(row["date"]).weekday() < 5
                    ),
                    "work_hours_sum": sum(
                        row["work_hours"] for row in rows
                        if row["status"] in main.PRESENCE_STATUSES and row.get("work_hours") is not None
                    ),
                    "work_hours_count": sum(
                        row["status"] in main.PRESENCE_STATUSES and row.get("work_hours") is not None
                        for row in rows
                    ),
                    "late_count": sum(row.get("late_minutes", 0) > 0 for row in rows),
                    "total_late_minutes": sum(row.get("late_minutes", 0) for row in rows),
                    "leave_count": sum(row["status"] == "LEAVE" for row in rows),
                    "on_duty_count": sum(row["status"] == "ON_DUTY" for row in rows),
                }
            )
        return result


class AnalyticsDb:
    def __init__(self, employees, logs):
        self.employees = AnalyticsCollection(employees, "employees")
        self.attendance_logs = AnalyticsCollection(logs, "attendance")
        self.employees.attendance_logs = self.attendance_logs
        self.attendance_logs._db = self
        self.employees._db = self


@pytest.fixture
def analytics_client(monkeypatch):
    employees = [
        _employee("EMP1000", "Asha", "Engineering", "2026-07-01"),
        _employee("EMP1001", "Bina", "Engineering", "2026-07-15"),
        _employee("EMP1002", "Chetan", "Sales", "2026-01-01"),
    ]
    logs = [
        _log("EMP1000", "2026-07-01", late=10, overtime=30, hours=8.25),
        _log("EMP1000", "2026-07-04", late=20, half_day=True),
        _log("EMP1001", "2026-07-15", status="LEAVE"),
        _log("EMP1002", "2026-07-01", late=10),
        _log("EMP9999", "2026-07-02", late=100),
    ]
    fake_db = AnalyticsDb(employees, logs)
    monkeypatch.setattr(main, "db", fake_db)
    client = TestClient(main.app)
    yield client
    client.close()


def test_employee_monthly_and_unknown_employee(analytics_client):
    response = analytics_client.get(
        "/analytics/employees/EMP1000/monthly", params={"month": "2026-07"}
    )
    assert response.status_code == 200
    assert response.json() == {
        "emp_code": "EMP1000",
        "month": "2026-07",
        "working_days": 23,
        "present_days": 1.0,
        "leave_days": 0,
        "late_count": 2,
        "total_late_minutes": 30,
        "total_overtime_minutes": 30,
        "attendance_pct": 4.35,
    }
    assert analytics_client.get(
        "/analytics/employees/EMP9998/monthly", params={"month": "2026-07"}
    ).status_code == 404


def test_department_summary_includes_zero_log_employee_and_filter(analytics_client):
    response = analytics_client.get(
        "/analytics/departments/summary", params={"month": "2026-07", "department": "Engineering"}
    )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["department"] == "Engineering"
    assert item["headcount"] == 2
    assert item["present_days"] == 1.0
    assert item["avg_work_hours"] == 8.13


def test_late_leaderboard_ties_and_unknown_employee_are_ignored(analytics_client):
    response = analytics_client.get(
        "/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1}
    )
    assert response.status_code == 200
    assert response.json()["items"] == [
        {
            "emp_code": "EMP1000",
            "name": "Asha",
            "department": "Engineering",
            "total_late_minutes": 30,
            "late_count": 2,
            "rank": 1,
        }
    ]


def test_department_trend_includes_weekend_gap_and_rates(analytics_client):
    response = analytics_client.get(
        "/analytics/departments/Engineering/trend",
        params={"from": "2026-07-03", "to": "2026-07-05"},
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["date"] for item in items] == ["2026-07-03", "2026-07-04", "2026-07-05"]
    assert items[0]["attendance_rate"] == 0.0
    assert items[1]["attendance_rate"] is None
    assert items[2]["attendance_rate"] is None
    assert analytics_client.get(
        "/analytics/departments/Unknown/trend",
        params={"from": "2026-07-03", "to": "2026-07-03"},
    ).status_code == 404
