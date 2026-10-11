import pytest
from fastapi.testclient import TestClient
from pymongo.errors import PyMongoError

import app.main as main


class ExplainCursor:
    def __init__(self, collection, query):
        self.collection = collection
        self.query = query
        self.operations = []

    def sort(self, value):
        self.operations.append(("sort", value))
        return self

    def skip(self, value):
        self.operations.append(("skip", value))
        return self

    def limit(self, value):
        self.operations.append(("limit", value))
        return self

    def explain(self, verbosity=None):
        self.collection.last_find = {
            "query": self.query,
            "operations": self.operations,
            "verbosity": verbosity,
        }
        return {"executionStats": {"nReturned": 0, "totalDocsExamined": 0}}


class ExplainCollection:
    def __init__(self, name):
        self.name = name
        self.last_find = None

    def find(self, query):
        return ExplainCursor(self, query)

    def aggregate(self, pipeline):
        self.last_pipeline = pipeline
        return []

    def find_one(self, query):
        if query.get("emp_code") == "EMP1000":
            return {
                "emp_code": "EMP1000",
                "joined_on": "2026-01-01",
                "department": "Engineering",
            }
        if query.get("department") == "Engineering":
            return {"department": "Engineering"}
        return None


class ExplainDb:
    def __init__(self):
        self.employees = ExplainCollection("employees")
        self.attendance_logs = ExplainCollection("attendance_logs")
        self.commands = []
        self.fail_explain = False

    def command(self, name, command, verbosity=None):
        if self.fail_explain:
            raise PyMongoError("explain unavailable")
        self.commands.append((name, command, verbosity))
        return {"executionStats": {"nReturned": 0, "totalDocsExamined": 0}}


@pytest.fixture
def explain_client(monkeypatch):
    fake_db = ExplainDb()
    monkeypatch.setattr(main, "db", fake_db)
    client = TestClient(main.app)
    yield client, fake_db
    client.close()


@pytest.mark.parametrize(
    ("endpoint", "params", "collection"),
    [
        ("attendance_list", {"date_from": "2026-07-01", "page": 2, "page_size": 5}, "attendance_logs"),
        ("employee_monthly", {"emp_code": "EMP1000", "month": "2026-07"}, "attendance_logs"),
        ("department_summary", {"month": "2026-07"}, "employees"),
        ("late_leaderboard", {"month": "2026-07"}, "attendance_logs"),
        (
            "department_trend",
            {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-03"},
            "attendance_logs",
        ),
    ],
)
def test_supported_explain_endpoints(explain_client, endpoint, params, collection):
    client, fake_db = explain_client
    response = client.get(f"/admin/explain/{endpoint}", params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["endpoint"] == endpoint
    assert body["collection"] == collection
    assert body["explain"]["executionStats"]["totalDocsExamined"] == 0
    assert fake_db.commands or fake_db.attendance_logs.last_find is not None


def test_attendance_explain_preserves_filter_sort_and_pagination(explain_client):
    client, fake_db = explain_client
    response = client.get(
        "/admin/explain/attendance_list",
        params={
            "emp_code": "EMP1000",
            "date_from": "2026-07-01",
            "date_to": "2026-07-31",
            "status": "PRESENT",
            "page": 3,
            "page_size": 7,
        },
    )
    assert response.status_code == 200
    assert fake_db.attendance_logs.last_find == {
        "query": {
            "emp_code": "EMP1000",
            "date": {"$gte": "2026-07-01", "$lte": "2026-07-31"},
            "status": "PRESENT",
        },
        "operations": [
            ("sort", [("date", main.DESCENDING), ("emp_code", main.ASCENDING)]),
            ("skip", 14),
            ("limit", 7),
        ],
        "verbosity": None,
    }


def test_explain_validation_and_no_mutation(explain_client):
    client, fake_db = explain_client
    assert client.get("/admin/explain/not_supported").status_code == 422
    assert client.get("/admin/explain/employee_monthly", params={"month": "2026-07"}).status_code == 422
    assert client.get("/admin/explain/department_trend", params={"department": "Engineering", "from": "2026-07-03"}).status_code == 422
    assert client.get(
        "/admin/explain/department_trend",
        params={"department": "Engineering", "from": "2026-07-03", "to": "2026-04-01"},
    ).status_code == 422
    assert fake_db.commands == []


def test_explain_database_error_is_explicit(explain_client):
    client, fake_db = explain_client
    fake_db.fail_explain = True
    response = client.get(
        "/admin/explain/department_summary",
        params={"month": "2026-07"},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "MongoDB explain failed"
