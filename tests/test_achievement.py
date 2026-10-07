"""The optional `achievement` field end to end: validation, persistence, the
queued worker path, the certificate itself, and the results API."""

import functools
import io
import json
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from reportlab.pdfbase import pdfmetrics
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import worker
from app.db import engine
from app.models import JobRecipient
from app.schemas import MAX_ACHIEVEMENT_LENGTH
from app.services import generation
from app.services.pdf import (
    ACHIEVEMENT_FIELD_SPECS,
    ACHIEVEMENT_PHRASE,
    ACHIEVEMENT_PHRASE_BASELINE,
    FIELD_SPECS,
    FONT_ASCENT,
    FONT_DESCENT,
    CertificateData,
    UnsupportedCharactersError,
    layout_certificate,
    render_certificate,
)
from app.services.validation import validate_recipients
from app.text_policy import supported_code_points
from tests.database import PROJECT_ROOT

ACHIEVEMENT = "Outstanding Performance in Cloud Computing"
JOBS_URL = "/api/v1/jobs"
SAMPLE = CertificateData(
    uuid.UUID("7e524b50-7651-49c0-a997-aedcaa1c434d"), "Kavi", "Cloud Computing Bootcamp", "ABC Academy", date(2026, 10, 7)
)


def page_text(pdf: bytes) -> str:
    return PdfReader(io.BytesIO(pdf)).pages[0].extract_text()


def squash(value: str) -> str:
    return "".join(value.split())


def stored_achievements(session: Session, job_id: str) -> list[str | None]:
    query = select(JobRecipient.achievement).where(JobRecipient.job_id == uuid.UUID(job_id)).order_by(JobRecipient.input_index)
    return list(session.scalars(query))


# --- validation -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"name": "Kavi", "achievement": ACHIEVEMENT}, ACHIEVEMENT),
        ({"name": "Kavi"}, None),
        ({"name": "Kavi", "achievement": None}, None),
        ({"name": "Kavi", "achievement": ""}, None),
        ({"name": "Kavi", "achievement": "   "}, None),
        ({"name": "Kavi", "achievement": "  Cafe\u0301 Award  "}, "Café Award"),
        ({"name": "Kavi", "achievement": "e\u0301" * MAX_ACHIEVEMENT_LENGTH}, "é" * MAX_ACHIEVEMENT_LENGTH),
    ],
    ids=["given", "omitted", "null", "empty", "blank", "normalized", "longest-after-normalization"],
)
def test_achievement_is_optional_and_normalized(entry: dict[str, Any], expected: str | None) -> None:
    result = validate_recipients([entry])

    assert result.invalid == []
    assert result.valid[0].achievement == expected


@pytest.mark.parametrize(
    ("achievement", "code"),
    [
        ("x" * (MAX_ACHIEVEMENT_LENGTH + 1), "string_too_long"),
        ("Top Performer 🏆", "unsupported_characters"),
        ("Line one\nLine two", "unsupported_characters"),
        (42, "string_type"),
    ],
    ids=["too-long", "emoji", "newline", "not-a-string"],
)
def test_invalid_achievement_makes_only_that_entry_invalid(achievement: object, code: str) -> None:
    result = validate_recipients([{"name": "Kavi", "achievement": achievement}, {"name": "Asha"}])

    assert [recipient.input_index for recipient in result.valid] == [1]
    [error] = result.invalid[0].errors
    assert (error["field"], error["code"]) == ("achievement", code)


# --- API and persistence --------------------------------------------------------------


def test_request_with_achievement_is_accepted_and_persisted(
    client: TestClient, db_session: Session, dispatched: list[Any]
) -> None:
    payload = json.loads((PROJECT_ROOT / "examples" / "job-request-with-achievement.json").read_text(encoding="utf-8"))

    response = client.post(JOBS_URL, json=payload)

    assert response.status_code == 202
    assert (response.json()["valid_count"], response.json()["invalid_count"]) == (2, 0)
    assert stored_achievements(db_session, response.json()["job_id"]) == [ACHIEVEMENT, None]


def test_request_without_achievement_behaves_as_before(
    client: TestClient, db_session: Session, dispatched: list[Any]
) -> None:
    payload = json.loads((PROJECT_ROOT / "examples" / "job-request.json").read_text(encoding="utf-8"))

    response = client.post(JOBS_URL, json=payload)

    assert response.status_code == 202
    assert set(response.json()) == {"job_id", "status", "total_count", "valid_count", "invalid_count", "status_url"}
    assert stored_achievements(db_session, response.json()["job_id"]) == [None, None, None]


def test_invalid_achievement_does_not_reject_its_siblings(
    client: TestClient, db_session: Session, dispatched: list[Any], job_payload: Callable[..., dict[str, Any]]
) -> None:
    payload = job_payload([{"name": "Kavi", "achievement": "x" * 151}, {"name": "Asha", "achievement": ACHIEVEMENT}])

    response = client.post(JOBS_URL, json=payload)

    assert response.status_code == 202
    assert (response.json()["valid_count"], response.json()["invalid_count"]) == (1, 1)
    # The rejected text is not stored; only the valid sibling's achievement is.
    assert stored_achievements(db_session, response.json()["job_id"]) == [None, ACHIEVEMENT]


