#!/bin/bash

# 快速测试 Signed URL 生成
# 用于本地开发和调试

set -e

echo "🧪 Podcast Service - Signed URL 测试"
echo "════════════════════════════════════════════"

# 配置
API_ENDPOINT="${1:-http://localhost:8080/v4/generate}"
PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-your-project-id}"
BUCKET="${GCS_BUCKET_NAME:-podcast-service-data}"

echo "📍 API 端点: $API_ENDPOINT"
echo "📦 GCS Bucket: $BUCKET"
echo ""

# 准备测试数据
TEST_REQUEST='{
  "topic": "Tesla Q3 Earnings Report",
  "style_name": "english_2_hosts",
  "duration_minutes": 2,
  "generate_audio": true
}'

echo "📤 发送请求..."
echo ""

# 调用 API
RESPONSE=$(curl -s -X POST "$API_ENDPOINT" \
  -H "Content-Type: application/json" \
  -d "$TEST_REQUEST")

echo "📥 响应内容:"
echo ""
echo "$RESPONSE" | jq '.' 2>/dev/null || echo "$RESPONSE"
echo ""

# 提取 signed URLs
SCRIPT_URL=$(echo "$RESPONSE" | jq -r '.script_file_signed_url // empty' 2>/dev/null)
AUDIO_URL=$(echo "$RESPONSE" | jq -r '.audio_file_signed_url // empty' 2>/dev/null)

echo "════════════════════════════════════════════"
echo "🔍 Signed URL 检查"
echo "════════════════════════════════════════════"

if [ -z "$SCRIPT_URL" ]; then
    echo "❌ 脚本 Signed URL: 未生成"
else
    echo "✅ 脚本 Signed URL: 已生成"
    echo "   长度: ${#SCRIPT_URL} 字符"
    echo "   预览: ${SCRIPT_URL:0:80}..."
    echo ""
    echo "   🔗 测试链接（在浏览器中打开或使用 curl）:"
    echo "   curl -I \"$SCRIPT_URL\""
fi

echo ""

if [ -z "$AUDIO_URL" ]; then
    echo "❌ 音频 Signed URL: 未生成"
else
    echo "✅ 音频 Signed URL: 已生成"
    echo "   长度: ${#AUDIO_URL} 字符"
    echo "   预览: ${AUDIO_URL:0:80}..."
    echo ""
    echo "   🔗 测试链接（在浏览器中打开或使用 curl）:"
    echo "   curl -I \"$AUDIO_URL\""
fi

echo ""
echo "════════════════════════════════════════════"

# 验证 URL 可用性
if [ ! -z "$SCRIPT_URL" ]; then
    echo ""
    echo "🔐 测试脚本 URL 可访问性..."
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -I "$SCRIPT_URL")
    if [ "$HTTP_CODE" = "200" ]; then
        echo "✅ 脚本 URL 可访问 (HTTP $HTTP_CODE)"
    else
        echo "⚠️  脚本 URL 返回 HTTP $HTTP_CODE"
    fi
fi

if [ ! -z "$AUDIO_URL" ]; then
    echo ""
    echo "🔐 测试音频 URL 可访问性..."
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -I "$AUDIO_URL")
    if [ "$HTTP_CODE" = "200" ]; then
        echo "✅ 音频 URL 可访问 (HTTP $HTTP_CODE)"
    else
        echo "⚠️  音频 URL 返回 HTTP $HTTP_CODE"
    fi
fi

echo ""
echo "════════════════════════════════════════════"
echo "✨ 测试完成！"
