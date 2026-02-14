#!/bin/bash

# 这是一个用于部署 financial-engine 服务到 Google Cloud Run 的脚本。

# 设置变量 (可选，但推荐，方便修改)
SERVICE_NAME="financial-engine"
REGION="us-central1"
PORT="8080"
CPU_LIMIT="1"
MEMORY_LIMIT="512Mi"
MIN_INSTANCES="0"
MAX_INSTANCES="1"
GCS_BUCKET_NAME="financial-service-bucket" # 替换为你的实际GCS桶名称
ENGINE_TZ="America/Los_Angeles"
# 交易服务地址（用于获取 next earnings day，决定财报是否需要刷新）
# 示例: https://trading-data-engine-xxxxx-uc.a.run.app
TRADING_DATA_ENGINE_URL="${TRADING_DATA_ENGINE_URL:-https://trading-data-engine-805008808538.us-central1.run.app}"
FINANCIAL_L1_HIT_TTL_SECONDS="${FINANCIAL_L1_HIT_TTL_SECONDS:-600}"
FINANCIAL_L1_MISS_TTL_SECONDS="${FINANCIAL_L1_MISS_TTL_SECONDS:-120}"
FINANCIAL_NO_EARNINGS_MAX_STALENESS_DAYS="${FINANCIAL_NO_EARNINGS_MAX_STALENESS_DAYS:-3}"

echo "--- 开始部署 Google Cloud Run 服务: ${SERVICE_NAME} ---"
echo "区域: ${REGION}"
echo "GCS 桶: ${GCS_BUCKET_NAME}"
echo "时区: ${ENGINE_TZ}"
echo "Trading 服务: ${TRADING_DATA_ENGINE_URL:-<not-set>}"

# 执行 gcloud run deploy 命令
# --source . 表示从当前目录的源代码部署，Cloud Build 会自动触发
gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --region "${REGION}" \
  --platform managed \
  --allow-unauthenticated \
  --port "${PORT}" \
  --cpu "${CPU_LIMIT}" \
  --memory "${MEMORY_LIMIT}" \
  --min-instances "${MIN_INSTANCES}" \
  --max-instances "${MAX_INSTANCES}" \
  --set-env-vars PYTHONUNBUFFERED=1,GCS_BUCKET_NAME="${GCS_BUCKET_NAME}",ENGINE_TZ="${ENGINE_TZ}",TRADING_DATA_ENGINE_URL="${TRADING_DATA_ENGINE_URL}",FINANCIAL_L1_HIT_TTL_SECONDS="${FINANCIAL_L1_HIT_TTL_SECONDS}",FINANCIAL_L1_MISS_TTL_SECONDS="${FINANCIAL_L1_MISS_TTL_SECONDS}",FINANCIAL_NO_EARNINGS_MAX_STALENESS_DAYS="${FINANCIAL_NO_EARNINGS_MAX_STALENESS_DAYS}" \
  --project "$(gcloud config get-value project)" # 自动获取当前gcloud配置的项目ID

echo "--- ✅ 部署成功。请检查终端输出获取部署状态。 ---"
