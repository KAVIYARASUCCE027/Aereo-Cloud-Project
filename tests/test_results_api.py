"""GET job status, paginated recipient results, and certificate downloads."""

import io
import logging
import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from kombu.exceptions import OperationalError as BrokerOperationalError
from pypdf import PdfReader
from sqlalchemy import text

from app.api.certificates import get_storage_root
from app.db import engine
from app.main import app
from app.services import dispatch, generation
from app.services.generation import process_job
from app.services.pdf import CertificateData, TextDoesNotFitError

real_render = generation.render_certificate
CreateJob = Callable[..., uuid.UUID]


@pytest.fixture
def downloads(storage_root: Path) -> Iterator[Path]:
    """Serve downloads from the same temporary storage process_job writes to."""
    app.dependency_overrides[get_storage_root] = lambda: storage_root
    yield storage_root
    app.dependency_overrides.pop(get_storage_root, None)


def status_of(client: TestClient, job_id: uuid.UUID) -> dict[str, Any]:
    response = client.get(f"/api/v1/jobs/{job_id}")
    assert response.status_code == 200
    return response.json()


def recipients_of(client: TestClient, job_id: uuid.UUID, **params: int) -> dict[str, Any]:
    response = client.get(f"/api/v1/jobs/{job_id}/recipients", params=params)
    assert response.status_code == 200
    return response.json()


def assert_counts_add_up(body: dict[str, Any]) -> None:
    counts = ["pending_count", "processing_count", "success_count", "invalid_count", "failed_count"]
    assert sum(body[name] for name in counts) == body["total_count"]
    assert body["processed_count"] == body["success_count"] + body["invalid_count"] + body["failed_count"]
    assert body["progress_percent"] == round(body["processed_count"] / body["total_count"] * 100, 2)


def render_failing_for(name: str) -> Callable[[CertificateData], bytes]:
    def render(data: CertificateData) -> bytes:
        if data.recipient_name == name:
            raise TextDoesNotFitError("recipient_name", "recipient_name does not fit")
        return real_render(data)

    return render


# --- job status -------------------------------------------------------------------------


def test_status_of_a_queued_job(client: TestClient, create_job: CreateJob) -> None:
    job_id = create_job([{"name": "Asha"}, {"name": ""}, {"name": "Rohan"}])

    body = status_of(client, job_id)

    assert body == body | {
        "job_id": str(job_id),
        "status": "QUEUED",
        "event_name": "Backend Development Workshop",
        "issuer_name": "ABC Academy",
        "issue_date": "2026-10-07",
        "total_count": 3,
        "pending_count": 2,
        "processing_count": 0,
        "success_count": 0,
        "invalid_count": 1,
        "failed_count": 0,
        "processed_count": 1,  # the INVALID entry already has its final outcome
        "progress_percent": 33.33,
        "error_code": None,
        "error_message": None,
        "started_at": None,
        "completed_at": None,
        "recipients_url": f"/api/v1/jobs/{job_id}/recipients",
    }
    assert_counts_add_up(body)


def test_status_after_processing(client: TestClient, create_job: CreateJob, storage_root: Path) -> None:
    job_id = create_job([{"name": "Asha"}, {"name": ""}, {"name": "Rohan"}])
    process_job(job_id, storage_root=storage_root)

    body = status_of(client, job_id)

    assert (body["status"], body["success_count"], body["invalid_count"], body["processed_count"]) == (
        "COMPLETED_WITH_ERRORS",
        2,
        1,
        3,
    )
    assert body["progress_percent"] == 100
    assert body["started_at"] is not None and body["completed_at"] is not None
    assert_counts_add_up(body)


