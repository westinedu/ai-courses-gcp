#!/usr/bin/env bash
#
# deploy_news_crawler_v2.sh - 更稳健的 Cloud Run 部署脚本（使用 --env-vars-file）
# 用法： bash deploy_news_crawler_v2.sh
set -euo pipefail

# === 基本配置（按需修改） ======================================================
SERVICE_NAME="${SERVICE_NAME:-news-crawler-agent}"
GCP_REGION="${GCP_REGION:-us-central1}"

# 存储（GCS 或 local）
STORAGE_BACKEND="${STORAGE_BACKEND:-gcs}"
GCS_BUCKET_NAME="${GCS_BUCKET_NAME:-news-raw-data-bucket}"
GCS_BASE_PREFIX="${GCS_BASE_PREFIX:-raw-news}"
TOPIC_CONFIG_GCS_BLOB="${TOPIC_CONFIG_GCS_BLOB:-config/topic_configs.json}"  # 当 STORAGE_BACKEND=gcs 时可设置 
LOCAL_STORAGE_ROOT="${LOCAL_STORAGE_ROOT:-}"  # 当 STORAGE_BACKEND=local 时可设置

# 应用变量（与 settings.py 对齐）
DEFAULT_TICKERS="${DEFAULT_TICKERS:-AAPL,MSFT,GOOGL,AMZN,META,TSLA,NVDA,AMD,JPM,V,BRK-B,WMT,COST,KO,NKE,LLY,UNH,CAT,DIS,NFLX}"
MAX_ARTICLES_PER_TICKER="${MAX_ARTICLES_PER_TICKER:-50}"
HTTP_TIMEOUT="${HTTP_TIMEOUT:-15}"
USER_AGENT="${USER_AGENT:-news-crawler-agent/0.1 (+https://example.com)}"
ENABLE_GOOGLE_NEWS="${ENABLE_GOOGLE_NEWS:-1}"
ENABLE_YAHOO_FINANCE="${ENABLE_YAHOO_FINANCE:-1}"
TIMEZONE="${TIMEZONE:-Asia/Shanghai}"

# 资源与伸缩
CPU_LIMIT="${CPU_LIMIT:-1}"
MEMORY_LIMIT="${MEMORY_LIMIT:-1Gi}"
MIN_INSTANCES="${MIN_INSTANCES:-0}"
MAX_INSTANCES="${MAX_INSTANCES:-2}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-900}"
SERVICE_ACCOUNT_EMAIL="${SERVICE_ACCOUNT_EMAIL:-}"  # 可选

PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
if [[ -z "${PROJECT_ID}" ]]; then
  echo "未检测到 gcloud 默认项目，请先执行：gcloud config set project <PROJECT_ID>"
  exit 1
fi

echo "------------------------------------------------------------------"
echo " 开始部署 Cloud Run 服务：${SERVICE_NAME}"
echo " 项目：${PROJECT_ID}"
echo " 区域：${GCP_REGION}"
echo " 存储：${STORAGE_BACKEND}"
echo "  GCS_BUCKET_NAME=${GCS_BUCKET_NAME}"
echo "  GCS_BASE_PREFIX=${GCS_BASE_PREFIX}"
echo "  LOCAL_STORAGE_ROOT=${LOCAL_STORAGE_ROOT}"
echo " DEFAULT_TICKERS=${DEFAULT_TICKERS}"
echo "------------------------------------------------------------------"

# 生成临时 env 文件（YAML），避免 --set-env-vars 的逗号/空格转义问题
TMP_ENV="$(mktemp)"
cat > "$TMP_ENV" <<EOF_ENV
GOOGLE_CLOUD_PROJECT: "${PROJECT_ID}"
GCP_REGION: "${GCP_REGION}"
STORAGE_BACKEND: "${STORAGE_BACKEND}"
GCS_BUCKET_NAME: "${GCS_BUCKET_NAME}"
GCS_BASE_PREFIX: "${GCS_BASE_PREFIX}"
TOPIC_CONFIG_GCS_BLOB: "${TOPIC_CONFIG_GCS_BLOB}"
LOCAL_STORAGE_ROOT: "${LOCAL_STORAGE_ROOT}"
DEFAULT_TICKERS: "${DEFAULT_TICKERS}"
MAX_ARTICLES_PER_TICKER: "${MAX_ARTICLES_PER_TICKER}"
HTTP_TIMEOUT: "${HTTP_TIMEOUT}"
USER_AGENT: "${USER_AGENT}"
ENABLE_GOOGLE_NEWS: "${ENABLE_GOOGLE_NEWS}"
ENABLE_YAHOO_FINANCE: "${ENABLE_YAHOO_FINANCE}"
TIMEZONE: "${TIMEZONE}"
EOF_ENV

# 组装 gcloud 参数
GCLOUD_ARGS=(
  run deploy "${SERVICE_NAME}"
  --source .
  --region "${GCP_REGION}"
  --platform managed
  --allow-unauthenticated
  --cpu "${CPU_LIMIT}"
  --memory "${MEMORY_LIMIT}"
  --min-instances "${MIN_INSTANCES}"
  --max-instances "${MAX_INSTANCES}"
  --timeout "${TIMEOUT_SECONDS}"
  --env-vars-file "$TMP_ENV"
  --project "${PROJECT_ID}"
)

# 可选：绑定服务账号
if [[ -n "${SERVICE_ACCOUNT_EMAIL}" ]]; then
  GCLOUD_ARGS+=( --service-account "${SERVICE_ACCOUNT_EMAIL}" )
fi

# 执行部署
gcloud "${GCLOUD_ARGS[@]}"

# 输出 URL 并给出验证命令
SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --region="${GCP_REGION}" --project="${PROJECT_ID}" --format='value(status.url)')"
echo "------------------------------------------------------------------"
echo " 部署完成 ✅"
echo " 服务 URL: ${SERVICE_URL}"


# 清理临时文件
rm -f "$TMP_ENV"


