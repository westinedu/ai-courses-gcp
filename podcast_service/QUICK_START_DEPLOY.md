# 🚀 Podcast Service 快速部署指南

## 前置条件

在部署前，确保您拥有以下条件：

### 1. 本地环境
```bash
# 安装 Google Cloud CLI
# macOS (使用 Homebrew)
brew install --cask google-cloud-sdk

# 或访问 https://cloud.google.com/sdk/docs/install

# 验证安装
gcloud --version
```

### 2. GCP 账户和项目
- 拥有 GCP 账户
- 已创建 GCP 项目
- 项目 ID: `able-engine-466308-q2` (或您自己的项目 ID)

### 3. 验证和权限
```bash
# 登录 GCP
gcloud auth login

# 设置默认项目
gcloud config set project able-engine-466308-q2

# 验证配置
gcloud config list
```

### 4. 必要的文件（已包含）
- ✅ `main.py` - FastAPI 应用入口
- ✅ `Dockerfile` - 容器配置
- ✅ `requirements.txt` - Python 依赖
- ✅ `.env` - 环境变量（已从 podcast_engine 复制）
- ✅ `able-engine-466308-q2-7ae4754c4a4a.json` - Google Cloud 服务账户密钥
- ✅ `deploy_podcast_service.sh` - 部署脚本

## ✨ 一键部署

### 最简单的方式（自动检测区域）

```bash
# 进入 podcast_service 目录
cd podcast_service

# 运行部署脚本
./deploy_podcast_service.sh
```

### 指定特定区域

```bash
# 部署到特定区域（例如 asia-east1）
./deploy_podcast_service.sh asia-east1
```

