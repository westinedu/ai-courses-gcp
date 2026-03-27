#!/bin/bash

if [ -z "${BASH_VERSION:-}" ]; then
  exec /bin/bash "$0" "$@"
fi

set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'EOF'
Configure Cloud Scheduler job for Heatmap refresh.

Environment overrides:
  PROJECT_ID          (default: gcloud current project)
  REGION              (default: us-central1, for Cloud Run service lookup)
  SCHEDULER_REGION    (default: REGION)
  SERVICE_NAME        (default: heatmap-service)
  HEATMAP_SERVICE_URL (default: auto-discover from Cloud Run service URL)
  JOB_NAME            (default: heatmap-refresh-hk)
  SCHEDULE            (default: */5 9-16 * * 1-5)
  TIME_ZONE           (default: Asia/Hong_Kong)
  MARKETS_CSV         (default: hk) e.g. hk,ks
  HEATMAP_CRON_TOKEN  (optional)
  ENABLE_APIS         (default: 1) enable cloudscheduler API automatically
  RUN_NOW             (default: 0) set 1 to trigger one immediate run
EOF
  exit 0
fi

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
SCHEDULER_REGION="${SCHEDULER_REGION:-${REGION}}"
SERVICE_NAME="${SERVICE_NAME:-heatmap-service}"
SERVICE_URL="${HEATMAP_SERVICE_URL:-https://heatmap-service-805008808538.us-central1.run.app}"
JOB_NAME="${JOB_NAME:-heatmap-refresh-hk}"
SCHEDULE="${SCHEDULE:-*/5 9-16 * * 1-5}"
TIME_ZONE="${TIME_ZONE:-Asia/Hong_Kong}"
MARKETS_CSV="${MARKETS_CSV:-hk,tw}"
HEATMAP_CRON_TOKEN="${HEATMAP_CRON_TOKEN:-13ca1a26a3b842c409820331638cc05ebc561c9ca2165c4e9ece09f0c7fd999f}"
ENABLE_APIS="${ENABLE_APIS:-1}"
RUN_NOW="${RUN_NOW:-0}"

if [ -z "${PROJECT_ID}" ] || [ "${PROJECT_ID}" = "(unset)" ]; then
  echo "Error: no GCP project found."
  echo "Run: gcloud config set project <PROJECT_ID>"
  echo "Or:  PROJECT_ID=<PROJECT_ID> ./configure_heatmap_scheduler.sh"
  exit 1
fi

if [ "${ENABLE_APIS}" = "1" ]; then
  echo "Enabling Cloud Scheduler API in project ${PROJECT_ID} ..."
  gcloud services enable cloudscheduler.googleapis.com --project "${PROJECT_ID}" >/dev/null
fi

if [ -z "${SERVICE_URL}" ]; then
  SERVICE_URL="$(
    gcloud run services describe "${SERVICE_NAME}" \
      --region "${REGION}" \
      --project "${PROJECT_ID}" \
      --format='value(status.url)' 2>/dev/null || true
  )"
fi
SERVICE_URL="${SERVICE_URL%/}"

if [ -z "${SERVICE_URL}" ]; then
  echo "Error: failed to resolve service URL."
  echo "Deploy service first, or set HEATMAP_SERVICE_URL explicitly."
  exit 1
fi

declare -a MARKET_ITEMS=()
IFS=',' read -r -a RAW_MARKETS <<< "${MARKETS_CSV}"
for raw in "${RAW_MARKETS[@]}"; do
  market="${raw//[[:space:]]/}"
  market="$(printf '%s' "${market}" | tr '[:upper:]' '[:lower:]')"
  if [ -z "${market}" ]; then
    continue
  fi
  if [[ ! "${market}" =~ ^[a-z0-9_-]{2,16}$ ]]; then
    echo "Error: invalid market '${market}' in MARKETS_CSV=${MARKETS_CSV}"
    exit 1
  fi
  MARKET_ITEMS+=("\"${market}\"")
done

if [ "${#MARKET_ITEMS[@]}" -eq 0 ]; then
  echo "Error: no valid markets found in MARKETS_CSV=${MARKETS_CSV}"
  exit 1
fi

MARKETS_JSON="$(IFS=,; echo "${MARKET_ITEMS[*]}")"
MESSAGE_BODY="{\"markets\":[${MARKETS_JSON}]}"

HEADERS="Content-Type=application/json"
if [ -n "${HEATMAP_CRON_TOKEN}" ]; then
  HEADERS="${HEADERS},x-heatmap-token=${HEATMAP_CRON_TOKEN}"
else
  echo "Warning: HEATMAP_CRON_TOKEN is empty. This is fine only if refresh endpoints are open."
fi

TARGET_URI="${SERVICE_URL}/v1/heatmap/refresh_all"

echo "--- Configure Heatmap Scheduler ---"
echo "Project          : ${PROJECT_ID}"
echo "Scheduler Region : ${SCHEDULER_REGION}"
echo "Cloud Run Region : ${REGION}"
echo "Service Name     : ${SERVICE_NAME}"
echo "Service URL      : ${SERVICE_URL}"
echo "Job Name         : ${JOB_NAME}"
echo "Schedule         : ${SCHEDULE}"
echo "Time Zone        : ${TIME_ZONE}"
echo "Markets          : ${MARKETS_CSV}"
echo "Target URI       : ${TARGET_URI}"

COMMON_ARGS=(
  --project "${PROJECT_ID}"
  --location "${SCHEDULER_REGION}"
  --schedule "${SCHEDULE}"
  --time-zone "${TIME_ZONE}"
  --uri "${TARGET_URI}"
  --http-method POST
  --message-body "${MESSAGE_BODY}"
)

if gcloud scheduler jobs describe "${JOB_NAME}" --project "${PROJECT_ID}" --location "${SCHEDULER_REGION}" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "${JOB_NAME}" "${COMMON_ARGS[@]}" \
    --update-headers "${HEADERS}"
  echo "Updated scheduler job: ${JOB_NAME}"
else
  gcloud scheduler jobs create http "${JOB_NAME}" "${COMMON_ARGS[@]}" \
    --headers "${HEADERS}"
  echo "Created scheduler job: ${JOB_NAME}"
fi

if [ "${RUN_NOW}" = "1" ]; then
  gcloud scheduler jobs run "${JOB_NAME}" --project "${PROJECT_ID}" --location "${SCHEDULER_REGION}"
  echo "Triggered one run: ${JOB_NAME}"
fi

echo "Done."
