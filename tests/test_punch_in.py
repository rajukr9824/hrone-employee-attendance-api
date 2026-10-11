from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

import app.main as main


class FakeEmployees:
    def __init__(self, employees):
        self.employees = employees

    def find_one(self, query):
        return self.employees.get(query["emp_code"])


class FakeAttendance:
    def __init__(self):
        self.documents = []
        self.lock = Lock()

    def insert_one(self, document):
        with self.lock:
            key = (document["emp_code"], document["date"])
            if any(
                (item["emp_code"], item["date"]) == key
                for item in self.documents
            ):
                raise DuplicateKeyError("duplicate attendance key")
            stored = dict(document)
            stored["_id"] = object()
            self.documents.append(stored)


class FakeDb:
    def __init__(self, employees):
        self.employees = FakeEmployees(employees)
        self.attendance_logs = FakeAttendance()


def epoch_ms(value):
    return int(value.timestamp() * 1000)


@pytest.fixture
def punch_client(monkeypatch):
    prefix = f"EMP{uuid4().hex[:6].upper()}"
    employees = {
        prefix: {
            "emp_code": prefix,
            "name": "Day Worker",
            "shift_start": "09:30",
            "shift_end": "18:30",
        },
        f"{prefix}A": {
            "emp_code": f"{prefix}A",
            "name": "Boundary Worker A",
            "shift_start": "09:30",
            "shift_end": "18:30",
        },
        f"{prefix}B": {
            "emp_code": f"{prefix}B",
            "name": "Boundary Worker B",
            "shift_start": "09:30",
            "shift_end": "18:30",
        },
        f"{prefix}C": {
            "emp_code": f"{prefix}C",
            "name": "Boundary Worker C",
            "shift_start": "09:30",
            "shift_end": "18:30",
        },
        f"{prefix}D": {
            "emp_code": f"{prefix}D",
            "name": "Midnight Worker D",
            "shift_start": "00:00",
            "shift_end": "08:00",
        },
        f"{prefix}E": {
            "emp_code": f"{prefix}E",
            "name": "Midnight Worker E",
            "shift_start": "00:00",
            "shift_end": "08:00",
        },
        f"{prefix}N": {
            "emp_code": f"{prefix}N",
            "name": "Night Worker",
            "shift_start": "22:00",
            "shift_end": "06:00",
        },
    }
    fake_db = FakeDb(employees)
    monkeypatch.setattr(main, "db", fake_db)
    client = TestClient(main.app)
    yield client, fake_db, prefix
    client.close()


def test_unknown_employee_and_status_validation(punch_client):
    client, _, prefix = punch_client
    assert client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP9999", "status": "PRESENT"},
    ).status_code == 404
    assert client.post(
        "/attendance/punch-in",
        json={"emp_code": prefix, "status": "ABSENT"},
    ).status_code == 422


def test_timestamp_validation_and_truncation(punch_client):
    client, _, prefix = punch_client
    invalid_values = [99_999_999_999, 4_102_444_800_001, 1.0, "1783312500000"]
    for value in invalid_values:
        assert client.post(
            "/attendance/punch-in",
            json={"emp_code": prefix, "punched_at": value},
        ).status_code == 422

    instant = datetime(2026, 7, 6, 4, 0, 0, 900_000, tzinfo=timezone.utc)
    response = client.post(
        "/attendance/punch-in",
        json={"emp_code": prefix, "punched_at": epoch_ms(instant)},
    )
    assert response.status_code == 201
    assert response.json()["punch_in"] == epoch_ms(instant.replace(microsecond=0))


def test_ist_midnight_and_late_boundaries(punch_client):
    client, _, prefix = punch_client
    cases = [
        (
            f"{prefix}A",
            datetime(2026, 7, 5, 18, 0, 0, tzinfo=timezone.utc),
            "2026-07-05",
            840,
        ),
        (
            f"{prefix}B",
            datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc),
            "2026-07-06",
            0,
        ),
        (
            f"{prefix}C",
            datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc),
            "2026-07-06",
            10,
        ),
        (
            f"{prefix}D",
            datetime(2026, 7, 6, 18, 29, 59, tzinfo=timezone.utc),
            "2026-07-06",
            1439,
        ),
        (
            f"{prefix}E",
            datetime(2026, 7, 6, 18, 30, 0, tzinfo=timezone.utc),
            "2026-07-07",
            0,
        ),
    ]
    for emp_code, instant, expected_date, expected_late in cases:
        response = client.post(
            "/attendance/punch-in",
            json={"emp_code": emp_code, "punched_at": epoch_ms(instant)},
        )
        assert response.status_code == 201
        assert response.json()["date"] == expected_date
        assert response.json()["late_minutes"] == expected_late


def test_overnight_shift_assigns_previous_attendance_date(punch_client):
    client, _, prefix = punch_client
    instant = datetime(2026, 7, 7, 0, 29, tzinfo=timezone.utc)
    response = client.post(
        "/attendance/punch-in",
        json={"emp_code": f"{prefix}N", "punched_at": epoch_ms(instant)},
    )
    assert response.status_code == 201
    assert response.json()["date"] == "2026-07-06"
    assert response.json()["late_minutes"] == 479


def test_punch_in_response_shape_and_defaults(punch_client):
    client, _, prefix = punch_client
    response = client.post(
        "/attendance/punch-in",
        json={"emp_code": prefix, "status": "WFH"},
    )
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "emp_code",
        "date",
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
        "history",
    }
    assert body["status"] == "WFH"
    assert body["punch_out"] is None
    assert body["work_hours"] is None
    assert body["overtime_minutes"] == 0
    assert body["half_day"] is False
    assert body["history"] == []
    assert "_id" not in body
    assert "id" not in body


def test_concurrent_duplicate_punch_ins(punch_client):
    client, fake_db, prefix = punch_client
    payload = {
        "emp_code": prefix,
        "punched_at": epoch_ms(
            datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)
        ),
    }

    def send_request():
        request_client = TestClient(main.app)
        try:
            return request_client.post("/attendance/punch-in", json=payload).status_code
        finally:
            request_client.close()

    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(lambda _: send_request(), range(8)))

    assert statuses.count(201) == 1
    assert statuses.count(409) == 7
    assert len(fake_db.attendance_logs.documents) == 1
