# HROne Employee Attendance & Analytics API

A FastAPI and MongoDB application for employee management, attendance tracking, attendance regularization, and workforce analytics.

## Features

- Employee and attendance management
- Punch-in and punch-out functionality
- Attendance regularization with audit history
- Four analytics endpoints
- MongoDB indexes and aggregation pipelines
- Admin explain endpoint for inspecting query execution statistics
- Automated regression tests

## Tech Stack

- Python
- FastAPI
- MongoDB
- Pytest

## Setup

Create and activate a virtual environment, then install dependencies:

```bash
python -m venv .venv
```

On Windows:

```powershell
.\.venv\Scripts\Activate.ps1
```

On Linux/macOS:

```bash
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Configure `MONGO_URI` and `MONGO_DB` using your local environment variables. Do not commit `.env` files or credentials.

Start the API:

```bash
uvicorn app.main:app --port 8000
```

## Run Tests

```bash
python -m pytest -v
```

The latest reported local test run completed with **43 passed, 0 failed, and 0 unexecuted tests**. Three deprecation warnings remained.

## Project Structure

- `app/main.py` — API implementation
- `tests/` — automated regression tests
- `requirements.txt` — Python dependencies
- `openapi.yaml` — API contract
- `DATA_MODEL.md` — database schema and sample documents
- `REVIEW.md` — code review
- `DECISIONS.md` — design decisions

## Submission

This repository contains the implementation and supporting documentation for the employee attendance and analytics API assignment. It excludes `.env` files, secrets, and the Dockerfile.
