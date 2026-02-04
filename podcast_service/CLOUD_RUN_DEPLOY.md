# 🚀 播客服务 - Cloud Run 部署指南

## 📋 前置准备

### 1. 创建GCP项目
```bash
# 设置项目ID
export PROJECT_ID="your-project-id"
gcloud config set project $PROJECT_ID
```

### 2. 启用所需API
```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  containerregistry.googleapis.com \
  texttospeech.googleapis.com \
  secretmanager.googleapis.com
```

### 3. 创建服务账户
```bash
# 为Cloud Run创建服务账户
gcloud iam service-accounts create podcast-service \
  --display-name "Podcast Service"

# 授予权限
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:podcast-service@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/texttospeech.client"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:podcast-service@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/storage.admin"
```

### 4. 添加OpenAI API密钥到Secret Manager
```bash
echo -n "sk-your-openai-api-key" | gcloud secrets create openai-api-key --data-file=-

# 授予Cloud Run服务访问权限
gcloud secrets add-iam-policy-binding openai-api-key \
  --member="serviceAccount:podcast-service@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

### 5. 生成Google Cloud服务账户密钥
```bash
gcloud iam service-accounts keys create key.json \
  --iam-account=podcast-service@${PROJECT_ID}.iam.gserviceaccount.com
```

## 📦 构建并推送Docker镜像

### 1. 构建镜像
```bash
gcloud builds submit \
  --tag gcr.io/${PROJECT_ID}/podcast-service:latest \
  --timeout=1800s
```

或者使用本地Docker：
```bash
docker build -t podcast-service:latest .
docker tag podcast-service:latest gcr.io/${PROJECT_ID}/podcast-service:latest
docker push gcr.io/${PROJECT_ID}/podcast-service:latest
```

### 2. 验证镜像
```bash
gcloud container images list --repository=gcr.io/${PROJECT_ID}
```

## 🚀 部署到Cloud Run

### 1. 部署服务
```bash
gcloud run deploy podcast-service \
  --image gcr.io/${PROJECT_ID}/podcast-service:latest \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --memory 2Gi \
  --cpu 2 \
  --timeout 300 \
  --max-instances 100 \
  --set-env-vars="OPENAI_API_KEY=$(gcloud secrets versions access latest --secret=openai-api-key)" \
  --service-account=podcast-service@${PROJECT_ID}.iam.gserviceaccount.com
```

### 2. 获取服务URL
```bash
gcloud run services describe podcast-service \
  --platform managed \
  --region us-central1 \
  --format 'value(status.url)'
```

## ✅ 测试部署

### 1. 健康检查
```bash
SERVICE_URL=$(gcloud run services describe podcast-service \
  --platform managed \
  --region us-central1 \
  --format 'value(status.url)')

curl ${SERVICE_URL}/health
```

### 2. API文档
访问: `${SERVICE_URL}/docs`

### 3. 测试生成播客
```bash
curl -X POST ${SERVICE_URL}/v4/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "topic": "Test Podcast",
    "style_name": "english_4_panel",
    "tone": "professional",
    "dialogue_style": "conversation",
    "duration_minutes": 5,
    "source_content": "This is test content for podcast generation.",
    "generate_audio": false
  }'
```

## 📊 监控和日志

### 1. 查看日志
```bash
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=podcast-service" \
  --limit 50 \
  --format json
```

### 2. 实时日志
```bash
gcloud alpha run services logs read podcast-service --limit 50 --follow
```

### 3. 查看指标
```bash
# 在GCP Console查看
# Cloud Run → podcast-service → Metrics
```

## 🔧 故障排查

### 问题1: 容器启动失败
```bash
# 检查容器日志
gcloud run services describe podcast-service --platform managed --region us-central1
```

### 问题2: 权限错误
```bash
# 验证服务账户权限
gcloud projects get-iam-policy $PROJECT_ID \
  --flatten="bindings[].members" \
  --format='table(bindings.role)' \
  --filter="bindings.members:podcast-service@${PROJECT_ID}.iam.gserviceaccount.com"
```

### 问题3: Secret Manager访问失败
```bash
# 验证Secret Manager权限
gcloud secrets get-iam-policy openai-api-key
```

## 💰 成本优化

### 1. 自动扩缩容配置
```bash
gcloud run services update podcast-service \
  --min-instances 0 \
  --max-instances 50 \
  --region us-central1
```

### 2. 内存优化
- 如果负载不高，可以减少内存到1Gi
- 根据实际需求调整CPU和内存

### 3. 地域优化
- 根据用户位置选择最近的区域
- 可以部署多个地域的实例

## 📈 性能调优

### 1. 增加并发能力
```bash
gcloud run services update podcast-service \
  --concurrency 100 \
  --region us-central1
```

### 2. 调整超时时间
```bash
gcloud run services update podcast-service \
  --timeout 300 \
  --region us-central1
```

### 3. 启用VPC连接（可选）
```bash
gcloud run services update podcast-service \
  --vpc-connector projects/${PROJECT_ID}/locations/us-central1/connectors/my-connector \
  --region us-central1
```

## 🔐 安全最佳实践

### 1. 禁用公共访问（可选）
```bash
gcloud run services remove-iam-policy-binding podcast-service \
  --member="allUsers" \
  --role="roles/run.invoker" \
  --platform managed \
  --region us-central1
```

### 2. 启用VPC-SC（可选）
```bash
# 在GCP Console中配置VPC Service Controls
```

### 3. 定期轮换API密钥
```bash
# 创建新的Secret版本
echo -n "new-api-key" | gcloud secrets versions add openai-api-key --data-file=-
```

## 📝 更新部署

### 1. 更新代码后重新部署
```bash
gcloud builds submit --tag gcr.io/${PROJECT_ID}/podcast-service:latest
gcloud run deploy podcast-service \
  --image gcr.io/${PROJECT_ID}/podcast-service:latest \
  --platform managed \
  --region us-central1
```

### 2. 回滚到之前的版本
```bash
gcloud run services update-traffic podcast-service \
  --to-revisions REVISION_NAME=100 \
  --region us-central1
```

## 📚 相关资源

- [Cloud Run文档](https://cloud.google.com/run/docs)
- [Cloud Run定价](https://cloud.google.com/run/pricing)
- [Text-to-Speech定价](https://cloud.google.com/text-to-speech/pricing)
- [OpenAI API定价](https://openai.com/pricing)

---

## 🎉 部署完成！

成功部署后，您的播客服务将：
- ✅ 自动扩展以处理流量
- ✅ 高可用和容错
- ✅ 按使用量计费
- ✅ 无需管理服务器
