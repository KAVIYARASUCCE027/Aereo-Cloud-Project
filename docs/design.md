# Bulk Certificate Generator — Design Decisions

This file records the agreed decisions. Implementation must follow it; change
this file first if a decision changes.

**Current state: all eight phases complete.** Jobs are submitted over HTTP, stored,
and published to Redis; a separate Celery worker runs `process_job(job_id)`;
clients poll status, page through recipient results, and download PDFs (see
[Current limitations](#current-limitations)).

## Stack

| Component | Choice |
|---|---|
| Language | Python 3.12 |
| API | FastAPI (sync route handlers, run in FastAPI's threadpool) |
| Database | PostgreSQL 17 |
| DB access | SQLAlchemy 2.x (sync) with the psycopg 3 driver |
| Migrations | Alembic (never `create_all()` at startup) |
| Background processing | Celery 5.6, one task per job, prefork worker |
| Broker | Redis 7 with append-only persistence |
| PDF | ReportLab, with bundled DejaVu fonts |
| Email syntax | email-validator (syntax only, no DNS lookups) |
| Tests | pytest, HTTPX (FastAPI `TestClient`), pypdf |
| Local setup | Docker Compose |

Sync SQLAlchemy is used everywhere so the API and the worker share the same
database code. Each request and each processing run owns its own sessions.

## Workflow

1. Client submits shared certificate information plus a list of recipients.
2. API validates shared information (reject whole request with `422` if invalid).
3. API validates each recipient independently, preserving its `input_index`.
4. API saves the job and every recipient outcome in one transaction.
5. After commit, API publishes one Celery task carrying only the job ID.
6. API returns `202 Accepted` with the job ID (or `503` if publishing failed).
   The API never renders PDFs.
7. A worker runs `process_job(job_id)`: PDFs for valid recipients, one at a time.
8. It records `SUCCESS` or `FAILED` per recipient and finalizes the job.
9. Client polls progress, reads per-recipient results, downloads PDFs.

All steps are implemented. `python -m app.cli process-job JOB_ID` can also
run step 7 by hand, without Celery.

**PostgreSQL is the source of truth for progress.** Clients never depend on
Celery task state, so no Celery result backend is configured.

## Validation rules

| Input | Rule |
|---|---|
| Event name | Required string; NFC-normalized and trimmed; 1–200 characters; supported characters only |
| Issuer name | Required string; NFC-normalized and trimmed; 1–200 characters; supported characters only |
| Issue date | Required, ISO `YYYY-MM-DD` string that is a real calendar date |
| Recipients | Required list, 1–1,000 entries (`MAX_RECIPIENTS_PER_JOB` in `app/schemas.py`) |
| Recipient name | Required string; NFC-normalized and trimmed; 1–150 characters; supported characters only |
| Recipient email | Optional or `null`; when a string is given, its syntax is validated (not subject to the character rules, because it is never printed) |
| Duplicate names or emails | Allowed; each entry processed independently |
| Invalid shared information | `422`, no records created |
| Some invalid recipients | Job accepted; those entries stored as `INVALID` |
| All recipients invalid | `422` with indexed errors; no records created |

How it is implemented:

- **Two levels.** `JobCreateRequest` validates the shared fields and only the
  outer recipients list (type and length). Its items are declared
  `SkipValidation[RecipientInput]`, so entries stay as submitted and Swagger
  still shows the recipient schema.
- **Per entry.** `app/services/validation.py` calls
  `RecipientInput.model_validate(entry)` for each entry separately and
  catches its `ValidationError`. Malformed entries (`null`, strings, numbers,
  lists, missing fields, wrong types) become `INVALID` without affecting
  their siblings. `input_index` is the zero-based position in the submitted list.
- **Printed text** (name, event name, issuer name) uses the shared text
  policy (next section), in this order: NFC-normalize and trim, then check the
  length, then check every character. Normalizing first means `e` followed by
  a combining accent counts as one character, and whitespace-only values fail
  as too short. The stored value is the normalized one. Numbers are not
  coerced to strings.
- **Issue date** must be a `YYYY-MM-DD` string; Pydantic alone would also accept
  Unix timestamps and datetimes. `2026-02-30` is rejected.
- **Email** is checked with `email-validator` (`check_deliverability=False`, so
  no network access) and stored in normalized form (domain lowercased). An
  empty or whitespace-only email string is invalid; omit it or send `null`.
- **Unknown extra fields** are ignored, at both levels.
- The 1,000 limit is an application choice, not part of the assignment.

### Certificate text policy (supported characters)

`app/text_policy.py` is the single definition used by both submission
validation and the renderer, so they cannot disagree.

**Application limitation:** certificates can only display the following
left-to-right scripts, because ReportLab does not perform glyph shaping or
right-to-left layout, and the bundled fonts cover only these:

| Range | Examples |
|---|---|
| Basic Latin (printable, U+0020–007E) | `A-Z a-z 0-9 & - ' .` |
| Latin-1 Supplement (U+00A0–00FF) | `é ü ñ ç å ø ß « »` |
| Latin Extended-A and -B (U+0100–024F) | `ł ż ő č ş ș ț` |
| Latin Extended Additional (U+1E00–1EFF) | Vietnamese `ễ ị ừ` |
| Greek and Coptic (U+0370–03FF) | `Αλέξανδρος` |
| Cyrillic (U+0400–04FF) | `Дмитрий` |
| Punctuation (U+2010–2027) | `– — ‘ ’ “ ” • …` |

Within these ranges a character is supported only if both serif fonts used
for user text can draw it and it is not a control, format (e.g. soft hyphen),
unassigned, or combining character, and not one that NFC would replace. That
excludes about 70 rare characters the fonts lack (historic Cyrillic, Coptic
letters, Middle-Welsh V). Unsupported examples: Devanagari, Arabic, Hebrew,
Thai, CJK, emoji, newline, tab, zero-width space, a combining mark with no
precomposed form.

Error code `unsupported_characters`; the message lists up to five code points
with their Unicode names, e.g. `Contains characters certificates cannot
display: U+0906 DEVANAGARI LETTER AA, ...`.

- Unsupported recipient name: that entry is `INVALID` (field `name`).
- Unsupported event or issuer name: `422`, no job created.

Tests check that every supported character exists in every text font and that
every supported character, submitted as names, validates and renders.

### Error formats

Each recipient error is a plain JSON object:

```json
{ "field": "email", "code": "email_invalid", "message": "Invalid email address: ..." }
```

`field` is `null` when the whole entry is wrong (for example `null` or not an
object; code `model_type`) or when the failure is not tied to a field.
`code` is Pydantic's error type or one of ours. The submitted value,
exception objects, stack traces, and internal paths are never stored.

| Code | Where |
|---|---|
| `missing`, `string_type`, `string_too_short`, `string_too_long`, `model_type` (Pydantic) | validation |
| `email_invalid`, `date_format`, `unsupported_characters` | validation |
| `unsupported_characters`, `text_does_not_fit`, `render_failed` | rendering (recipient `FAILED`) |
| `storage_error` | saving the PDF failed (message names only the error kind, e.g. `ENOSPC`) |
| `generation_error` | unexpected error while rendering (details only in logs) |
| `dispatch_failed` | the job could not be published to the queue (job-level `error_code`, and each recipient left unprocessed) |

| Case | `422` body |
|---|---|
| Shared fields or recipients list invalid | FastAPI's standard `{"detail": [{"type", "loc", "msg", "input"}]}` |
| Every recipient invalid | `{"detail": {"code": "all_recipients_invalid", "message", "recipient_errors": [{"input_index", "errors": [...]}]}}` |

| Case | `503` body |
|---|---|
| Job saved but could not be queued | `{"detail": {"code": "dispatch_failed", "message", "job_id", "job_status": "FAILED" or null, "status_url"}}` |

## Database

Migrations (`migrations/versions/`): `f8fdd795353d` creates the tables;
`da47b265b133` adds recipient processing timestamps and outcome rules;
`56bdc5b0374c` lets a recipient be `FAILED` without having started (needed
when its job could not be queued).

`generation_jobs`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | generated by the application |
| `event_name`, `issuer_name` | varchar(200) | validated, normalized |
| `issue_date` | date | |
| `status` | varchar(32) | CHECK: one of the job states |
| `total_count` | integer | all submitted entries, valid or not; CHECK `>= 1` |
| `error_code`, `error_message` | nullable | job-level failure only (`dispatch_failed`) |
| `created_at`, `updated_at` | timestamptz, not null | server default `now()` |
| `started_at` | timestamptz, nullable | set when a processing run claims the job |
| `completed_at` | timestamptz, nullable | set exactly when the job becomes terminal |

`job_recipients`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | also the certificate ID |
| `job_id` | UUID FK → `generation_jobs.id` | `ON DELETE CASCADE` |
| `input_index` | integer | zero-based; CHECK `>= 0` |
| `name` | varchar(150), nullable | validated value; `NULL` for `INVALID` rows |
| `email` | varchar(320), nullable | validated, normalized; `NULL` for `INVALID` rows |
| `status` | varchar(32) | CHECK: one of the recipient states |
| `error_details` | JSONB, nullable | list of error objects; Python `None` is stored as SQL `NULL` |
| `storage_key` | varchar(255), nullable | `{id}.pdf`, set together with `SUCCESS` |
| `created_at`, `updated_at` | timestamptz, not null | server default `now()` |
| `started_at`, `completed_at` | timestamptz, nullable | processing times; both `NULL` for `INVALID` rows; `started_at` is `NULL` for a `FAILED` row that never started |

Constraints and indexes:

- `uq_job_recipients_job_id_input_index` on `(job_id, input_index)`.
- `ix_job_recipients_job_id_status` on `(job_id, status)`.
- `ck_*_status_valid` on both tables: status values are enforced by the database.
- `ck_job_recipients_name_matches_status`: `INVALID` rows store no name or
  email; every other row has a name.
- `ck_job_recipients_invalid_has_error_details`, `..._failed_has_error_details`:
  `INVALID` and `FAILED` rows always explain why.
- `ck_job_recipients_success_has_storage_key`: `SUCCESS` always names its file.
- `ck_job_recipients_processing_or_success_has_started_at`,
  `..._finished_has_completed_at`: processing timestamps match the status
  (`SUCCESS` and `FAILED` always have `completed_at`).
- `ck_generation_jobs_completed_at_matches_status`: `completed_at` is set if
  and only if the job is terminal; `..._processing_has_started_at`.

Status columns are VARCHAR + CHECK rather than native PostgreSQL ENUM types,
so adding a state later is a constraint change, not an `ALTER TYPE`.
Migrations spell out the values instead of importing `app.models`, so they
keep describing the schema as it was written.

Result counts are computed from recipient rows, not stored counters.
`updated_at` is maintained by SQLAlchemy (`onupdate`), so raw SQL updates must
set it themselves.

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
| `QUEUED` | Waiting to be processed |
| `PROCESSING` | Claimed by a processing run |
| `COMPLETED` | Every recipient succeeded |
| `COMPLETED_WITH_ERRORS` | All entries finished, with at least one success and at least one `INVALID`/`FAILED` |
| `FAILED` | All entries finished with zero successes |

### Progress

`app/services/progress.py` (`JobProgress`, `get_job_progress`):

```text
processed        = success_count + invalid_count + failed_count
progress_percent = processed / total_count × 100
```

Counts come from a `GROUP BY status` over recipient rows. 100% means every
entry has a final outcome, not that every certificate succeeded. A job is
finalized only when no entry is `PENDING` or `PROCESSING` and the counts add
up to `total_count`; otherwise finalization is refused.

## Job creation and transaction boundary

`POST /api/v1/jobs` → `app/api/jobs.py` (thin route) →
`app/services/jobs.submit_job`:

1. `create_job` validates recipients. If none is valid it raises
   `AllRecipientsInvalidError` before touching the database (route → `422`).
2. It builds the job plus one row per submitted entry (`PENDING` or
   `INVALID`) and commits them in **one transaction**. On any failure the
   session is rolled back and the error re-raised (client gets `500`), leaving
   no job and no recipient rows.
3. Only after the commit, `submit_job` calls `dispatch.enqueue_job(job_id)`,
   so a worker never receives the ID of a job it cannot read. Publish
   failures are handled as described under Dispatch.

## Processing (`process_job`)

`app/services/generation.py`: `process_job(job_id, storage_root=None)`. It
takes no request or task object, opens its own sessions, and is called by
the Celery task, by tests, and by `python -m app.cli process-job`.

| Step | Transaction | What happens |
|---|---|---|
| Claim | own, committed | `UPDATE generation_jobs SET status='PROCESSING', started_at=now() WHERE id=:id AND status='QUEUED' RETURNING ...` |
| List work | own, read only | `PENDING` recipient IDs in `input_index` order |
| Start recipient | own, committed | `PENDING → PROCESSING`, `started_at`; reads the name (skips rows no longer `PENDING`) |
| Render + save | **none** | `render_certificate` then `write_file_atomically` |
| Finish recipient | own, committed | `SUCCESS` + `storage_key` + `completed_at`, or `FAILED` + `error_details` + `completed_at` |
| Finalize | own, committed | status from recipient counts; `completed_at` |

- **Claim.** Only one caller can claim a job: PostgreSQL locks the row for the
  first `UPDATE`; a concurrent caller waits, re-evaluates `status = 'QUEUED'`
  against the committed row, and updates nothing. Tested deterministically
  (second caller observed waiting on the lock, then losing) and with eight
  simultaneous callers.
- **Outcomes.** `processed` (this call ran the job), `already_processing`
  (another run holds the claim; nothing started), `already_finished` (terminal
  job; nothing regenerated). An unknown ID raises `JobNotFoundError`.
- **No open transaction while rendering.** Each step uses
  `with SessionLocal.begin()` and ends before rendering. Only plain values
  (UUIDs, strings, dates) leave a transaction, never ORM objects, so no lazy
  load can start a transaction during rendering or file I/O.
- **Stable certificate ID.** The recipient's own UUID is the certificate ID and
  the file name (`{id}.pdf`), so any attempt for that recipient writes the same
  file instead of creating new IDs.
- **Failure isolation.** Render and storage exceptions are caught per
  recipient and mapped to a stable code (see Error formats); the recipient
  becomes `FAILED` and processing continues with the next one. Unexpected
  exceptions are logged with job ID, recipient ID, and stack trace; the stored
  message stays generic. Database writes happen outside that exception
  handling, so a database error is never recorded as a certificate failure.
- **Renderer checks are kept.** Validation already rejects unsupported text,
  but older rows (or other callers) are still checked at render time and
  become `FAILED` with `unsupported_characters`.
- **Database failure.** The failed transaction is rolled back, processing
  stops, the error is logged, and `JobProcessingError` is raised (the command
  exits with code 2). Earlier committed results are kept. The job stays
  `PROCESSING` and one recipient may stay `PROCESSING`; nothing currently
  recovers such jobs (see limitations).

### File and database consistency

Saving the PDF (a rename) and recording `SUCCESS` (a PostgreSQL commit) are
not one transaction. Order: the final PDF is saved first, then `SUCCESS` is
committed, so `SUCCESS` always refers to an existing file. If the database
write fails after the save, the file exists but no `SUCCESS` row references
it (tested). Therefore **file existence alone never means success**; only the
recipient row's status does. File names are stable, so a later retry would
overwrite the same file. Temporary files are removed after ordinary write
failures; there is no cleanup of unreferenced or abandoned temporary files.

### Running it manually

```bash
docker compose exec worker python -m app.cli process-job JOB_ID
```

Prints the outcome and counts as JSON. Exit codes: `0` processed or nothing
to do, `1` unknown job, `2` stopped by an infrastructure error.
`--storage-dir DIR` overrides `CERTIFICATE_STORAGE_DIR`. It runs in the worker
container so PDFs land in the shared certificate volume.

## Dispatch and the worker

`app/worker.py` defines the Celery app and one task, `certificates.process_job`,
whose only argument is the job ID as a string. The task is a thin wrapper,
`process_job(UUID(job_id))`, returning a small summary that Celery logs (there
is no result backend). `app/services/dispatch.py` publishes it.

**Delivery settings** (asserted by a test):

| Setting | Value | Why |
|---|---|---|
| `task_acks_late` | `True` | acknowledge after the task returns, so a worker that dies before claiming the job does not lose the message |
| `task_reject_on_worker_lost` | `True` | if a worker child process dies, requeue its message immediately |
| `worker_prefetch_multiplier` | `1` | a busy process does not reserve further jobs |
| `visibility_timeout` | 3600 s | Redis redelivers an unacknowledged message after this long |
| retries | none | failed tasks are logged and acknowledged, never retried automatically |

This is at-least-once delivery. It is safe only because `process_job` claims
a job atomically: a second delivery of the same message returns
`already_processing` or `already_finished` and changes nothing (tested).
Celery does not provide exactly-once execution, and none is assumed.

Each prefork child disposes the inherited SQLAlchemy pool on start
(`worker_process_init`), so it never shares a connection with its parent.
Concurrency is `WORKER_CONCURRENCY` (default 2): that many jobs run in
parallel, each processing its recipients sequentially. Redis runs with an
append-only file on a volume, so queued messages survive a Redis restart.

### Publish failures

`enqueue_job` retries the publish once (enough to replace a pooled connection
that went stale when Redis restarted), then raises `DispatchError`.
`submit_job` then calls `record_dispatch_failure` in the request's session:

- One conditional `UPDATE generation_jobs ... WHERE id = :id AND status = 'QUEUED'`
  sets `FAILED`, `error_code = 'dispatch_failed'`, `error_message`, and
  `completed_at`. The same transaction marks every `PENDING` recipient
  `FAILED` with `dispatch_failed` (`started_at` stays `NULL`); `INVALID`
  entries keep their own errors. Progress is then 100%: every entry has a
  final outcome. The client gets `503 dispatch_failed` with `job_status: FAILED`.
- **A worker's claim is never overwritten.** The publish may have reached
  Redis even though the client saw an error (for example a lost reply). The
  failure update and the worker's claim are both conditional on
  `status = 'QUEUED'` on the same row, so PostgreSQL's row lock lets exactly
  one win. If the worker won, nothing is changed and the client gets `202`
  with the job's current status. If the failure was recorded first, a late
  message finds the job `FAILED` and does nothing.
- If the failure itself cannot be saved (database error), the job stays
  `QUEUED` with no message; the client gets `503` with `job_status: null`,
  and the error is logged.

Measured in Docker Compose (Phase 5 demo): after a Redis restart, submissions
still returned `202` (the retry reconnects). With the `redis` container
stopped, `503` arrived after 16 s and then 8 s, because each lookup of the
stopped container's host name takes 4–8 s in Docker's DNS. A refused
connection fails in under a second. Once Redis was back, the next submission
got `202` in 0.03 s.

### Crash windows (not handled)

- **Commit, then crash before publish.** The job stays `QUEUED` with no
  message, and nothing re-publishes it. Future work: a transactional outbox
  (write the message in the job's transaction, publish from that table) or a
  sweeper that re-publishes old `QUEUED` jobs. The atomic claim makes
  re-publishing safe.
- **Publish succeeded, response lost.** The job is processed, but the client
  saw an error and may submit again, creating a second job (no idempotency key).
- **Redis loses data.** With `appendfsync everysec`, up to about one second
  of queued messages can be lost if Redis crashes; those jobs stay `QUEUED`.

### Worker interruption: observed behaviour

Measured with 1,000-recipient jobs (about 30 s of processing, roughly 30 ms
per certificate):

- **Graceful stop** (`docker compose stop worker`, SIGTERM): Celery's warm
  shutdown let the running job finish (the stop took 26 s; job `COMPLETED`,
  1,000 `SUCCESS`), then the worker exited. `stop_grace_period` is 2 minutes;
  a job still running after that is killed, as below.
- **Crash** (`docker compose kill -s SIGKILL worker` after about 250
  certificates): the job stayed `PROCESSING` with 258 `SUCCESS`, 1 `PROCESSING`,
  and 741 `PENDING`. Redis kept the message unacknowledged. After restarting
  the worker nothing changed, and a manual `process-job` returned
  `already_processing`. The message is redelivered after the visibility
  timeout (1 hour; not waited for in the demo) and then does nothing, for the
  same reason. **There is no recovery of interrupted jobs.** Certificates
  already produced remain valid and recorded.
- **Crash between receiving the message and the claim:** the unacknowledged
  message is redelivered (immediately if only a child process died, otherwise
  after the visibility timeout) and the job is processed then. Not
  demonstrated; it follows from `acks_late`.

Recovering interrupted jobs needs a rule defined first, for example: a job
`PROCESSING` without progress for N minutes is reclaimed, its `PROCESSING`
recipient is reset to `PENDING` (its file name is stable, so regenerating it
overwrites the same file), and processing resumes with the `PENDING` rows.
Automatic retries should only be added together with such a rule.

## API contract

| Method | Endpoint | Purpose | Responses |
|---|---|---|---|
| `POST` | `/api/v1/jobs` | Submit a batch | `202`, `422`, `503` |
| `GET` | `/api/v1/jobs/{job_id}` | Status and aggregate progress | `200`, `404`, `422` (malformed ID) |
| `GET` | `/api/v1/jobs/{job_id}/recipients` | Paginated recipient results (`limit` 1–200, default 50; `offset` ≥ 0) | `200`, `404`, `422` |
| `GET` | `/api/v1/certificates/{certificate_id}/download` | Download a successful PDF | `200`, `404`, `409`, `500` |
| `GET` | `/health` | API running and database reachable | `200`, `503` |

Routes are thin (`app/api/`); reads live in `app/services/progress.py`
(`load_job_summary`) and `app/services/results.py`. URL paths in responses
come from `app/api/urls.py`. Error bodies are `{"detail": {"code", "message", ...}}`
except FastAPI's own `422` validation errors.

`POST /api/v1/jobs` returns `202`:

```json
{
  "job_id": "7e524b50-7651-49c0-a997-aedcaa1c434d",
  "status": "QUEUED",
  "total_count": 3,
  "valid_count": 2,
  "invalid_count": 1,
  "status_url": "/api/v1/jobs/7e524b50-7651-49c0-a997-aedcaa1c434d"
}
```

`GET /api/v1/jobs/{job_id}`: the job's fields plus `pending_count`,
`processing_count`, `success_count`, `invalid_count`, `failed_count`,
`processed_count`, `progress_percent` (2 decimals), `error_code`,
`error_message`, timestamps, and `recipients_url`. The job row and all counts
are read by **one SQL statement** (`count(*) FILTER (WHERE status = ...)`
grouped by the job), so they come from one database snapshot and always add up
to `total_count`, even while a worker is updating rows.

`GET /api/v1/jobs/{job_id}/recipients`: `{"job_id", "total", "limit",
"offset", "items": [...]}` in `input_index` order. Each item has
`recipient_id` (the certificate ID), `input_index`, `status`, `name` and
`email` (null for `INVALID`), `errors` (for `INVALID`/`FAILED`),
`download_url` (only for `SUCCESS`), `started_at`, `completed_at`. An offset
past the end returns an empty `items` list. Pages are read with
`LIMIT/OFFSET`; between page requests a running job may change statuses, but
the order (`input_index`) is stable.

`GET /api/v1/certificates/{certificate_id}/download`:

| Status | Code | When |
|---|---|---|
| `200` | | `SUCCESS`: `application/pdf`, `Content-Disposition: attachment; filename="certificate-{id}.pdf"` |
| `404` | `certificate_not_found` | no recipient with this ID |
| `409` | `certificate_not_ready` | `PENDING` or `PROCESSING` (body includes `status`) |
| `409` | `certificate_not_available` | `INVALID` or `FAILED`; there will be no PDF |
| `500` | `certificate_file_missing` | row is `SUCCESS` but the file cannot be read, or its key resolves outside storage (logged; no path in the response) |

Availability is decided by the database row, never by the file's existence,
so a file left behind after a database failure is not served. The file path
comes from the stored key, confined to `CERTIFICATE_STORAGE_DIR`. The file
is read into memory (about 65 KB) before responding.

## Certificate rendering

`app/services/pdf.py`: `render_certificate(CertificateData) -> bytes`. It is a
pure function of its input: no FastAPI, Celery, database, or file writes
(a test enforces that importing it loads none of those modules).

**Template.** One fixed A4 landscape page: double border, title
"CERTIFICATE / OF PARTICIPATION", "This is to certify that", recipient name,
"has participated in", event name, issuer ("Issued by") and issue date
("Date of issue", formatted like `7 October 2026` without using the system
locale), and the certificate ID in the footer. PDF metadata: title, author
(issuer), subject (event).

**Long text.** Each variable text has a fixed area (`FIELD_SPECS`). The
renderer wraps the text on spaces and lowers the font size in 0.5 pt steps
until every line fits the area's width and all lines fit its height (name
34→12 pt, event 22→8 pt, issuer 16→7 pt). A word wider than a line is broken
between characters. Line height uses the fonts' real ascent and descent
(0.94 / 0.24 em), so accented capitals stay inside their area. If text still
does not fit, `TextDoesNotFitError` is raised; text is never drawn clipped.
Tests show the longest values validation accepts fit even when made entirely
of the widest supported glyph.

**Fonts.** DejaVu Serif, Serif Bold, and Sans 2.37 are bundled in
`app/assets/fonts/` (Bitstream Vera license, redistributable; see the
README and LICENSE there) and registered by `app/fonts.py`. ReportLab embeds
only the used glyphs, and every font in the PDF is embedded.

**Characters.** The renderer applies the shared text policy and additionally
checks the glyph table of the exact font each field uses. Errors share the
base class `CertificateRenderError` with a stable `code` (`render_failed`,
`unsupported_characters`, `text_does_not_fit`) and the `field` that caused it.

## Storage

`app/services/storage.py`. PDFs are written to a directory shared by the API
and worker containers (`CERTIFICATE_STORAGE_DIR`, a named Docker volume). The
database stores only metadata and a `storage_key`.

- **Key:** `{certificate_id}.pdf`, built from the recipient ID, never from user
  input. `resolve_storage_path` refuses any key that would resolve outside the
  storage root.
- **Atomic write:** bytes go to a hidden temporary file in the target's own
  directory (same filesystem), are flushed and `fsync`ed, then `os.replace`
  swaps it into place, and the directory is `fsync`ed (POSIX). Readers see the
  old file, the complete new file, or no file; never a partial PDF. On error
  the temporary file is removed and any previous file is left untouched.
- **Not handled:** if the process is killed mid-write, a `.<key>.*.tmp` file
  can remain. It is never served (not a valid key) but is not cleaned up.

## Testing

- Tests run in the `tests` Compose service against a **dedicated test
  database** named by `TEST_DATABASE_URL` (`certificates_test`). DATABASE_URL
  is replaced with it before the app is imported, so the app under test
  cannot reach the application database.
- Each run drops and recreates the test database, then applies the real
  migrations with the Alembic CLI. Each test starts with truncated tables.
- Guards (`tests/database.py`): the test database name must end in `_test`
  and must differ from the application database name. Every create, drop,
  migrate, and truncate helper rechecks the name. Otherwise the run stops
  with exit code 4 before touching any database.
- The test process uses Celery's in-memory transport: `CELERY_BROKER_URL` is
  set to `memory://` before the app is imported (Celery reads that variable
  ahead of its own configuration), so tests never reach the real Redis.
  `tests/test_dispatch.py` reads back the real published message (task name
  and `[job_id]` only), executes it through the registered task, checks
  redelivery for finished and claimed jobs, and simulates broker outages by
  replacing the task object with one that raises kombu's `OperationalError`.
  Most API tests replace `enqueue_job` with a recorder.
- **Integration tests** (`tests/test_integration.py`, marker `integration`)
  start a real `uvicorn` API process and a real `celery worker` process,
  connected through Redis database 1 (`TEST_REDIS_URL`; the application
  worker consumes database 0, and a guard refuses to use the same one), with
  the test database and temporary storage. They submit the documented sample
  request over HTTP, poll until the job finishes, list results, and download
  the PDFs; and they check a real unreachable broker gives `503` and a
  `FAILED` job. Skip them with `pytest -m "not integration"`.

### Checklist coverage

| Scenario | Expected | Tests |
|---|---|---|
| Valid batch | job created; all PDFs generated | `test_jobs_api::test_valid_batch_creates_queued_job_and_pending_recipients`, `test_generation::test_all_valid_recipients_produce_real_parseable_pdfs` |
| Missing event information | `422`, no job | `test_jobs_api::test_missing_shared_field_returns_422`, `::test_invalid_shared_fields_return_422_and_create_nothing` |
| Empty recipient list | `422` | `test_jobs_api::test_empty_recipient_list_returns_422` |
| Batch above the limit | `422` | `test_jobs_api::test_more_than_the_recipient_limit_returns_422` (and `::test_exactly_the_recipient_limit_is_accepted`) |
| Mixed valid and invalid | valid continue; invalid keep errors | `test_jobs_api::test_mixed_batch_keeps_valid_and_invalid_outcomes`, `test_generation::test_invalid_recipients_are_skipped` |
| Malformed individual entry | rejected alone | `test_jobs_api::test_malformed_entries_do_not_reject_a_valid_sibling`, `test_validation::test_non_object_entry_is_invalid_as_a_whole` |
| All recipients invalid | `422` with indexed errors | `test_jobs_api::test_all_invalid_recipients_return_422_with_indexed_errors` |
| Duplicate names | distinct IDs and files | `test_jobs_api::test_duplicate_names_get_distinct_recipient_ids`, `test_generation::test_duplicate_names_get_distinct_certificate_ids_and_files` |
| PDF generation | parseable, expected text | `test_pdf::test_pdf_contains_every_certificate_detail` and the rest of `test_pdf.py` |
| One renderer failure | others still succeed | `test_generation::test_renderer_failure_for_the_middle_recipient_does_not_stop_the_next`, `test_results_api::test_one_injected_failure_leaves_the_others_downloadable` |
| Progress | counts add up; 100% at the end | `test_results_api::test_status_during_processing_matches_the_recipient_rows` (paused at a controlled point), `::test_status_after_processing`, `test_progress.py` |
| Successful download | correct PDF response | `test_results_api::test_download_returns_the_pdf_with_type_and_filename` |
| Missing or unfinished certificate | documented error | `test_results_api::test_download_of_an_unknown_certificate_returns_404`, `::test_download_of_an_unfinished_certificate_returns_409_not_ready`, `::test_successful_certificate_with_a_missing_file_returns_500` |
| Queue unavailable | explicit error, no false success | `test_dispatch::test_broker_outage_returns_503_and_marks_the_job_failed`, `test_integration::test_unreachable_broker_gives_503_and_a_failed_job_never_a_false_success` |
| Repeated processing attempt | no duplicate results | `test_generation::test_reprocessing_a_finished_job_generates_nothing`, `::test_simultaneous_claims_have_exactly_one_winner`, `test_dispatch::test_redelivered_message_for_a_claimed_job_does_nothing` |
| Real worker integration | API-created job produces downloadable files | `test_integration::test_api_created_job_is_processed_by_a_real_worker_and_downloadable` |
- PDF tests render real PDFs and read them back with pypdf.
- Processing tests (`tests/test_generation.py`) use the real renderer and
  storage in a temporary directory. Failures are injected with `monkeypatch`
  (renderer, `os.replace` inside the real atomic write) or a SQLAlchemy
  `before_cursor_execute` hook that raises a real driver error; there is no
  public parameter for failing certificates. Concurrency tests synchronize
  with events, a barrier, and a check for a session waiting on a row lock,
  not with fixed sleeps.
- Sample certificates for manual review are in `examples/sample-certificates/`
  (regenerate with `python -m scripts.render_sample_certificates DIR`).
- Known warning: Starlette prints `StarletteDeprecationWarning: Using httpx
  with starlette.testclient is deprecated; install httpx2 instead`. We keep
  the locked, tested `httpx` (as decided) and do not suppress the warning.

## Current limitations

- **No crash recovery.** If a worker dies mid-job or hits a database error,
  the job stays `PROCESSING` (and possibly one recipient too). Redelivered
  messages and manual runs return `already_processing`; nothing detects
  abandoned jobs or marks their unfinished entries as failed. Deferred.
- **Commit-to-publish window** and lost Redis writes can leave `QUEUED` jobs
  that no message will process; there is no outbox or sweeper.
- While the broker is unreachable, a submission waits for the publish to fail
  (8–16 s measured with a stopped Redis container) before the `503`, holding
  an API worker thread meanwhile.
- **Unreferenced files** can remain after a database failure that follows a
  successful save, and abandoned temporary files after a killed write. No
  cleanup exists.
- **Character coverage** is limited to the scripts listed in the text policy.
- Recipients are processed sequentially; the pending ID list for a job is read
  into memory at once (fine for 1,000 recipients).
- No authentication or authorization: anyone who can reach the API can read
  any job and download any certificate whose ID they know (IDs are random UUIDs).
- Submitting the same request twice creates two jobs (no idempotency key).

## Implementation phases

1. Project foundation: config, DB connection, Alembic, Docker, `/health`. ✅
2. Models, migration, recipient validation, `POST /api/v1/jobs`, isolated
   test database (moved forward from Phase 7). ✅
3. Standalone ReportLab certificate renderer and atomic storage. ✅
4. `process_job(job_id)` with per-recipient failure isolation, shared text
   policy at submission, manual processing command. ✅
5. Celery and Redis integration: one task per job, publish-failure handling,
   delivery settings, Redis persistence, measured interruption behaviour. ✅
6. Job status (one-snapshot counts), paginated recipient results,
   certificate downloads with documented error responses. ✅
7. Complete automated tests, including real Redis + Celery integration and
   checklist coverage. ✅
8. README, sample requests, curl demo, benchmark, verification from a clean
   clone. ✅

## Measured performance

Single runs on the development machine (Docker Desktop on Windows, worker
concurrency 2), not throughput guarantees:

| Batch | `202` after | Worker processing (`started_at` to `completed_at`) | Per certificate | Outcome |
|---|---|---|---|---|
| 100 recipients (`scripts/benchmark.py`) | 0.25 s | 3.63 s | 36 ms | 100 `SUCCESS` |
| 1,000 recipients (Phase 5 demo) | – | 29.9 s | 30 ms | 1,000 `SUCCESS` |
