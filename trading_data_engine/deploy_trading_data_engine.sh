#!/bin/bash

# ==============================================================================
# Google Cloud Run 部署脚本 - Trading Data Engine
#
# 该脚本用于将 Trading Data Engine 服务部署到 Google Cloud Run。
# 它会自动从当前目录（包含 Dockerfile 和您的代码）构建 Docker 镜像，
# 并将其部署为一个无服务器容器。
# ==============================================================================

# ------------------------------------------------------------------------------
# 配置变量
# 请根据您的需求修改以下变量。
# ------------------------------------------------------------------------------

# 您的 Google Cloud 项目 ID
# 优先级：脚本第1个参数 > 环境变量 PROJECT_ID > 当前 gcloud 配置
YOUR_PROJECT_ID="${1:-${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}}"

# Cloud Run 服务名称
SERVICE_NAME="${SERVICE_NAME:-trading-data-engine}"

# Cloud Run 部署区域
# 推荐选择离用户或数据源较近的区域，例如 us-central1, asia-east1 等。
REGION="${REGION:-us-central1}"

# GCS 存储桶名称，用于持久化历史交易数据。
# 请确保此存储桶已存在，并且您的 Cloud Run 服务账户具有写入权限。
GCS_BUCKET_NAME="${GCS_BUCKET_NAME:-stockflow-trading-data-bucket}"

# 可选：指定 Cloud Run 服务运行的服务账号（推荐用于生产环境最小权限）
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-}"

# 可选：指定 Cloud Build 的构建服务账号（修复默认构建账号权限不足导致的 PERMISSION_DENIED）
# 推荐使用：<PROJECT_NUMBER>@cloudbuild.gserviceaccount.com
BUILD_SERVICE_ACCOUNT="${BUILD_SERVICE_ACCOUNT:-}"

# ------------------------------------------------------------------------------
# 脚本执行
# ------------------------------------------------------------------------------

echo "--- 正在检查 Google Cloud 项目配置 ---"
if [ -z "${YOUR_PROJECT_ID}" ] || [ "${YOUR_PROJECT_ID}" = "(unset)" ]; then
  echo "错误: 未检测到当前gcloud配置的项目ID。"
  echo "请运行 'gcloud auth login' 后执行 'gcloud config set project YOUR_PROJECT_ID'，或直接运行："
  echo "  PROJECT_ID=YOUR_PROJECT_ID ./deploy_trading_data_engine.sh"
  echo "  ./deploy_trading_data_engine.sh YOUR_PROJECT_ID"
  exit 1
fi
echo "Google Cloud 项目已自动设置为: ${YOUR_PROJECT_ID}"
echo ""

echo "--- 正在部署 ${SERVICE_NAME} 到 Google Cloud Run ---"
echo "部署区域: ${REGION}"
echo "GCS 存储桶: ${GCS_BUCKET_NAME}"
echo ""

SERVICE_ACCOUNT_ARGS=()
if [ -n "${SERVICE_ACCOUNT}" ]; then
  SERVICE_ACCOUNT_ARGS=(--service-account "${SERVICE_ACCOUNT}")
fi

BUILD_SERVICE_ACCOUNT_ARGS=()
if [ -n "${BUILD_SERVICE_ACCOUNT}" ]; then
  BUILD_SERVICE_ACCOUNT_ARGS=(--build-service-account "${BUILD_SERVICE_ACCOUNT}")
fi

gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --region "${REGION}" \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --cpu 1 \
  --memory 512Mi \
  --min-instances 0 \
  --max-instances 1 \
  --timeout 300s \
  "${SERVICE_ACCOUNT_ARGS[@]}" \
  "${BUILD_SERVICE_ACCOUNT_ARGS[@]}" \
  --set-env-vars PYTHONUNBUFFERED=1,GCS_BUCKET_NAME="${GCS_BUCKET_NAME}",ENGINE_TZ="America/Los_Angeles",GENERATE_ANALYSIS_IN_BATCH=1 \
  --project "${YOUR_PROJECT_ID}" # 显式指定项目ID，确保部署到正确项目

# 检查部署命令的退出状态
if [ $? -ne 0 ]; then
  echo "错误: Cloud Run 部署失败。"
  echo "请检查以上错误信息，确保您的gcloud环境配置正确，"
  echo "服务账户有足够的权限（例如 Storage Object Creator/Viewer），"
  echo "并且GCS存储桶名称正确。"
  exit 1
else
  echo ""
  echo "✅ --- ${SERVICE_NAME} 服务已成功部署！ ---"
  SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --region "${REGION}" --format='value(status.url)' --project "${YOUR_PROJECT_ID}" 2>/dev/null)"
  if [ -n "${SERVICE_URL}" ]; then
    echo "URL: ${SERVICE_URL}"
  else
    echo "您可以在 Google Cloud Console 中查看服务状态和URL。"
  fi

  # 由于 --allow-unauthenticated 可能因权限不足而只给出 warning（但命令仍返回成功），
  # 这里做一次最佳努力校验，避免部署成功但访问 403。
  IAM_JSON="$(gcloud run services get-iam-policy "${SERVICE_NAME}" --region "${REGION}" --project "${YOUR_PROJECT_ID}" --format=json 2>/dev/null)"
  if [ -n "${IAM_JSON}" ]; then
    if ! echo "${IAM_JSON}" | grep -q '"roles/run.invoker"' || ! echo "${IAM_JSON}" | grep -q '"allUsers"'; then
      echo ""
      echo "⚠️  检测到该服务目前仍需要认证（未授予 allUsers 的 roles/run.invoker）。"
      echo "    如需公开访问（例如打开 /docs），请使用拥有 run.services.setIamPolicy 权限的账号执行："
      echo "    gcloud run services add-iam-policy-binding ${SERVICE_NAME} \\"
      echo "      --region ${REGION} --project ${YOUR_PROJECT_ID} \\"
      echo "      --member=allUsers --role=roles/run.invoker"
    fi
  fi
fi
