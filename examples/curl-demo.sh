#!/usr/bin/env sh
# Submit a batch, poll until it finishes, list the results, and download the
# first successful certificate. Needs only curl and standard shell tools.
#
# Usage: sh examples/curl-demo.sh [REQUEST_FILE] [API_URL]
#   REQUEST_FILE  default examples/job-request.json
#   API_URL       default http://localhost:8000
set -eu

REQUEST=${1:-examples/job-request.json}
API=${2:-http://localhost:8000}

# Extract a string field from the API's compact JSON (no jq needed).
field() { sed -n "s/.*\"$1\":\"\([^\"]*\)\".*/\1/p"; }
number() { sed -n "s/.*\"$1\":\([0-9.]*\).*/\1/p"; }

echo "1. Submit $REQUEST"
# --fail-with-body: a 422 or 503 exits non-zero but still returns the error body.
RESPONSE=$(curl -sS --fail-with-body -X POST "$API/api/v1/jobs" \
  -H "Content-Type: application/json" --data-binary "@$REQUEST") || {
  echo "   rejected: $RESPONSE"
  exit 1
}
echo "   $RESPONSE"
JOB_ID=$(printf '%s' "$RESPONSE" | field job_id)

echo "2. Poll $API/api/v1/jobs/$JOB_ID"
attempt=0
while :; do
  STATUS_JSON=$(curl -sS --fail-with-body "$API/api/v1/jobs/$JOB_ID")
  STATUS=$(printf '%s' "$STATUS_JSON" | field status)
  echo "   status=$STATUS progress=$(printf '%s' "$STATUS_JSON" | number progress_percent)%"
  case $STATUS in COMPLETED | COMPLETED_WITH_ERRORS | FAILED) break ;; esac
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 120 ]; then echo "   gave up after 120 polls" >&2; exit 1; fi
  sleep 1
done
echo "   $STATUS_JSON"

echo "3. Recipient results"
RESULTS=$(curl -sS --fail-with-body "$API/api/v1/jobs/$JOB_ID/recipients?limit=50&offset=0")
echo "   $RESULTS"

DOWNLOAD=$(printf '%s' "$RESULTS" | grep -o '"download_url":"[^"]*"' | head -n 1 | sed 's/"download_url":"\(.*\)"/\1/' || true)
if [ -z "$DOWNLOAD" ]; then
  echo "4. No successful certificate to download"
  exit 0
fi
echo "4. Download $API$DOWNLOAD"
curl -sS --fail-with-body -o certificate.pdf "$API$DOWNLOAD"
echo "   saved certificate.pdf ($(wc -c < certificate.pdf | tr -d ' ') bytes)"