### 支持的区域
- `us-central1` (默认) - 美国中部
- `us-east1` - 美国东部
- `europe-west1` - 欧洲西部
- `asia-east1` - 亚洲东部 (台湾)
- `asia-northeast1` - 亚洲东北部 (东京)
- 更多区域见: [Google Cloud 地区](https://cloud.google.com/run/docs/quickstarts/deploy-continuously)

## 📝 部署脚本会做什么？

部署脚本 `deploy_podcast_service.sh` 会自动执行以下操作：

### 1️⃣ 环境检查
- ✅ 检查 `gcloud` CLI 是否安装
- ✅ 验证您是否已登录 GCP
- ✅ 检查项目 ID 是否配置
- ✅ 验证必要文件是否存在

### 2️⃣ 启用必要的 APIs
- ✅ Cloud Run API
- ✅ Cloud Build API
- ✅ Container Registry API
- ✅ Text-to-Speech API
- ✅ Cloud Storage API
- ✅ Secret Manager API

### 3️⃣ 创建存储资源
- ✅ 创建 Cloud Storage 存储桶 (`podcast-service-data`)
- ✅ 设置适当的访问权限

### 4️⃣ 验证凭证
- ✅ 检查 `.env` 文件中的 API 密钥
- ✅ 验证 Google Cloud 服务账户密钥
- ✅ 确保所有必要的配置已准备好

### 5️⃣ 构建和部署
- ✅ 使用 Cloud Build 构建 Docker 镜像
- ✅ 将镜像推送到 Container Registry
- ✅ 部署到 Cloud Run
- ✅ 配置自动扩展 (0-100 实例)
- ✅ 设置超时时间为 10 分钟

### 6️⃣ 输出服务信息
- ✅ 显示服务 URL
- ✅ 提供常用命令示例
- ✅ 提供故障排除建议

## 🔧 常用命令

### 部署或更新服务
```bash
cd podcast_service
./deploy_podcast_service.sh
```

### 查看服务状态
```bash
gcloud run services describe podcast-service --region us-central1
```

### 查看实时日志
```bash
gcloud run services logs read podcast-service --region us-central1 --limit 50 --follow
```

### 查看最后 100 行日志
```bash
gcloud run services logs read podcast-service --region us-central1 --limit 100
```

### 获取服务 URL
```bash
gcloud run services describe podcast-service \
  --region us-central1 \
  --format='value(status.url)'
```

### 删除服务
```bash
gcloud run services delete podcast-service --region us-central1
```

### 查看配置和环境变量
```bash
gcloud run services describe podcast-service \
  --region us-central1 \
  --format=json | jq '.spec.template.spec.containers[0].env'
```

## 📊 部署后验证

### 1. 访问服务
```bash
# 获取服务 URL
SERVICE_URL=$(gcloud run services describe podcast-service \
  --region us-central1 \
  --format='value(status.url)')

# 访问服务
open "$SERVICE_URL"

# 或使用 curl
curl "$SERVICE_URL"
```

### 2. 访问 API 文档
```bash
# Swagger UI (OpenAPI 文档)
open "${SERVICE_URL}/docs"

# ReDoc (备选文档)
open "${SERVICE_URL}/redoc"
```

### 3. 测试生成播客
```bash
curl -X POST "${SERVICE_URL}/v4/generate" \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "测试话题",
    "content": "这是测试内容",
    "podcast_style": "english_2_hosts",
    "target_duration": 300,
    "generate_audio": true
  }'
```

### 4. 监控指标
访问 Google Cloud Console:
- [Cloud Run 服务](https://console.cloud.google.com/run)
- [Cloud Logging](https://console.cloud.google.com/logs)
- [Cloud Monitoring](https://console.cloud.google.com/monitoring)

## ⚠️ 常见问题和解决方案

### 问题 1: "项目 ID 未检测到"
```bash
# 解决方案：设置默认项目
gcloud config set project able-engine-466308-q2

# 验证
gcloud config list
```

### 问题 2: "API 未启用"
```bash
# 解决方案：手动启用 API
gcloud services enable run.googleapis.com \
  cloudbuild.googleapis.com \
  containerregistry.googleapis.com \
  texttospeech.googleapis.com
```

### 问题 3: "权限被拒绝"
```bash
# 解决方案：检查 IAM 角色
gcloud projects get-iam-policy able-engine-466308-q2

# 需要的角色：
# - roles/run.admin
# - roles/cloudbuild.builds.editor
# - roles/storage.admin
```

### 问题 4: "服务账户密钥无效"
```bash
# 验证密钥文件内容
cat able-engine-466308-q2-7ae4754c4a4a.json | jq .

# 应该包含：
# "type": "service_account"
# "project_id": "able-engine-466308-q2"
# "private_key": "..."
```

### 问题 5: "部署超时"
- 首次部署可能需要 5-10 分钟（包括构建和推送镜像）
- 之后的部署会更快（约 2-3 分钟）
- 如果超时，检查网络连接和 GCP 配额

## 📈 性能和成本

### 当前配置
- **CPU**: 2 个 vCPU
- **内存**: 2 GB
- **超时**: 600 秒 (10 分钟)
- **并发**: 自动扩展 (0-100 实例)

### 成本估算
假设每月 10,000 次请求，平均响应时间 30 秒：

```
= 10,000 requests × 30s = 300,000 秒
= 300,000 ÷ 3,600 = 83.33 小时的 CPU 时间
= 83.33 小时 × 2 vCPU = 166.67 vCPU-小时

成本：
- 计算: 166.67 × $0.00002400 = $4.00
- 存储: ~1 GB × $0.020 = $0.02
- 网络: 数据输出可能 $0.12/GB
- 总计: 约 $4-20/月 (取决于流量和数据输出)
```

### 优化建议
如果成本过高，可以：
1. 减少 CPU (从 2 改为 1)
2. 减少内存 (从 2GB 改为 1GB)
3. 增加 `--min-instances 0` (自动缩放)
4. 设置更少的最大并发实例

## 🔐 安全检查清单

部署前请确保：

- [ ] `.env` 文件中的 API 密钥是最新的
- [ ] Google Cloud 服务账户密钥文件存在且有效
- [ ] `.gitignore` 包含 `.env` 和 `*.json` (保护敏感信息)
- [ ] 您没有在代码中硬编码任何 API 密钥
- [ ] Cloud Run 服务只允许必要的入站连接
- [ ] 已启用审计日志记录
- [ ] 定期检查 IAM 权限

## 📚 进一步阅读

- [Google Cloud Run 文档](https://cloud.google.com/run/docs)
- [Cloud Run 最佳实践](https://cloud.google.com/run/docs/quickstarts/build-and-deploy)
- [Python FastAPI on Cloud Run](https://cloud.google.com/run/docs/quickstarts/build-and-deploy/python)
- [Cloud Run 价格](https://cloud.google.com/run/pricing)

## 🆘 获取帮助

如果遇到问题：

1. **查看脚本输出** - 脚本会显示详细的错误信息
2. **检查日志** - `gcloud run services logs read podcast-service`
3. **查看 GCP Console** - https://console.cloud.google.com
4. **联系支持** - Google Cloud 支持团队

---

**祝您部署顺利！** 🎉

有任何问题，请随时询问。
