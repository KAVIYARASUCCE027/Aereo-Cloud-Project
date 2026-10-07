"""Submit one generated batch and measure how long background processing takes.

Usage (standard library only, so it runs on the host):
    python scripts/benchmark.py [--api http://localhost:8000] [--recipients 100]

Prints the measured times and outcome counts; it makes no throughput claims
beyond this one run.
"""

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime

TERMINAL = {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"}


def call(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--recipients", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()

    payload = {
        "event_name": "Benchmark Workshop",
        "issuer_name": "ABC Academy",
        "issue_date": "2026-10-07",
        "recipients": [
            {"name": f"Benchmark Recipient {i:04d}", "email": f"recipient{i}@example.com"}
            for i in range(args.recipients)
        ],
    }

    submitted_at = time.monotonic()
    status_code, created = call("POST", f"{args.api}/api/v1/jobs", payload)
    accepted_after = time.monotonic() - submitted_at
    if status_code != 202:
        print(f"submission failed: HTTP {status_code} {created}", file=sys.stderr)
        return 1
    job_id = created["job_id"]

    while True:
        _, job = call("GET", f"{args.api}/api/v1/jobs/{job_id}")
        if job["status"] in TERMINAL:
            break
        if time.monotonic() - submitted_at > args.timeout:
            print(f"job {job_id} not finished after {args.timeout}s: {job}", file=sys.stderr)
            return 1
        time.sleep(0.25)
    observed_total = time.monotonic() - submitted_at

    processing = (datetime.fromisoformat(job["completed_at"]) - datetime.fromisoformat(job["started_at"])).total_seconds()
    print(f"job_id:                    {job_id}")
    print(f"recipients:                {job['total_count']}")
    print(f"status:                    {job['status']}")
    print(f"success / failed / invalid {job['success_count']} / {job['failed_count']} / {job['invalid_count']}")
    print(f"202 Accepted after:        {accepted_after:.2f} s")
    print(f"worker processing time:    {processing:.2f} s (started_at -> completed_at)")
    print(f"per certificate:           {processing / job['total_count'] * 1000:.1f} ms")
    print(f"submit -> observed done:   {observed_total:.2f} s (includes queueing and 0.25 s polling)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
