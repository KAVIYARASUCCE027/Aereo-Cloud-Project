"""Certificate file storage.

Files live under one root directory (CERTIFICATE_STORAGE_DIR), shared by the
API and worker containers. A file's storage key is derived from its generated
certificate ID, never from user input, and is what the database stores.

Writes are atomic: bytes go to a temporary file in the same directory (so the
same filesystem), are flushed and fsynced, then os.replace() swaps it into
place. A reader sees either the old file, the complete new file, or no file;
never a partial PDF. If a process is killed mid-write, a hidden
`.<name>.*.tmp` file can be left behind; it is never served because it is
not a valid storage key for any certificate.
"""

import os
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path


class InvalidStorageKeyError(ValueError):
    pass


def certificate_storage_key(certificate_id: uuid.UUID) -> str:
    return f"{certificate_id}.pdf"


def resolve_storage_path(root: Path, storage_key: str) -> Path:
    """Return the file path for a key, refusing anything that would escape root."""
    root = root.resolve()
    path = (root / storage_key).resolve()
    if root not in path.parents:
        raise InvalidStorageKeyError(f"storage key {storage_key!r} does not name a file inside the storage root")
    return path


def write_file_atomically(root: Path, storage_key: str, data: bytes) -> Path:
    """Write data to the key's path atomically and return that path."""
    target = resolve_storage_path(root, storage_key)
    target.parent.mkdir(parents=True, exist_ok=True)

    file_descriptor, temp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(file_descriptor, "wb") as temp_file:
            temp_file.write(data)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, target)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise

    _fsync_directory(target.parent)
    return target


def _fsync_directory(directory: Path) -> None:
    """Persist the rename itself. POSIX only; Windows cannot open a directory this way."""
    if os.name != "posix":
        return
    directory_descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