def test_status_during_processing_matches_the_recipient_rows(
    client: TestClient, create_job: CreateJob, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = create_job([{"name": "First"}, {"name": "Second"}, {"name": "Third"}, {"name": ""}])
    rendering_second, release = threading.Event(), threading.Event()

    def pausing_render(data: CertificateData) -> bytes:
        if data.recipient_name == "Second":
            rendering_second.set()
            if not release.wait(timeout=30):
                raise TimeoutError("test did not release the renderer")
        return real_render(data)

    monkeypatch.setattr(generation, "render_certificate", pausing_render)

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(process_job, job_id, storage_root)
        try:
            assert rendering_second.wait(timeout=30)
            paused = status_of(client, job_id)
        finally:
            release.set()
        running.result(timeout=60)

    assert paused["status"] == "PROCESSING"
    assert [paused[name] for name in ("pending_count", "processing_count", "success_count", "invalid_count")] == [
        1,
        1,
        1,
        1,
    ]
    assert paused["progress_percent"] == 50
    assert_counts_add_up(paused)
    assert status_of(client, job_id)["progress_percent"] == 100


def test_status_of_a_job_that_could_not_be_queued(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, job_payload: Callable[..., dict[str, Any]]
) -> None:
    class BrokerDownTask:
        def apply_async(self, args: list[str], **_: Any) -> None:
            raise BrokerOperationalError("Error 111 connecting to redis:6379. Connection refused.")

    monkeypatch.setattr(dispatch, "process_job_task", BrokerDownTask())
    job_id = client.post("/api/v1/jobs", json=job_payload([{"name": "Asha"}, {"name": ""}])).json()["detail"]["job_id"]

    body = status_of(client, uuid.UUID(job_id))

    assert (body["status"], body["error_code"], body["failed_count"], body["invalid_count"]) == (
        "FAILED",
        "dispatch_failed",
        1,
        1,
    )
    assert body["progress_percent"] == 100
    assert body["started_at"] is None and body["completed_at"] is not None


@pytest.mark.parametrize("path", ["/api/v1/jobs/{id}", "/api/v1/jobs/{id}/recipients"])
def test_unknown_job_returns_404_and_malformed_id_422(client: TestClient, path: str) -> None:
    missing = client.get(path.format(id=uuid.uuid4()))
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "job_not_found"

    assert client.get(path.format(id="not-a-uuid")).status_code == 422


# --- recipient results ------------------------------------------------------------------


def test_one_injected_failure_leaves_the_others_downloadable(
    client: TestClient, create_job: CreateJob, downloads: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = create_job([{"name": "Asha", "email": "asha@example.com"}, {"email": "x"}, {"name": "Breaks"}, {"name": "Rohan"}])
    monkeypatch.setattr(generation, "render_certificate", render_failing_for("Breaks"))
    process_job(job_id, storage_root=downloads)

    body = status_of(client, job_id)
    assert (body["status"], body["success_count"], body["failed_count"], body["invalid_count"]) == (
        "COMPLETED_WITH_ERRORS",
        2,
        1,
        1,
    )
    assert_counts_add_up(body)

    page = recipients_of(client, job_id)
    assert page["total"] == 4
    asha, invalid, broken, rohan = page["items"]
    assert [item["input_index"] for item in page["items"]] == [0, 1, 2, 3]
    assert [item["status"] for item in page["items"]] == ["SUCCESS", "INVALID", "FAILED", "SUCCESS"]

    assert (asha["name"], asha["email"], asha["errors"]) == ("Asha", "asha@example.com", None)
    assert asha["download_url"] == f"/api/v1/certificates/{asha['recipient_id']}/download"
    assert (invalid["name"], invalid["email"], invalid["download_url"]) == (None, None, None)
    assert {error["field"] for error in invalid["errors"]} == {"name", "email"}
    assert broken["download_url"] is None
    assert broken["errors"] == [{"field": "name", "code": "text_does_not_fit", "message": "recipient_name does not fit"}]

    for item in (asha, rohan):
        response = client.get(item["download_url"])
        assert response.status_code == 200
        assert item["name"] in PdfReader(io.BytesIO(response.content)).pages[0].extract_text()
    for item in (invalid, broken):
        response = client.get(f"/api/v1/certificates/{item['recipient_id']}/download")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "certificate_not_available"


def test_recipient_results_are_paginated_in_submission_order(client: TestClient, create_job: CreateJob) -> None:
    job_id = create_job([{"name": f"Recipient {i}"} for i in range(5)])

    pages = [recipients_of(client, job_id, limit=2, offset=offset) for offset in (0, 2, 4, 10)]

    assert [[item["input_index"] for item in page["items"]] for page in pages] == [[0, 1], [2, 3], [4], []]
    assert {page["total"] for page in pages} == {5}
    assert (pages[1]["limit"], pages[1]["offset"]) == (2, 2)
    assert recipients_of(client, job_id)["limit"] == 50  # default page size


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"offset": -1}, {"limit": "many"}])
def test_invalid_page_parameters_return_422(client: TestClient, create_job: CreateJob, params: dict[str, Any]) -> None:
    job_id = create_job([{"name": "Asha"}])

    assert client.get(f"/api/v1/jobs/{job_id}/recipients", params=params).status_code == 422


