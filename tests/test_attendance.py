from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as main


class FakeCursor:
    def __init__(self, documents):
        self.documents = documents

    def sort(self, fields, direction=None):
        if isinstance(fields, str):
            fields = [(fields, direction)]
        for field, sort_direction in reversed(fields):
            self.documents.sort(
                key=lambda document: document.get(field),
                reverse=sort_direction == main.DESCENDING,
            )
        return self

    def skip(self, amount):
        self.documents = self.documents[amount:]
        return self

    def limit(self, amount):
        self.documents = self.documents[:amount]
        return self

    def __iter__(self):
        return iter(self.documents)


class FakeCollection:
    def __init__(self, documents):
        self.documents = documents

    def _matches(self, document, query):
        for field, expected in query.items():
            actual = document.get(field)
            if isinstance(expected, dict):
                if "$gte" in expected and actual < expected["$gte"]:
                    return False
                if "$lte" in expected and actual > expected["$lte"]:
                    return False
            elif actual != expected:
                return False
        return True

    def count_documents(self, query):
        return sum(self._matches(document, query) for document in self.documents)

    def find(self, query, projection):
        documents = [
            {key: value for key, value in document.items() if key != "_id"}
            for document in self.documents
            if self._matches(document, query)
        ]
        return FakeCursor(documents)

    def delete_many(self, query):
        self.documents[:] = [
            document
            for document in self.documents
            if not self._matches(document, query)
        ]


class FakeDb:
    def __init__(self, documents):
        self.attendance_logs = FakeCollection(documents)


@pytest.fixture
def attendance_client(monkeypatch):
    prefix = f"TEST-{uuid4().hex}"
    records = [
        {
            "_id": object(),
            "emp_code": f"{prefix}-B",
            "date": "2026-07-08",
            "status": "WFH",
            "punch_in": datetime(2026, 7, 8, 4, 0),
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
        },
        {
            "_id": object(),
            "emp_code": f"{prefix}-A",
            "date": "2026-07-08",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 8, 3, 30, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 8, 12, 30, tzinfo=timezone.utc),
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        {
            "_id": object(),
            "emp_code": f"{prefix}-A",
            "date": "2026-07-07",
            "status": "ABSENT",
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
        },
    ]
    fake_db = FakeDb(records)
    monkeypatch.setattr(main, "db", fake_db)
    client = TestClient(main.app)
    yield client, prefix
    client.close()
    fake_db.attendance_logs.delete_many(
        {"emp_code": {"$gte": prefix, "$lte": f"{prefix}\uffff"}}
    )


def test_attendance_filters_inclusive_totals_and_sorting(attendance_client):
    client, prefix = attendance_client
    response = client.get(
        "/attendance",
        params={
            "emp_code": f"{prefix}-A",
            "date_from": "2026-07-07",
            "date_to": "2026-07-08",
            "status": "PRESENT",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [item["date"] for item in body["items"]] == ["2026-07-08"]


def test_attendance_pagination_is_deterministic(attendance_client):
    client, prefix = attendance_client
    response = client.get(
        "/attendance",
        params={"date_from": "2026-07-08", "date_to": "2026-07-08", "page_size": 1},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["items"][0]["emp_code"] == f"{prefix}-A"


def test_attendance_rejects_invalid_dates_and_pagination(attendance_client):
    client, _ = attendance_client
    assert client.get("/attendance", params={"date_from": "2026-02-30"}).status_code == 422
    assert client.get("/attendance", params={"date_from": "2026-2-3"}).status_code == 422
    assert client.get(
        "/attendance",
        params={"date_from": "2026-07-09", "date_to": "2026-07-08"},
    ).status_code == 422
    assert client.get("/attendance", params={"page": 0}).status_code == 422
    assert client.get("/attendance", params={"page_size": 101}).status_code == 422


def test_attendance_serializes_instants_and_legacy_defaults(attendance_client):
    client, prefix = attendance_client
    response = client.get("/attendance", params={"emp_code": f"{prefix}-B"})
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["punch_in"] == int(
        datetime(2026, 7, 8, 4, 0, tzinfo=timezone.utc).timestamp() * 1000
    )
    assert item["punch_out"] is None
    assert item["half_day"] is False
    assert item["history"] == []
    assert "_id" not in item
    assert "id" not in item
