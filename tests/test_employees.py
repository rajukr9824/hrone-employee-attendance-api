import re
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import app, db

client = TestClient(app)


def valid_employee(**overrides):
    data = {
        "emp_code": f"EMP{uuid.uuid4().int % 1_000_000:06d}",
        "name": "Test Employee",
        "email": "test.employee@example.com",
        "department": "QA_TEST",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-15",
    }
    data.update(overrides)
    return data


def cleanup_employee(emp_code):
    db.employees.delete_one({"emp_code": emp_code})


def test_create_employee_returns_contract_response():
    data = valid_employee()
    try:
        response = client.post("/employees", json=data)

        assert response.status_code == 201, response.text
        body = response.json()

        assert body["emp_code"] == data["emp_code"]
        assert body["name"] == data["name"]
        assert isinstance(body["created_at"], int)
        assert "_id" not in body
        assert re.fullmatch(r"EMP\d{4,6}", body["emp_code"])
    finally:
        cleanup_employee(data["emp_code"])


def test_duplicate_employee_returns_409():
    data = valid_employee()
    try:
        first = client.post("/employees", json=data)
        assert first.status_code == 201, first.text

        second = client.post("/employees", json=data)
        assert second.status_code == 409, second.text
    finally:
        cleanup_employee(data["emp_code"])


@pytest.mark.parametrize(
    "override",
    [
        {"emp_code": "EMP12"},
        {"email": "not-an-email"},
        {"joined_on": "2026-02-30"},
        {"shift_start": "25:00"},
        {"shift_start": "09:30", "shift_end": "09:30"},
        {"name": ""},
        {"department": ""},
    ],
)
def test_invalid_employee_input_returns_422(override):
    response = client.post(
        "/employees",
        json=valid_employee(**override),
    )
    assert response.status_code == 422, response.text


def test_concurrent_duplicate_requests_create_one_employee():
    data = valid_employee()

    def create():
        return client.post("/employees", json=data).status_code

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            statuses = list(executor.map(lambda _: create(), range(8)))

        assert statuses.count(201) == 1, statuses
        assert statuses.count(409) == 7, statuses
    finally:
        cleanup_employee(data["emp_code"])
