from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from threading import Barrier, Lock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as main


def epoch_ms(value):
    return int(value.timestamp() * 1000)


class FakeEmployees:
    def __init__(self, employee):
        self.employee = employee

    def find_one(self, query):
        if query.get("emp_code") == self.employee["emp_code"]:
            return self.employee
        return None


class FakeAttendance:
    def __init__(self, document):
        self.document = document
        self.lock = Lock()
        self.barrier = None

    def find_one(self, query, projection=None, sort=None):
        if (
            query.get("emp_code") == self.document["emp_code"]
            and query.get("date") == self.document["date"]
        ):
            result = deepcopy(self.document)
            result["_id"] = self.document["_id"]
            if self.barrier is not None:
                self.barrier.wait()
            return result
        return None

    def find_one_and_update(
        self,
        query,
        update,
        projection=None,
        return_document=None,
    ):
        with self.lock:
            history_matches = (
                self.document.get("history") == query["$or"][0]["history"]
                or "history" not in self.document
            )
            if self.document["_id"] != query["_id"] or not history_matches:
                return None
            self.document.update(update["$set"])
            self.document.setdefault("history", []).append(
                update["$push"]["history"]
            )
            result = deepcopy(
                {
                    key: value
                    for key, value in self.document.items()
                    if key != "_id"
                }
            )
            return result


class FakeDb:
    def __init__(self, employee, document):
        self.employees = FakeEmployees(employee)
        self.attendance_logs = FakeAttendance(document)


@pytest.fixture
def regularize_client(monkeypatch):
    prefix = f"EMP{uuid4().hex[:6].upper()}"
    employee = {
        "emp_code": prefix,
        "name": "Correction Worker",
        "shift_start": "09:30",
        "shift_end": "18:30",
    }
    original_in = datetime(2026, 7, 6, 4, 35, tzinfo=timezone.utc)
    original_out = datetime(2026, 7, 6, 13, 40, tzinfo=timezone.utc)
    document = {
        "_id": object(),
        "emp_code": prefix,
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": original_in,
        "punch_out": original_out,
        "work_hours": 9.08,
        "late_minutes": 35,
        "overtime_minutes": 40,
        "half_day": False,
        "history": [
            {
                "at": datetime(2026, 7, 7, 5, 0, tzinfo=timezone.utc),
                "by": "previous.admin",
                "reason": "existing correction",
                "changes": {},
            }
        ],
    }
    fake_db = FakeDb(employee, document)
    monkeypatch.setattr(main, "db", fake_db)
    client = TestClient(main.app)
    yield client, fake_db, prefix, original_in, original_out
    client.close()


def test_successful_correction_recomputes_and_preserves_history(regularize_client):
    client, fake_db, prefix, original_in, original_out = regularize_client
    corrected_in = datetime(2026, 7, 6, 3, 58, tzinfo=timezone.utc)
    response = client.patch(
        f"/attendance/{prefix}/2026-07-06",
        json={
            "punch_in": epoch_ms(corrected_in),
            "reason": "biometric glitch",
            "regularized_by": "hr.admin",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["punch_in"] == epoch_ms(corrected_in)
    assert body["punch_out"] == epoch_ms(original_out)
    assert body["work_hours"] == 9.7
    assert body["late_minutes"] == 0
    assert body["overtime_minutes"] == 40
    assert body["half_day"] is False
    assert len(body["history"]) == 2
    assert body["history"][0]["by"] == "previous.admin"
    change = body["history"][1]["changes"]["punch_in"]
    assert change == {
        "from": epoch_ms(original_in),
        "to": epoch_ms(corrected_in),
    }
    assert fake_db.attendance_logs.document["history"][0]["by"] == "previous.admin"


def test_missing_and_invalid_corrections(regularize_client):
    client, _, prefix, original_in, _ = regularize_client
    assert client.patch(
        "/attendance/EMP9999/2026-07-06",
        json={"reason": "valid reason", "regularized_by": "admin"},
    ).status_code == 404
    assert client.patch(
        f"/attendance/{prefix}/2026-07-07",
        json={"reason": "valid reason", "regularized_by": "admin"},
    ).status_code == 404
    assert client.patch(
        f"/attendance/{prefix}/2026-07-06",
        json={
            "status": "ABSENT",
            "punch_in": epoch_ms(original_in),
            "reason": "valid reason",
            "regularized_by": "admin",
        },
    ).status_code == 422
    assert client.patch(
        f"/attendance/{prefix}/2026-07-06",
        json={
            "punch_out": epoch_ms(original_in),
            "reason": "valid reason",
            "regularized_by": "admin",
        },
    ).status_code == 422


def test_noop_and_presence_validation(regularize_client):
    client, _, prefix, _, _ = regularize_client
    assert client.patch(
        f"/attendance/{prefix}/2026-07-06",
        json={"reason": "valid reason", "regularized_by": "admin"},
    ).status_code == 422
    assert client.patch(
        f"/attendance/{prefix}/2026-07-06",
        json={
            "status": "ABSENT",
            "reason": "valid reason",
            "regularized_by": "admin",
        },
    ).status_code == 200


def test_concurrent_corrections_do_not_lose_history(regularize_client):
    _, fake_db, prefix, _, _ = regularize_client
    fake_db.attendance_logs.barrier = Barrier(2)
    payloads = [
        {
            "status": "WFH",
            "reason": "parallel correction one",
            "regularized_by": "admin.one",
        },
        {
            "status": "ON_DUTY",
            "reason": "parallel correction two",
            "regularized_by": "admin.two",
        },
    ]

    def send_request(payload):
        request_client = TestClient(main.app)
        try:
            return request_client.patch(
                f"/attendance/{prefix}/2026-07-06",
                json=payload,
            ).status_code
        finally:
            request_client.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(send_request, payloads))
    assert statuses.count(200) == 1
    assert statuses.count(409) == 1
    assert len(fake_db.attendance_logs.document["history"]) == 2