def test_database_never_stores_an_achievement_on_an_invalid_row(create_job: Callable[..., uuid.UUID]) -> None:
    job_id = create_job([{"name": ""}, {"name": "Asha"}])

    with pytest.raises(IntegrityError, match="ck_job_recipients_invalid_has_no_achievement"), engine.begin() as connection:
        connection.execute(
            text("UPDATE job_recipients SET achievement = 'Smuggled' WHERE job_id = :job_id AND status = 'INVALID'"),
            {"job_id": job_id},
        )


def test_recipient_results_include_the_achievement(client: TestClient, create_job: Callable[..., uuid.UUID]) -> None:
    job_id = create_job([{"name": "Kavi", "achievement": ACHIEVEMENT}, {"name": "Asha"}])

    items = client.get(f"/api/v1/jobs/{job_id}/recipients").json()["items"]

    assert [(item["name"], item["achievement"]) for item in items] == [("Kavi", ACHIEVEMENT), ("Asha", None)]


# --- through the queue to the worker ---------------------------------------------------------


def test_achievement_reaches_the_worker_through_the_queued_job(
    client: TestClient,
    broker_messages: Callable[[], list[Any]],
    monkeypatch: pytest.MonkeyPatch,
    storage_root: Path,
) -> None:
    rendered: dict[str, str | None] = {}
    real_render = generation.render_certificate

    def recording_render(data: CertificateData) -> bytes:
        rendered[data.recipient_name] = data.achievement
        return real_render(data)

    monkeypatch.setattr(generation, "render_certificate", recording_render)
    monkeypatch.setattr(worker, "process_job", functools.partial(generation.process_job, storage_root=storage_root))
    payload = json.loads((PROJECT_ROOT / "examples" / "job-request-with-achievement.json").read_text(encoding="utf-8"))

    job_id = client.post(JOBS_URL, json=payload).json()["job_id"]
    [(task_name, args, kwargs)] = broker_messages()
    # The message still carries only the job ID; the worker reads the achievement
    # from PostgreSQL, like every other recipient value.
    assert (args, kwargs) == ([job_id], {})
    result = worker.celery_app.tasks[task_name](*args, **kwargs)

    assert result["status"] == "COMPLETED"
    assert rendered == {"Kavi": ACHIEVEMENT, "Asha Sharma": None}
    items = client.get(f"/api/v1/jobs/{job_id}/recipients").json()["items"]
    kavi = page_text((storage_root / f"{items[0]['recipient_id']}.pdf").read_bytes())
    asha = page_text((storage_root / f"{items[1]['recipient_id']}.pdf").read_bytes())
    assert ACHIEVEMENT in kavi and squash(ACHIEVEMENT_PHRASE) in squash(kavi)
    assert squash(ACHIEVEMENT_PHRASE) not in squash(asha)


# --- the certificate ------------------------------------------------------------------------


def test_certificate_shows_the_achievement() -> None:
    text = page_text(render_certificate(replace(SAMPLE, achievement=ACHIEVEMENT)))

    assert ACHIEVEMENT in text
    assert squash(ACHIEVEMENT_PHRASE) in squash(text)
    for expected in ("Kavi", "Cloud Computing Bootcamp", "ABC Academy", "7 October 2026"):
        assert expected in text


@pytest.mark.parametrize("achievement", [None, "", "   "], ids=["none", "empty", "blank"])
def test_certificate_without_achievement_is_laid_out_exactly_as_before(achievement: str | None) -> None:
    data = replace(SAMPLE, achievement=achievement)

    blocks = layout_certificate(data)

    assert [block.field for block in blocks] == list(FIELD_SPECS)
    assert all(block.spec == FIELD_SPECS[block.field] for block in blocks)
    text = page_text(render_certificate(data))
    assert squash(ACHIEVEMENT_PHRASE) not in squash(text)
    assert text == page_text(render_certificate(SAMPLE))


def test_longest_achievement_and_event_fit_without_overlapping() -> None:
    widest = max((chr(c) for c in supported_code_points()), key=lambda char: pdfmetrics.stringWidth(char, "DejaVuSerif-Bold", 10))
    data = replace(SAMPLE, event_name=widest * 200, achievement=widest * MAX_ACHIEVEMENT_LENGTH)

    blocks = {block.field: block for block in layout_certificate(data)}

    for block in blocks.values():
        box = block.spec.box
        assert all(pdfmetrics.stringWidth(line, block.spec.font_name, block.font_size) <= box.width for line in block.lines)
        assert len(block.lines) * block.line_height <= box.height
    # Event area, fixed phrase, and achievement area are stacked without overlap,
    # above the issuer and date areas.
    phrase_top = ACHIEVEMENT_PHRASE_BASELINE + FONT_ASCENT * 13
    phrase_bottom = ACHIEVEMENT_PHRASE_BASELINE - FONT_DESCENT * 13
    assert blocks["event_name"].spec.box.y >= phrase_top
    assert blocks["achievement"].spec.box.top <= phrase_bottom
    assert blocks["achievement"].spec.box.y >= ACHIEVEMENT_FIELD_SPECS["issuer_name"].box.top
    assert "".join(blocks["achievement"].lines) == widest * MAX_ACHIEVEMENT_LENGTH


def test_unsupported_achievement_in_an_older_record_fails_at_render_time() -> None:
    with pytest.raises(UnsupportedCharactersError) as error:
        render_certificate(replace(SAMPLE, achievement="Top Performer 🏆"))

    assert error.value.field == "achievement"
