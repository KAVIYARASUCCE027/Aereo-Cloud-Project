# Bulk Certificate Generator — Design Decisions

This file records the agreed decisions. Implementation must follow it; change
this file first if a decision changes.

## Stack

| Component | Choice |
|---|---|
| Language | Python 3.12 |
| API | FastAPI (sync route handlers, run in FastAPI's threadpool) |
| Database | PostgreSQL 17 |
| DB access | SQLAlchemy 2.0 (sync) with the psycopg 3 driver |
| Migrations | Alembic |
| Background processing | Celery, one task per job |
| Broker | Redis |
| PDF | ReportLab |
| Tests | pytest, HTTPX (FastAPI `TestClient`), pypdf |
| Local setup | Docker Compose |

Sync SQLAlchemy is used everywhere so the API and the worker share the same
database code. Each request and each worker execution owns its own session.

## Workflow

1. Client submits shared certificate information plus a list of recipients.
2. API validates shared information (reject whole request with `422` if invalid).
3. API validates each recipient independently, preserving its `input_index`.
4. API saves the job and every recipient outcome in one transaction.
5. After commit, API enqueues the job ID (only the ID) to Celery.
6. API returns `202 Accepted` with the job ID.
7. Worker generates PDFs for valid recipients, one at a time.
8. Worker records `SUCCESS` or `FAILED` per recipient.
9. Client polls progress, reads per-recipient results, downloads PDFs.

**PostgreSQL is the source of truth for progress.** Clients never depend on
Celery task state, so no Celery result backend is configured.

## Validation rules

| Input | Rule |
|---|---|
| Event name | Required, trimmed, 1–200 characters |
| Issuer name | Required, trimmed, 1–200 characters |
| Issue date | Required, valid ISO date |
| Recipients | Required list, 1–1,000 entries (application limit, configurable) |
| Recipient name | Required string, trimmed, 1–150 characters |
| Recipient email | Optional; format validated when provided |
| Duplicate names or emails | Allowed; each entry processed independently |
| Invalid shared information | `422`, no job created |
| Some invalid recipients | Job accepted; those entries stored as `INVALID` |
| All recipients invalid | `422` with indexed errors; no job created |

Partial acceptance: the outer list is validated first (type and length), then
`RecipientInput.model_validate()` is called separately for each entry and its
errors are captured.

## Database

`generation_jobs`: `id` (UUID), `event_name`, `issuer_name`, `issue_date`,
`status`, `total_count`, `error_code`, `error_message`, timestamps.

`job_recipients`: `id` (UUID, also the certificate ID), `job_id` (FK),
`input_index`, nullable validated `name` and `email`, `status`,
validation/error details, nullable `storage_key`, timestamps.

- Unique constraint on `(job_id, input_index)`.
- Index on `(job_id, status)`.
- Result counts are computed from recipient rows, not stored counters.

### Recipient states

| State | Meaning |
|---|---|
| `PENDING` | Validated and waiting |
| `PROCESSING` | Currently generating |
| `SUCCESS` | PDF saved and available |
| `INVALID` | Recipient validation failed |
| `FAILED` | Processing failed |

### Job states

| State | Meaning |
|---|---|
| `QUEUED` | Waiting for the worker |
| `PROCESSING` | Worker is processing recipients |
| `COMPLETED` | Every recipient succeeded |
| `COMPLETED_WITH_ERRORS` | All entries finished, with both successes and errors |
| `FAILED` | All entries finished with zero successes |

### Progress

```text
processed        = success_count + failed_count + invalid_count
progress_percent = processed / total_count × 100
```

100% means every entry has a final outcome, not that every certificate
succeeded. Infrastructure failures that end a job record an explicit job error
and give unfinished entries a terminal failure outcome.

## API contract

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/v1/jobs` | Submit a batch |
| `GET` | `/api/v1/jobs/{job_id}` | Status and aggregate progress |
| `GET` | `/api/v1/jobs/{job_id}/recipients` | Paginated recipient results (`limit`, `offset`) |
| `GET` | `/api/v1/certificates/{certificate_id}/download` | Download a successful PDF |
| `GET` | `/health` | API running and database reachable |

Download responses: `200` PDF; `404` unknown certificate ID; `409` not ready
or generation did not succeed; documented `5xx` if a successful file is
unexpectedly missing.

## Storage

PDFs are written to a directory shared by the API and worker containers
(`CERTIFICATE_STORAGE_DIR`, a named Docker volume). The database stores only
metadata and a `storage_key`; file paths are derived from generated IDs, never
from user input. Files are written to a temporary file and atomically renamed.

## Implementation phases

1. Project foundation: config, DB connection, Alembic, Docker, `/health`.
2. Models, migration, recipient validation, `POST /api/v1/jobs`.
3. Standalone ReportLab certificate renderer.
4. `process_job(job_id)` with per-recipient failure isolation.
5. Celery and Redis integration with atomic job claim.
6. Job status, recipient results, downloads.
7. Complete automated tests, including real Redis + Celery integration.
8. README, examples, final verification.
