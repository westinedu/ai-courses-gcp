#!/bin/bash

# --- 配置 ---
# 请将以下变量替换为您自己的值
export PROJECT_ID="able-engine-466308-q2"
export GCP_REGION="us-central1" # e.g., us-central1
export JOB_NAME="batch-vcards-job" # Cloud Run Job 名称
# export SERVICE_ACCOUNT="your-job-runner-sa@${PROJECT_ID}.iam.gserviceaccount.com"
export AR_REPO_NAME="cloud-run-source-deploy" # Artifact Registry 仓库名称

# 资源配置
CPU_LIMIT="1"
MEMORY_LIMIT="512Mi"

# 各个引擎服务的 URL (从 Cloud Run 服务详情页获取)
FINANCIAL_ENGINE_URL="https://financial-engine-355131621961.us-central1.run.app"
TRADING_ENGINE_URL="https://trading-data-engine-355131621961.us-central1.run.app"
NEWS_ENGINE_URL="https://news-crawler-agent-355131621961.us-central1.run.app"
QA_ENGINE_URL="https://qa-engine-355131621961.us-central1.run.app"

# GCS 配置
GCS_BUCKET_NAME="vcards-meta-data"
TICKER_LIST_PATH="batch_config/ticker_list.json"
CARD_TYPES_PATH="batch_config/card_types.json"
LLM_CONFIG_PATH="batch_config/llm_config.json"
ENGINE_CONTROL_PATH="batch_config/engine_control.json" # <--- 新增

# --- 脚本开始 ---
echo "------------------------------------------------------------------"
echo " 部署 Cloud Run 作业：${JOB_NAME}"
echo " 项目：${PROJECT_ID}"
echo " 区域：${GCP_REGION}"
echo "------------------------------------------------------------------"
gcloud config set project ${PROJECT_ID}

# 1. 确保 Artifact Registry 仓库存在
if ! gcloud artifacts repositories describe ${AR_REPO_NAME} --location=${GCP_REGION} &> /dev/null; then
  echo "Artifact Registry 仓库 '${AR_REPO_NAME}' 不存在，正在创建..."
  gcloud artifacts repositories create ${AR_REPO_NAME} \
    --repository-format=docker \
    --location=${GCP_REGION} \
    --description="Application container images"
else
  echo "Artifact Registry 仓库 '${AR_REPO_NAME}' 已存在。"
fi

# 2. 构建 Docker 镜像并推送到 Artifact Registry
IMAGE_URI="${GCP_REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO_NAME}/${JOB_NAME}:latest"
echo "正在构建并推送镜像: ${IMAGE_URI}"
gcloud builds submit . --tag ${IMAGE_URI}

# 3. 生成临时的环境变量 YAML 文件 (最佳实践)
TMP_ENV="$(mktemp)"
cat > "$TMP_ENV" <<EOF
GCS_BUCKET_NAME: "${GCS_BUCKET_NAME}"
TICKER_LIST_PATH: "${TICKER_LIST_PATH}"
CARD_TYPES_PATH: "${CARD_TYPES_PATH}"
LLM_CONFIG_PATH: "${LLM_CONFIG_PATH}"
ENGINE_CONTROL_PATH: "${ENGINE_CONTROL_PATH}" # <--- 新增
FINANCIAL_ENGINE_URL: "${FINANCIAL_ENGINE_URL}"
TRADING_ENGINE_URL: "${TRADING_ENGINE_URL}"
NEWS_ENGINE_URL: "${NEWS_ENGINE_URL}"
QA_ENGINE_URL: "${QA_ENGINE_URL}"
EOF

echo "[env] 环境变量已写入临时文件: $TMP_ENV"

# 4. 部署或更新 Cloud Run Job
echo "正在部署 Cloud Run Job: ${JOB_NAME}..."
gcloud run jobs deploy ${JOB_NAME} \
  --image "${IMAGE_URI}" \
  --region "${GCP_REGION}" \
  --env-vars-file "$TMP_ENV" \
  --tasks 1 \
  --max-retries 1 \
  --cpu "${CPU_LIMIT}" \
  --memory "${MEMORY_LIMIT}" \
  --task-timeout "3600" # Job 单次执行的最长超时时间，单位秒（这里是1小时）

# 5. 清理临时文件
rm -f "$TMP_ENV"

echo "------------------------------------------------------------------"
echo " ✅ 部署成功：${JOB_NAME}"
echo " 要立即执行 Job，请运行:"
echo " gcloud run jobs execute ${JOB_NAME} --region ${GCP_REGION}"
echo "------------------------------------------------------------------"