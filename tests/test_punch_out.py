from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

import app.main as main


def epoch_ms(value):
    return int(value.timestamp() * 1000)


class FakeEmployees:
    def __init__(self, employee):
        self.employee = employee

    def find_one(self, query):
        if query["emp_code"] == self.employee["emp_code"]:
            return self.employee
        return None


class FakeCursor:
    def __init__(self, documents):
        self.documents = documents

    def sort(self, fields, direction=None):
        if isinstance(fields, str):
            fields = [(fields, direction)]
        for field, sort_direction in reversed(fields):
            self.documents.sort(
                key=lambda item: item.get(field),
                reverse=sort_direction == main.DESCENDING,
            )
        return self

    def __iter__(self):
        return iter(self.documents)


class FakeAttendance:
    def __init__(self, documents):
        self.documents = documents
        self.lock = Lock()

    def find_one(self, query, projection=None, sort=None):
        matches = [
            document
            for document in self.documents
            if document["emp_code"] == query["emp_code"]
            and document.get("punch_in") is not None
            and document["punch_in"] <= query["punch_in"]["$lte"]
            and (
                (query.get("punch_out") is None and document.get("punch_out") is None)
                or (
                    query.get("punch_out") == {"$ne": None}
                    and document.get("punch_out") is not None
                )
            )
        ]
        if sort:
            matches.sort(
                key=lambda item: item["punch_in"],
                reverse=sort[0][1] == main.DESCENDING,
            )
        return dict(matches[0]) if matches else None

    def find_one_and_update(
        self,
        query,
        update,
        projection=None,
        return_document=None,
    ):
        with self.lock:
            for document in self.documents:
                if (
                    document.get("_id") == query["_id"]
                    and document.get("punch_out") is None
                ):
                    document.update(update["$set"])
                    return {
                        key: value
                        for key, value in document.items()
                        if key != "_id"
                    }
        return None


class FakeDb:
    def __init__(self, employee, documents):
        self.employees = FakeEmployees(employee)
        self.attendance_logs = FakeAttendance(documents)


@pytest.fixture
def punch_out_client(monkeypatch):
    prefix = f"EMP{uuid4().hex[:6].upper()}"
    employee = {
        "_id": object(),
        "emp_code": prefix,
        "name": "Punch Out Worker",
        "shift_start": "09:30",
        "shift_end": "18:30",
    }
    punch_in = datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)
    documents = [
        {
            "_id": object(),
            "emp_code": prefix,
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": punch_in,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
    ]
    fake_db = FakeDb(employee, documents)
    monkeypatch.setattr(main, "db", fake_db)
    client = TestClient(main.app)
    yield client, fake_db, prefix, punch_in
    client.close()


def test_punch_out_serializes_and_calculates_fields(punch_out_client):
    client, fake_db, prefix, punch_in = punch_out_client
    punch_out = datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)
    response = client.post(
        "/attendance/punch-out",
        json={"emp_code": prefix, "punched_at": epoch_ms(punch_out)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["punch_in"] == epoch_ms(punch_in)
    assert body["punch_out"] == epoch_ms(punch_out)
    assert body["work_hours"] == 9.0
    assert body["overtime_minutes"] == 0
    assert body["half_day"] is False
    assert body["history"] == []
    assert "_id" not in body
    assert "id" not in body
    assert fake_db.attendance_logs.documents[0]["history"] == []


def test_punch_out_rejects_unknown_missing_and_invalid_time(punch_out_client):
    client, _, prefix, punch_in = punch_out_client
    assert client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP9999"},
    ).status_code == 404
    assert client.post(
        "/attendance/punch-out",
        json={"emp_code": prefix, "punched_at": epoch_ms(punch_in)},
    ).status_code == 422
    assert client.post(
        "/attendance/punch-out",
        json={
            "emp_code": prefix,
            "punched_at": epoch_ms(punch_in.replace(year=2028)),
        },
    ).status_code == 422


def test_concurrent_punch_out_is_atomic(punch_out_client):
    _, fake_db, prefix, punch_in = punch_out_client
    payload = {
        "emp_code": prefix,
        "punched_at": epoch_ms(
            datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)
        ),
    }

    def send_request():
        request_client = TestClient(main.app)
        try:
            return request_client.post("/attendance/punch-out", json=payload).status_code
        finally:
            request_client.close()

    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(lambda _: send_request(), range(8)))
    assert statuses.count(200) == 1
    assert statuses.count(409) == 7
