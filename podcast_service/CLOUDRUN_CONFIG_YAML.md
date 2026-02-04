# 🔧 cloudrun-config.yaml 说明文档

## 📝 cloudrun-config.yaml 是什么？

`cloudrun-config.yaml` 是一个 **Knative Service 配置文件**，用于定义 Cloud Run 服务的详细配置。

### 📍 位置
```
podcast_service/
└── cloudrun-config.yaml
```

---

## 🎯 用途

### 简单说明
```
部署脚本 (deploy_podcast_service.sh)
    ↓
使用 gcloud run deploy 命令
    ↓
自动生成 Cloud Run 配置
    ↓
服务运行（已自动配置）

VS.

cloudrun-config.yaml
    ↓
手动应用配置文件
    ↓
kubectl apply -f cloudrun-config.yaml
    ↓
服务运行（按 YAML 配置）
```

---

## ⚠️ 重要提示：现在需要吗？

### ❌ **不需要用这个文件！**

**原因**：
1. `deploy_podcast_service.sh` 脚本已经处理了所有配置
2. 脚本自动设置所有参数（CPU、内存、超时等）
3. 不需要手动应用 YAML 文件

### ✅ 什么时候需要？

| 场景 | 需要? |
|------|------|
| 使用 `deploy_podcast_service.sh` 部署 | ❌ 否 |
| 使用 `gcloud run deploy` 命令部署 | ❌ 否 |
| 需要版本控制配置 | ⚠️ 可选 |
| 使用 GitOps / 自动化部署 | ✅ 是 |
| 手动修改 Knative 配置 | ✅ 是 |

---

## 📋 文件内容详解

### 1. 元数据 (Metadata)
```yaml
metadata:
  name: podcast-service
  namespace: default
```
- **name**: 服务名称（必须与 Cloud Run 服务名称相同）
- **namespace**: Kubernetes 命名空间（Cloud Run 中通常是 default）

### 2. 容器配置 (Container)
```yaml
containers:
- image: gcr.io/YOUR_PROJECT_ID/podcast-service:latest
  ports:
  - containerPort: 8080
```
- **image**: Docker 镜像地址（需要更新为实际项目 ID）
- **containerPort**: 应用监听的端口（FastAPI 使用 8080）

### 3. 环境变量 (Environment Variables)
```yaml
env:
- name: OPENAI_API_KEY
  valueFrom:
    secretKeyRef:
      name: openai-secret
      key: api-key
```
- 从 Kubernetes Secret 中读取敏感信息
- 比 .env 文件更安全

### 4. 资源限制 (Resources)
```yaml
resources:
  limits:
    memory: "2Gi"      # 最大内存：2 GB
    cpu: "2"           # 最大 CPU：2 vCPU
  requests:
    memory: "1Gi"      # 请求内存：1 GB
    cpu: "1"           # 请求 CPU：1 vCPU
```
- 限制容器使用的资源
- 影响成本和性能

### 5. 健康检查 (Probes)
```yaml
livenessProbe:        # 存活检查
  httpGet:
    path: /health
    port: 8080
    
readinessProbe:       # 就绪检查
  httpGet:
    path: /health
    port: 8080
```
- 定期检查服务是否运行正常
- 容器有问题时自动重启

### 6. 超时配置 (Timeout)
```yaml
timeout: 300s         # 10 分钟超时
```

---

## 🔄 对比：三种部署方式

| 方式 | 文件 | 优点 | 缺点 |
|------|------|------|------|
| **脚本部署** | `deploy_podcast_service.sh` | ✅ 自动化，简单 | 配置硬编码在脚本中 |
| **CLI 命令** | gcloud 命令行 | ✅ 灵活 | 需要记住所有参数 |
| **YAML 文件** | `cloudrun-config.yaml` | ✅ 版本控制，可重用 | 需要手动管理 |

### 现在使用的方式
```bash
./deploy_podcast_service.sh
# ↓
# 使用 gcloud run deploy 命令
# ↓
# 自动应用所有配置
```

