# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB (local docker: docker run -p 27017:27017 mongo:7)
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```
MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

## Implemented API and verification

The application includes employee and attendance endpoints, attendance regularization with audit history, all four
analytics endpoints, and `GET /admin/explain/{endpoint}` for the five contract-defined endpoint identifiers.
The explain route accepts the corresponding filters and returns MongoDB `executionStats` without modifying data.

Run the regression suite with:

```bash
python -m pytest -v
```

The latest local run completed with 43 passed, 0 failed, and 0 unexecuted tests. Three existing deprecation
warnings remain. A live Atlas check succeeded for all five explain routes and confirmed the configured employee and
attendance indexes; representative indexed queries examined keys without a collection-scan stage. This folder-backed
workspace is not a Git repository, so Git status/diff verification is unavailable.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.
