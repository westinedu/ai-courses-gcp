#!/bin/bash

# 🔐 IAM 权限配置脚本
# 用于配置 Signed URL 生成所需的权限

set -e

echo "🔐 配置 Signed URL IAM 权限"
echo "════════════════════════════════════════════"

# 获取配置
PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-able-engine-466308-q2}"
SA_EMAIL="${GOOGLE_SERVICE_ACCOUNT_EMAIL:-355131621961-compute@developer.gserviceaccount.com}"

echo "📋 配置信息:"
echo "  项目 ID: $PROJECT_ID"
echo "  服务账号: $SA_EMAIL"
echo ""

# 1. 添加 Service Account Token Creator 角色（包含 signBlob 权限）
echo "1️⃣ 添加 Service Account Token Creator 角色..."
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/iam.serviceAccountTokenCreator" \
  --project="$PROJECT_ID"

echo "✅ Token Creator 角色已添加"
echo ""

# 2. 添加 Storage Admin 角色（GCS 访问权限）
echo "2️⃣ 添加 Storage Admin 角色..."
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/storage.admin"

echo "✅ Storage Admin 角色已添加"
echo ""

# 验证权限
echo "3️⃣ 验证权限..."
echo "  检查 Token Creator 权限:"
gcloud iam service-accounts get-iam-policy "$SA_EMAIL" \
  --project="$PROJECT_ID" \
  --format="table(bindings.role,bindings.members)" | grep -i token || echo "  ⚠️ 未找到 Token Creator"

echo ""
echo "  检查 Storage Admin 权限:"
gcloud projects get-iam-policy "$PROJECT_ID" \
  --flatten="bindings[].members" \
  --format="table(bindings.role)" \
  --filter="bindings.members:serviceAccount:$SA_EMAIL AND bindings.role:roles/storage.admin" || echo "  ⚠️ 未找到 Storage Admin"

echo ""
echo "════════════════════════════════════════════"
echo "✅ IAM 权限配置完成！"
echo ""
echo "📝 已配置的权限:"
echo "  ✓ roles/iam.serviceAccountTokenCreator"
echo "  ✓ roles/storage.admin"
echo ""
echo "现在可以部署 podcast_service 并生成 Signed URLs 了！"