def test_largest_page_size_is_accepted(client: TestClient, create_job: CreateJob) -> None:
    job_id = create_job([{"name": f"Recipient {i}"} for i in range(201)])

    assert len(recipients_of(client, job_id, limit=200)["items"]) == 200


# --- downloads ---------------------------------------------------------------------------


def test_download_returns_the_pdf_with_type_and_filename(
    client: TestClient, create_job: CreateJob, downloads: Path
) -> None:
    job_id = create_job([{"name": "Zoë Ångström"}])
    process_job(job_id, storage_root=downloads)
    [item] = recipients_of(client, job_id)["items"]

    response = client.get(item["download_url"])

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == f'attachment; filename="certificate-{item["recipient_id"]}.pdf"'
    assert response.content == (downloads / f"{item['recipient_id']}.pdf").read_bytes()
    text = PdfReader(io.BytesIO(response.content)).pages[0].extract_text()
    assert "Zoë Ångström" in text and item["recipient_id"] in text


def test_download_of_an_unknown_certificate_returns_404(client: TestClient, downloads: Path) -> None:
    response = client.get(f"/api/v1/certificates/{uuid.uuid4()}/download")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "certificate_not_found"


@pytest.mark.parametrize("status", ["PENDING", "PROCESSING"])
def test_download_of_an_unfinished_certificate_returns_409_not_ready(
    client: TestClient, create_job: CreateJob, downloads: Path, status: str
) -> None:
    job_id = create_job([{"name": "Asha"}])
    recipient_id = recipients_of(client, job_id)["items"][0]["recipient_id"]
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE job_recipients SET status = :status, started_at = now() WHERE id = :id"),
            {"status": status, "id": recipient_id},
        )

    response = client.get(f"/api/v1/certificates/{recipient_id}/download")

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "certificate_not_ready",
        "message": "The certificate has not been generated yet.",
        "status": status,
    }


def test_a_file_on_disk_alone_does_not_make_a_certificate_downloadable(
    client: TestClient, create_job: CreateJob, downloads: Path
) -> None:
    # As after a database failure following the save: file exists, row is not SUCCESS.
    job_id = create_job([{"name": "Asha"}])
    recipient_id = recipients_of(client, job_id)["items"][0]["recipient_id"]
    downloads.mkdir(parents=True)
    (downloads / f"{recipient_id}.pdf").write_bytes(b"%PDF-1.4 orphan")

    response = client.get(f"/api/v1/certificates/{recipient_id}/download")

    assert response.status_code == 409


def test_successful_certificate_with_a_missing_file_returns_500(
    client: TestClient, create_job: CreateJob, downloads: Path, caplog: pytest.LogCaptureFixture
) -> None:
    job_id = create_job([{"name": "Asha"}])
    process_job(job_id, storage_root=downloads)
    recipient_id = recipients_of(client, job_id)["items"][0]["recipient_id"]
    (downloads / f"{recipient_id}.pdf").unlink()

    with caplog.at_level(logging.ERROR, logger="app.services.results"):
        response = client.get(f"/api/v1/certificates/{recipient_id}/download")

    assert response.status_code == 500
    assert response.json()["detail"] == {
        "code": "certificate_file_missing",
        "message": "The certificate file is unexpectedly missing.",
    }
    assert str(downloads) not in response.text  # no internal paths for the client
    assert any(recipient_id in record.getMessage() for record in caplog.records)  # but logged


def test_a_storage_key_outside_the_storage_root_is_never_served(
    client: TestClient, create_job: CreateJob, downloads: Path
) -> None:
    job_id = create_job([{"name": "Asha"}])
    process_job(job_id, storage_root=downloads)
    recipient_id = recipients_of(client, job_id)["items"][0]["recipient_id"]
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE job_recipients SET storage_key = '../../../etc/passwd' WHERE id = :id"), {"id": recipient_id}
        )

    response = client.get(f"/api/v1/certificates/{recipient_id}/download")

    assert response.status_code == 500
    assert "root:" not in response.text


def test_openapi_documents_the_status_results_and_download_responses(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert set(paths["/api/v1/jobs/{job_id}"]["get"]["responses"]) >= {"200", "404", "422"}
    assert set(paths["/api/v1/jobs/{job_id}/recipients"]["get"]["responses"]) >= {"200", "404", "422"}
    download = paths["/api/v1/certificates/{certificate_id}/download"]["get"]["responses"]
    assert set(download) >= {"200", "404", "409", "500"}
    assert "application/pdf" in download["200"]["content"]
