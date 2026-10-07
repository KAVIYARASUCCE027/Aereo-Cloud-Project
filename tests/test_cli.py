"""python -m app.cli process-job"""

import json
import uuid
from pathlib import Path

import pytest

from app.cli import main
from app.schemas import JobCreateRequest
from app.db import SessionLocal
from app.services import jobs


def test_process_job_command_generates_certificates(
    tmp_path: Path, job_payload, capsys: pytest.CaptureFixture[str]
) -> None:
    request = JobCreateRequest.model_validate(job_payload([{"name": "Asha"}, {"name": ""}]))
    with SessionLocal() as session:
        job_id = jobs.create_job(session, request).job_id

    exit_code = main(["process-job", str(job_id), "--storage-dir", str(tmp_path)])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["outcome"] == "processed"
    assert output["status"] == "COMPLETED_WITH_ERRORS"
    assert (output["success_count"], output["invalid_count"], output["progress_percent"]) == (1, 1, 100)
    assert len(list(tmp_path.glob("*.pdf"))) == 1


def test_process_job_command_reports_unknown_job(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["process-job", str(uuid.uuid4()), "--storage-dir", str(tmp_path)])

    assert exit_code == 1
    assert "does not exist" in capsys.readouterr().err


def test_process_job_command_rejects_a_malformed_id() -> None:
    with pytest.raises(SystemExit) as error:
        main(["process-job", "not-a-uuid"])

    assert error.value.code == 2
