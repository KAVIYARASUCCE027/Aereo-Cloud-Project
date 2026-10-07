"""End-to-end: real API process -> real Redis -> real Celery worker process.

Unlike the rest of the suite (in-memory broker, no worker), these tests start
`uvicorn` and `celery worker` as separate processes and talk to them over
HTTP, exactly like a client would. Both processes use the isolated test
database, a temporary certificate directory, and a Redis database that the
application's own worker does not consume (TEST_REDIS_URL, database 1 in
Docker Compose), so the running application cannot take these messages.

Waiting is condition based (poll until a state is reached, with a deadline),
never a fixed sleep that the test depends on.
"""

import io
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import redis
from pypdf import PdfReader

from tests.conftest import APPLICATION_BROKER_URL
from tests.database import PROJECT_ROOT

pytestmark = pytest.mark.integration

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL")
SAMPLE_REQUEST = json.loads((PROJECT_ROOT / "examples" / "job-request.json").read_text(encoding="utf-8"))
TERMINAL = {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"}


def redis_target(url: str) -> tuple[str, int, int]:
    parts = urlsplit(url)
    return parts.hostname or "", parts.port or 6379, int(parts.path.strip("/") or 0)


@pytest.fixture
def test_redis() -> str:
    if not TEST_REDIS_URL:
        pytest.skip("TEST_REDIS_URL is not set (set automatically by `docker compose run tests`)")
    if APPLICATION_BROKER_URL and redis_target(TEST_REDIS_URL) == redis_target(APPLICATION_BROKER_URL):
        pytest.exit("Refusing to run: TEST_REDIS_URL is the application's broker database", returncode=4)
    redis.Redis.from_url(TEST_REDIS_URL).flushdb()  # test database only, checked above
    return TEST_REDIS_URL


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(condition: Callable[[], bool], what: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.2)


@contextmanager
def background_process(args: list[str], env: dict[str, str], log: Path) -> Iterator[subprocess.Popen]:
    with log.open("w") as output:
        process = subprocess.Popen(args, cwd=PROJECT_ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            yield process
        finally:
            process.terminate()  # SIGTERM: warm shutdown for Celery, graceful stop for uvicorn
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def process_env(broker_url: str, storage_dir: Path) -> dict[str, str]:
    # DATABASE_URL already points at the test database (see conftest.py).
    return {**os.environ, "CELERY_BROKER_URL": broker_url, "CERTIFICATE_STORAGE_DIR": str(storage_dir), "LOG_LEVEL": "INFO"}


@contextmanager
def api_process(env: dict[str, str], log: Path) -> Iterator[httpx.Client]:
    port = free_port()
    args = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)]
    with background_process(args, env, log) as process, httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:

        def healthy() -> bool:
            assert process.poll() is None, log.read_text()
            try:
                return client.get("/health").status_code == 200
            except httpx.TransportError:
                return False

        wait_until(healthy, "the API process to answer /health")
        yield client


@contextmanager
def worker_process(env: dict[str, str], log: Path) -> Iterator[subprocess.Popen]:
    args = [
        sys.executable, "-m", "celery", "-A", "app.worker:celery_app", "worker",
        "--pool=solo", "--loglevel=INFO", "--without-mingle", "--without-gossip", "--hostname=integration@%h",
    ]  # fmt: skip
    with background_process(args, env, log) as process:

        def ready() -> bool:
            assert process.poll() is None, log.read_text()
            return " ready." in log.read_text()

        wait_until(ready, "the Celery worker to be ready")
        yield process


def poll_until_finished(client: httpx.Client, job_id: str) -> dict[str, Any]:
    latest: dict[str, Any] = {}

    def finished() -> bool:
        latest.update(client.get(f"/api/v1/jobs/{job_id}").json())
        return latest["status"] in TERMINAL

    wait_until(finished, f"job {job_id} to finish")
    return latest


def test_api_created_job_is_processed_by_a_real_worker_and_downloadable(
    test_redis: str, storage_root: Path, tmp_path: Path
) -> None:
    env = process_env(test_redis, storage_root)

    with worker_process(env, tmp_path / "worker.log"), api_process(env, tmp_path / "api.log") as api:
        submitted = api.post("/api/v1/jobs", json=SAMPLE_REQUEST)
        assert submitted.status_code == 202, submitted.text
        job_id = submitted.json()["job_id"]

        final = poll_until_finished(api, job_id)
        page = api.get(f"/api/v1/jobs/{job_id}/recipients").json()
        downloads = {item["name"]: api.get(item["download_url"]) for item in page["items"] if item["download_url"]}

    assert (final["status"], final["success_count"], final["invalid_count"], final["failed_count"]) == (
        "COMPLETED_WITH_ERRORS",
        2,
        1,
        0,
    )
    assert final["progress_percent"] == 100
    assert [item["status"] for item in page["items"]] == ["SUCCESS", "SUCCESS", "INVALID"]
    assert set(downloads) == {"Asha Sharma", "Rohan Gupta"}
    for name, response in downloads.items():
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert name in PdfReader(io.BytesIO(response.content)).pages[0].extract_text()

    # The PDFs were produced by the worker process, not by the API process.
    worker_log = (tmp_path / "worker.log").read_text()
    assert f"Job {job_id} finished as COMPLETED_WITH_ERRORS" in worker_log
    assert "certificates.process_job" in worker_log
    assert "finished as" not in (tmp_path / "api.log").read_text()


def test_unreachable_broker_gives_503_and_a_failed_job_never_a_false_success(storage_root: Path, tmp_path: Path) -> None:
    # Real kombu publish path against a port nothing listens on.
    env = process_env("redis://127.0.0.1:1/0", storage_root)

    with api_process(env, tmp_path / "api.log") as api:
        started = time.monotonic()
        submitted = api.post("/api/v1/jobs", json=SAMPLE_REQUEST)
        elapsed = time.monotonic() - started
        assert submitted.status_code == 503, submitted.text
        detail = submitted.json()["detail"]
        status = api.get(detail["status_url"]).json()

    assert (detail["code"], detail["job_status"]) == ("dispatch_failed", "FAILED")
    assert (status["status"], status["error_code"], status["failed_count"], status["invalid_count"]) == (
        "FAILED",
        "dispatch_failed",
        2,
        1,
    )
    assert elapsed < 10  # a refused connection fails fast; recorded for the docs
    assert uuid.UUID(detail["job_id"])


def test_real_worker_prints_the_optional_achievement(test_redis: str, storage_root: Path, tmp_path: Path) -> None:
    request = json.loads((PROJECT_ROOT / "examples" / "job-request-with-achievement.json").read_text(encoding="utf-8"))
    env = process_env(test_redis, storage_root)

    with worker_process(env, tmp_path / "worker.log"), api_process(env, tmp_path / "api.log") as api:
        job_id = api.post("/api/v1/jobs", json=request).json()["job_id"]
        final = poll_until_finished(api, job_id)
        items = api.get(f"/api/v1/jobs/{job_id}/recipients").json()["items"]
        texts = {
            item["name"]: PdfReader(io.BytesIO(api.get(item["download_url"]).content)).pages[0].extract_text()
            for item in items
        }

    assert final["status"] == "COMPLETED"
    assert [item["achievement"] for item in items] == ["Outstanding Performance in Cloud Computing", None]
    assert "Outstanding Performance in Cloud Computing" in texts["Kavi"]
    assert "recognized" not in texts["Asha Sharma"]  # no achievement: the original layout
