#!/usr/bin/env bash
set -euo pipefail

# ------------------------------------------------------------------------------
# Simplified Cloud Run deployment script for the podcast service.
# Mirrors the lightweight style used by other services: one build + one deploy.
# Configure behaviour through environment variables rather than editing logic.
# ------------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# Optional: load local env file (useful for local deploy)
# Note: keeping real secrets in git is NOT recommended; prefer Secret Manager on Cloud Run.
if [[ -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-podcast-service}"
REPOSITORY="${REPOSITORY:-podcast-service-images}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${SERVICE_NAME}:${IMAGE_TAG}"
# Bucket for storing scripts/audio (already created in your project).
GCS_BUCKET_NAME="${GCS_BUCKET_NAME:-podcast-service-bucket}"
OPENAI_API_KEY_SECRET="${OPENAI_API_KEY_SECRET:-openai-api-key}"
OPENAI_API_KEY_SECRET_VERSION="${OPENAI_API_KEY_SECRET_VERSION:-latest}"

echo "[Deploy] project=${PROJECT_ID} region=${REGION} service=${SERVICE_NAME}"
echo "[Deploy] repository=${REPOSITORY} image=${IMAGE}"

if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "(unset)" ]]; then
  echo "ERROR: PROJECT_ID 未设置。使用 'gcloud config set project <id>' 或导出 PROJECT_ID 环境变量。"
  exit 1
fi

REQUIRED_FILES=(Dockerfile main.py requirements.txt)
for file in "${REQUIRED_FILES[@]}"; do
  if [[ ! -f "${file}" ]]; then
    echo "ERROR: 必需文件缺失：${file}"
    exit 1
  fi
done

gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com iamcredentials.googleapis.com \
  texttospeech.googleapis.com secretmanager.googleapis.com storage.googleapis.com \
  --project "${PROJECT_ID}"

if ! gcloud artifacts repositories describe "${REPOSITORY}" \
  --location "${REGION}" \
  --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "[Deploy] Artifact Registry 仓库不存在，正在创建…"
  gcloud artifacts repositories create "${REPOSITORY}" \
    --repository-format=docker \
    --location "${REGION}" \
    --description="Docker images for ${SERVICE_NAME}" \
    --project "${PROJECT_ID}"
fi

echo "[Deploy] 使用 Cloud Build 构建镜像…"
gcloud builds submit \
  --project "${PROJECT_ID}" \
  --tag "${IMAGE}"

# ------------------------------------------------------------------------------
# Cloud Run 环境变量 / Secret 注入
# - 推荐：使用 Secret Manager 注入 OPENAI_API_KEY（不会出现在命令行历史里）
# - 本地快速验证：也可直接导出 OPENAI_API_KEY 环境变量（不推荐在脚本里写死）
# ------------------------------------------------------------------------------
ENV_VARS=(
  "GOOGLE_CLOUD_PROJECT=${PROJECT_ID}"
  "GCS_BUCKET_NAME=${GCS_BUCKET_NAME}"
)

SECRETS=()
if [[ -n "${OPENAI_API_KEY:-}" ]]; then
  ENV_VARS+=("OPENAI_API_KEY=${OPENAI_API_KEY}")
else
  SECRETS+=("OPENAI_API_KEY=${OPENAI_API_KEY_SECRET}:${OPENAI_API_KEY_SECRET_VERSION}")
fi

join_by_comma() {
  local IFS=","
  echo "$*"
}

ENV_VARS_STR=$(join_by_comma "${ENV_VARS[@]}")
SECRETS_STR=""
if declare -p SECRETS >/dev/null 2>&1 && [[ ${#SECRETS[@]} -gt 0 ]]; then
  SECRETS_STR=$(join_by_comma "${SECRETS[@]}")
fi

DEPLOY_ARGS=(
  gcloud run deploy "${SERVICE_NAME}"
  --project "${PROJECT_ID}"
  --region "${REGION}"
  --image "${IMAGE}"
  --allow-unauthenticated
  --port 8080
  --cpu 1
  --memory 1Gi
  --timeout 300
  --concurrency 80
)

if [[ -n "${ENV_VARS_STR}" ]]; then
  DEPLOY_ARGS+=(--set-env-vars "${ENV_VARS_STR}")
fi

if [[ -n "${SECRETS_STR}" ]]; then
  DEPLOY_ARGS+=(--set-secrets "${SECRETS_STR}")
fi

if [[ -n "${SERVICE_ACCOUNT:-}" ]]; then
  DEPLOY_ARGS+=(--service-account "${SERVICE_ACCOUNT}")
fi

echo "[Deploy] 部署到 Cloud Run…"
"${DEPLOY_ARGS[@]}"

SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --platform managed \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --format='value(status.url)')

echo "✅ 部署成功：${SERVICE_NAME}"
echo "🌐 访问地址：${SERVICE_URL}"
