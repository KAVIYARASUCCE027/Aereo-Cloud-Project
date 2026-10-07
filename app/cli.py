"""Command-line tools.

    python -m app.cli process-job JOB_ID [--storage-dir DIR]

Runs certificate generation for one job directly, without Celery. Exit codes:
0 processed (or nothing to do), 1 unknown job, 2 processing stopped by an
infrastructure error.
"""

import argparse
import json
import logging
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

from app.config import get_settings
from app.services.generation import JobProcessingError, process_job
from app.services.progress import JobNotFoundError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    process = commands.add_parser("process-job", help="generate certificates for one queued job, without Celery")
    process.add_argument("job_id", type=uuid.UUID)
    process.add_argument("--storage-dir", type=Path, help="default: CERTIFICATE_STORAGE_DIR")
    args = parser.parse_args(argv)

    logging.basicConfig(level=get_settings().log_level)
    try:
        result = process_job(args.job_id, storage_root=args.storage_dir)
    except JobNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except JobProcessingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    progress = result.progress
    print(
        json.dumps(
            {
                "job_id": str(result.job_id),
                "outcome": result.outcome,
                "status": result.status,
                **asdict(progress),
                "processed_count": progress.processed_count,
                "progress_percent": progress.progress_percent,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
