"""Atomic certificate storage."""

import io
import os
import uuid
from datetime import date
from pathlib import Path

import pytest
from pypdf import PdfReader

from app.services import storage
from app.services.pdf import CertificateData, render_certificate
from app.services.storage import (
    InvalidStorageKeyError,
    certificate_storage_key,
    resolve_storage_path,
    write_file_atomically,
)


def files_in(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def test_storage_key_is_the_generated_certificate_id() -> None:
    certificate_id = uuid.UUID("7e524b50-7651-49c0-a997-aedcaa1c434d")

    assert certificate_storage_key(certificate_id) == "7e524b50-7651-49c0-a997-aedcaa1c434d.pdf"


def test_write_creates_the_file_with_exact_bytes(tmp_path: Path) -> None:
    path = write_file_atomically(tmp_path, "a.pdf", b"%PDF-1 first")

    assert path == tmp_path.resolve() / "a.pdf"
    assert path.read_bytes() == b"%PDF-1 first"
    assert files_in(tmp_path) == ["a.pdf"]  # no temporary file left behind


def test_write_creates_a_missing_storage_directory(tmp_path: Path) -> None:
    root = tmp_path / "not" / "yet" / "created"

    write_file_atomically(root, "a.pdf", b"data")

    assert (root / "a.pdf").read_bytes() == b"data"


def test_write_replaces_an_existing_file(tmp_path: Path) -> None:
    write_file_atomically(tmp_path, "a.pdf", b"old")

    write_file_atomically(tmp_path, "a.pdf", b"new")

    assert (tmp_path / "a.pdf").read_bytes() == b"new"
    assert files_in(tmp_path) == ["a.pdf"]


def test_temporary_file_is_written_next_to_the_target_then_renamed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renames: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def recording_replace(source: str, destination: Path) -> None:
        renames.append((Path(source), Path(destination)))
        assert Path(source).read_bytes() == b"complete"  # fully written before the rename
        real_replace(source, destination)

    monkeypatch.setattr(storage.os, "replace", recording_replace)

    write_file_atomically(tmp_path, "a.pdf", b"complete")

    [(source, destination)] = renames
    assert source.parent == destination.parent  # same directory, so same filesystem
    assert source.name.startswith(".a.pdf.") and source.name.endswith(".tmp")


def test_failed_write_keeps_the_previous_file_and_removes_the_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_file_atomically(tmp_path, "a.pdf", b"previous")

    def failing_replace(source: str, destination: Path) -> None:
        raise OSError("simulated failure during rename")

    monkeypatch.setattr(storage.os, "replace", failing_replace)

    with pytest.raises(OSError, match="simulated failure"):
        write_file_atomically(tmp_path, "a.pdf", b"replacement")

    assert (tmp_path / "a.pdf").read_bytes() == b"previous"
    assert files_in(tmp_path) == ["a.pdf"]


def test_failed_first_write_leaves_no_file_at_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_fsync(descriptor: int) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(storage.os, "fsync", failing_fsync)

    with pytest.raises(OSError, match="simulated disk failure"):
        write_file_atomically(tmp_path, "a.pdf", b"data")

    assert files_in(tmp_path) == []


@pytest.mark.parametrize("key", ["", ".", "../outside.pdf", "nested/../../outside.pdf", "/etc/passwd"])
def test_keys_outside_the_storage_root_are_rejected(tmp_path: Path, key: str) -> None:
    with pytest.raises(InvalidStorageKeyError):
        resolve_storage_path(tmp_path, key)


def test_rendered_certificate_round_trips_through_storage(tmp_path: Path) -> None:
    certificate_id = uuid.uuid4()
    data = CertificateData(certificate_id, "Asha Sharma", "Backend Development Workshop", "ABC Academy", date(2026, 10, 7))

    path = write_file_atomically(tmp_path, certificate_storage_key(certificate_id), render_certificate(data))

    assert path.name == f"{certificate_id}.pdf"
    assert "Asha Sharma" in PdfReader(io.BytesIO(path.read_bytes())).pages[0].extract_text()
