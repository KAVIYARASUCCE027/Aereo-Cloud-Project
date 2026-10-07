"""Render sample certificates for manual inspection.

Usage (from the project root): python -m scripts.render_sample_certificates OUTPUT_DIR
"""

import sys
import uuid
from datetime import date
from pathlib import Path

from app.services.pdf import CertificateData, render_certificate
from app.services.storage import write_file_atomically

SAMPLES = {
    "ordinary-name": CertificateData(
        certificate_id=uuid.UUID("7e524b50-7651-49c0-a997-aedcaa1c434d"),
        recipient_name="Asha Sharma",
        event_name="Backend Development Workshop",
        issuer_name="ABC Academy",
        issue_date=date(2026, 10, 7),
    ),
    "long-text": CertificateData(
        certificate_id=uuid.UUID("1b4e28ba-2fa1-11d2-883f-0016d3cca427"),
        # 150 characters: the longest name validation accepts.
        recipient_name=(
            "Maximiliana Alexandrina Wolfeschlegelsteinhausenbergerdorff "
            "de la Santísima Trinidad Montgomery-Fitzgerald Okonkwo-Vanderbilt Adebayo-Smithson-Lee III"
        ),
        event_name=(
            "Advanced Distributed Systems Engineering Bootcamp: Designing Reliable, "
            "Observable and Fault-Tolerant Backend Services with Python, PostgreSQL, "
            "Message Queues and Container Orchestration"
        ),
        issuer_name="The International Academy for Continuing Professional Education in Software Engineering",
        issue_date=date(2026, 9, 30),
    ),
    "with-achievement": CertificateData(
        certificate_id=uuid.UUID("5d0f7a3e-9c1b-4e8a-b2f4-3a6d9e1c7b20"),
        recipient_name="Kavi",
        event_name="Cloud Computing Bootcamp",
        issuer_name="ABC Academy",
        issue_date=date(2026, 10, 7),
        achievement="Outstanding Performance in Cloud Computing",
    ),
    "accented-characters": CertificateData(
        certificate_id=uuid.UUID("c9bf9e57-1685-4c89-bafb-ff5af830be8a"),
        recipient_name="Zoë Ångström-Łukasiewicz",
        event_name="Atelier « Données & Qualité » — Kraków / Αθήνα / Москва",
        issuer_name="École Supérieure d’Informatique Málaga",
        issue_date=date(2026, 1, 15),
    ),
}


def main(output_dir: Path) -> None:
    for name, data in SAMPLES.items():
        path = write_file_atomically(output_dir, f"{name}.pdf", render_certificate(data))
        print(path)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]))