---

## 📌 何时手动使用 cloudrun-config.yaml？

### 场景 1: 使用 kubectl 部署到 GKE
```bash
# 如果在 Google Kubernetes Engine (GKE) 中运行
kubectl apply -f cloudrun-config.yaml
```

### 场景 2: 版本控制所有配置
```bash
# 在 Git 中追踪配置历史
git add cloudrun-config.yaml
git commit -m "Update Cloud Run config"
```

### 场景 3: 自动化部署 (GitOps)
```bash
# 使用 ArgoCD、Flux 等工具自动部署
# 工具自动检测 YAML 变化并应用
```

---

## ✅ 建议：现在需要做什么？

### 立即部署时
**✅ 不需要修改或使用 cloudrun-config.yaml**

只需运行：
```bash
./deploy_podcast_service.sh
```

### 如果要手动应用配置（不推荐）
需要先修改文件中的：
```yaml
# 将此行
image: gcr.io/YOUR_PROJECT_ID/podcast-service:latest

# 改为
image: gcr.io/able-engine-466308-q2/podcast-service:latest
```

然后使用：
```bash
kubectl apply -f cloudrun-config.yaml
```

---

## 📊 对比 deploy_podcast_service.sh 中的配置

### cloudrun-config.yaml 中的设置
```yaml
resources:
  limits:
    cpu: "2"
    memory: "2Gi"
  timeout: 300s
  ports: 8080
```

### deploy_podcast_service.sh 中的等效设置
```bash
gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --cpu 2 \
  --memory 2Gi \
  --timeout 600s \
  --port 8080
```

**结果相同**，但脚本更方便自动化。

---

## 🎯 最终建议

### 现在（立即部署）
```
❌ 不需要使用 cloudrun-config.yaml
✅ 使用 deploy_podcast_service.sh 脚本
```

### 未来（如果需要）
```
✅ 如果要使用 GitOps 自动化部署
✅ 如果要版本控制所有配置
✅ 如果要在 GKE 中运行
```

### 保留这个文件？
```
✅ 是的，保留它
   • 作为参考文档
   • 未来可能需要
   • 不占用空间
```

---

## 🔍 文件安全性

### 当前配置中的敏感信息处理

❌ **不要这样做**（明文密钥）：
```yaml
env:
- name: OPENAI_API_KEY
  value: "sk-xxx..."  # ❌ 危险！
```

✅ **应该这样做**（从 Secret 读取）：
```yaml
env:
- name: OPENAI_API_KEY
  valueFrom:
    secretKeyRef:
      name: openai-secret
      key: api-key  # ✅ 安全！
```

当前的 `cloudrun-config.yaml` 已经使用了安全的方式。

---

## 📋 总结表

| 问题 | 答案 |
|------|------|
| **cloudrun-config.yaml 是什么？** | Knative Service 配置文件 |
| **现在需要用吗？** | ❌ 不需要 |
| **deploy_podcast_service.sh 已经处理了吗？** | ✅ 是的 |
| **何时需要用？** | 使用 GitOps 或 GKE 时 |
| **应该保留吗？** | ✅ 是的，作为参考 |
| **需要修改吗？** | ❌ 不需要，除非要手动使用 |

---

## 🚀 现在该做什么？

### 保持现状
```bash
cd podcast_service
./deploy_podcast_service.sh
# 脚本会自动处理所有配置
```

### 不需要操作 cloudrun-config.yaml
- ✅ 保留文件（参考用）
- ✅ 不需要修改
- ✅ 不需要手动应用

---

**结论**：

`cloudrun-config.yaml` 是一个 **可选参考文件**，用于手动或自动化部署场景。现在使用部署脚本时完全不需要它。

**现在可以安心运行**：
```bash
./deploy_podcast_service.sh
```

---

**更新时间**: 2025-10-21  
**文件用途**: 参考文档 (可选)
