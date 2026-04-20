#!/bin/bash

# ==============================================================================
# Google Cloud Run 部署脚本 - Heatmap Service
# ==============================================================================

if [ -z "${BASH_VERSION:-}" ]; then
  exec /bin/bash "$0" "$@"
fi

set -euo pipefail

# 与仓库内其他 service 保持一致：
# 优先级：环境变量 PROJECT_ID > 当前 gcloud 默认项目。
# 不需要每次命令行传参。
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
SERVICE_NAME="${SERVICE_NAME:-heatmap-service}"
REGION="${REGION:-us-central1}"
GCS_BUCKET_NAME="${GCS_BUCKET_NAME:-stockflow-heatmap-bucket}"
HEATMAP_GCS_PREFIX="${HEATMAP_GCS_PREFIX:-heatmap/snapshots}"
HEATMAP_CACHE_TTL_SECONDS="${HEATMAP_CACHE_TTL_SECONDS:-300}"
HEATMAP_QUOTE_TIMEOUT_SECONDS="${HEATMAP_QUOTE_TIMEOUT_SECONDS:-180}"
HEATMAP_DEFAULT_MARKET="${HEATMAP_DEFAULT_MARKET:-hk}"
HEATMAP_WRITE_HISTORY="${HEATMAP_WRITE_HISTORY:-1}"
HEATMAP_MARKETS_CONFIG_BLOB="${HEATMAP_MARKETS_CONFIG_BLOB:-}"
HEATMAP_CRON_TOKEN="${HEATMAP_CRON_TOKEN:-13ca1a26a3b842c409820331638cc05ebc561c9ca2165c4e9ece09f0c7fd999f}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-}"
BUILD_SERVICE_ACCOUNT="${BUILD_SERVICE_ACCOUNT:-}"

if [ -z "${PROJECT_ID}" ] || [ "${PROJECT_ID}" = "(unset)" ]; then
  echo "错误: 未检测到 GCP 项目 ID。"
  echo "请先执行: gcloud config set project <PROJECT_ID>"
  echo "或临时执行: PROJECT_ID=<PROJECT_ID> ./deploy_heatmap_service.sh"
  exit 1
fi

echo "--- 部署 Heatmap Service 到 Cloud Run ---"
echo "项目: ${PROJECT_ID}"
echo "服务: ${SERVICE_NAME}"
echo "区域: ${REGION}"
echo "GCS Bucket: ${GCS_BUCKET_NAME}"
echo "Quote Timeout: ${HEATMAP_QUOTE_TIMEOUT_SECONDS}s"

SERVICE_ACCOUNT_ARG=""
if [ -n "${SERVICE_ACCOUNT}" ]; then
  SERVICE_ACCOUNT_ARG="--service-account=${SERVICE_ACCOUNT}"
fi

BUILD_SERVICE_ACCOUNT_ARG=""
if [ -n "${BUILD_SERVICE_ACCOUNT}" ]; then
  BUILD_SERVICE_ACCOUNT_ARG="--build-service-account=${BUILD_SERVICE_ACCOUNT}"
fi

ENV_VARS=(
  "PYTHONUNBUFFERED=1"
  "SERVICE_NAME=${SERVICE_NAME}"
  "GCS_BUCKET_NAME=${GCS_BUCKET_NAME}"
  "HEATMAP_GCS_PREFIX=${HEATMAP_GCS_PREFIX}"
  "HEATMAP_CACHE_TTL_SECONDS=${HEATMAP_CACHE_TTL_SECONDS}"
  "HEATMAP_QUOTE_TIMEOUT_SECONDS=${HEATMAP_QUOTE_TIMEOUT_SECONDS}"
  "HEATMAP_DEFAULT_MARKET=${HEATMAP_DEFAULT_MARKET}"
  "HEATMAP_WRITE_HISTORY=${HEATMAP_WRITE_HISTORY}"
)

if [ -n "${HEATMAP_MARKETS_CONFIG_BLOB}" ]; then
  ENV_VARS+=("HEATMAP_MARKETS_CONFIG_BLOB=${HEATMAP_MARKETS_CONFIG_BLOB}")
fi

if [ -n "${HEATMAP_CRON_TOKEN}" ]; then
  ENV_VARS+=("HEATMAP_CRON_TOKEN=${HEATMAP_CRON_TOKEN}")
fi

IFS=,
ENV_JOINED="${ENV_VARS[*]}"
unset IFS

gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --region "${REGION}" \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --cpu 1 \
  --memory 512Mi \
  --min-instances 0 \
  --max-instances 2 \
  --timeout 600s \
  ${SERVICE_ACCOUNT_ARG} \
  ${BUILD_SERVICE_ACCOUNT_ARG} \
  --set-env-vars "${ENV_JOINED}" \
  --project "${PROJECT_ID}"

echo ""
echo "✅ 部署完成"
SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --region "${REGION}" --format='value(status.url)' --project "${PROJECT_ID}" 2>/dev/null || true)"
if [ -n "${SERVICE_URL}" ]; then
  echo "服务 URL: ${SERVICE_URL}"
  echo "建议 Scheduler 调用: ${SERVICE_URL}/v1/heatmap/refresh_all"
fi
