import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.urls import CERTIFICATES_PREFIX
from app.config import get_settings
from app.db import get_db
from app.schemas import CertificateNotAvailableResponse, ErrorResponse
from app.services import results

router = APIRouter(prefix=CERTIFICATES_PREFIX, tags=["certificates"])


def get_storage_root() -> Path:
    """Dependency, so tests can point downloads at temporary storage."""
    return get_settings().certificate_storage_dir


@router.get(
    "/{certificate_id}/download",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {"content": {"application/pdf": {}}, "description": "The certificate PDF."},
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "No such certificate (`certificate_not_found`)."},
        status.HTTP_409_CONFLICT: {
            "model": CertificateNotAvailableResponse,
            "description": (
                "`certificate_not_ready`: still PENDING or PROCESSING. "
                "`certificate_not_available`: INVALID or FAILED; there will be no PDF."
            ),
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorResponse,
            "description": "`certificate_file_missing`: recorded as SUCCESS, but the stored file cannot be read.",
        },
    },
)
def download_certificate(
    certificate_id: uuid.UUID,
    db: Annotated[Session, Depends(get_db)],
    storage_root: Annotated[Path, Depends(get_storage_root)],
) -> Response:
    """Download the PDF of a successfully generated certificate (the recipient ID)."""
    try:
        certificate = results.read_certificate(db, certificate_id, storage_root)
    except results.CertificateNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "certificate_not_found", "message": f"Certificate {certificate_id} does not exist."},
        ) from exc
    except results.CertificateNotAvailableError as exc:
        if exc.may_become_available:
            code, message = "certificate_not_ready", "The certificate has not been generated yet."
        else:
            code, message = "certificate_not_available", "No certificate was generated for this recipient."
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": code, "message": message, "status": exc.status.value},
        ) from exc
    except results.CertificateFileMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "certificate_file_missing", "message": "The certificate file is unexpectedly missing."},
        ) from exc

    return Response(
        content=certificate.content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{certificate.filename}"'},
    )
