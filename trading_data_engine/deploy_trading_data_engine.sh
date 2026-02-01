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

# 您的 Google Cloud 项目 ID (自动获取当前gcloud配置的项目ID)
YOUR_PROJECT_ID="$(gcloud config get-value project)"

# Cloud Run 服务名称
SERVICE_NAME="trading-data-engine"

# Cloud Run 部署区域
# 推荐选择离用户或数据源较近的区域，例如 us-central1, asia-east1 等。
REGION="us-central1"

# GCS 存储桶名称，用于持久化历史交易数据。
# 请确保此存储桶已存在，并且您的 Cloud Run 服务账户具有写入权限。
GCS_BUCKET_NAME="trading-data-daily-bucket"

# ------------------------------------------------------------------------------
# 脚本执行
# ------------------------------------------------------------------------------

echo "--- 正在检查 Google Cloud 项目配置 ---"
if [ -z "${YOUR_PROJECT_ID}" ]; then
  echo "错误: 未检测到当前gcloud配置的项目ID。"
  echo "请运行 'gcloud config set project YOUR_PROJECT_ID' 或 'gcloud auth login' 进行配置。"
  exit 1
fi
echo "Google Cloud 项目已自动设置为: ${YOUR_PROJECT_ID}"
echo ""

echo "--- 正在部署 ${SERVICE_NAME} 到 Google Cloud Run ---"
echo "部署区域: ${REGION}"
echo "GCS 存储桶: ${GCS_BUCKET_NAME}"
echo ""

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
  --set-env-vars PYTHONUNBUFFERED=1,GCS_BUCKET_NAME="${GCS_BUCKET_NAME}" \
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
  echo "您可以在 Google Cloud Console 中查看服务状态和URL。"
fi
