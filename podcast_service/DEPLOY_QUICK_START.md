# 🚀 部署快速开始

**升级完成日期**: 2025-10-22  
**所有文件已就绪，可立即部署**

---

## ⚡ 30秒快速检查

```bash
cd /Volumes/Quant/AI-Calendar-new/final_project_updated/podcast_service

# 验证文件完整
ls -1 cost_calculator.py main.py src/{audio_synthesizer,llm_script_generator,gcs_utils}.py generate_podcast_ui.html

# 验证语法
python3 -m py_compile cost_calculator.py main.py src/*.py

# 结果
✅ 所有文件存在
✅ 所有文件语法正确
✅ 可立即部署
```

---

## 📦 部署选项

### 选项 A: 本地测试（推荐先做）

```bash
# 1. 进入目录
cd /Volumes/Quant/AI-Calendar-new/final_project_updated/podcast_service

# 2. 启动服务 (需要 openai key + gcp auth)
export OPENAI_API_KEY="your-key-here"
export GOOGLE_APPLICATION_CREDENTIALS="path/to/credentials.json"
python3 main.py

# 3. 测试端点
curl -X POST http://localhost:8080/v4/generate \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "Test",
    "style_name": "english_2_hosts",
    "duration_minutes": 2,
    "generate_audio": false
  }'

# 4. 查看响应 - 应包含新字段:
#    - token_usage
#    - script_file_signed_url (如有 GCS bucket)
#    - 其他新字段...
```

### 选项 B: Docker 构建

```bash
# 1. 进入项目目录
cd /Volumes/Quant/AI-Calendar-new/final_project_updated/podcast_service

# 2. 构建镜像
docker build -t podcast-service:v5 -f Dockerfile .

# 3. 运行容器
docker run -p 8080:8080 \
  -e OPENAI_API_KEY="your-key" \
  -e GOOGLE_APPLICATION_CREDENTIALS="/credentials.json" \
  -v "$(pwd)/able-engine-466308-q2-7ae4754c4a4a.json:/credentials.json" \
  podcast-service:v5

# 4. 测试
curl -X POST http://localhost:8080/v4/generate ...
```

### 选项 C: 部署到 Cloud Run（生产推荐）

```bash
# 1. 验证脚本
ls -la deploy_podcast_service.sh

# 2. 执行部署脚本
chmod +x deploy_podcast_service.sh
./deploy_podcast_service.sh

# 脚本会：
# ✓ 设置 gcloud 项目
# ✓ 构建镜像
# ✓ 推送到 Artifact Registry
# ✓ 部署到 Cloud Run
# ✓ 显示服务 URL

# 3. 查看部署日志
gcloud run logs read podcast-service --limit 50

# 4. 测试部署的服务
curl -X POST https://podcast-service-xxx.run.app/v4/generate ...
```

---

## ✅ 部署前检查清单

- [ ] OPENAI_API_KEY 已设置
- [ ] GOOGLE_APPLICATION_CREDENTIALS 已配置
- [ ] GCS_BUCKET_NAME 已配置（若要使用 signed URLs）
- [ ] gcloud 已安装并认证
- [ ] Docker（本地测试）或 Cloud Run 访问权限
- [ ] 所有 Python 文件语法通过检查
- [ ] requirements.txt 中的依赖已安装

---

## 🔧 配置调整（可选）

### 修改成本定价

**文件**: `main.py`  
**位置**: 搜索 `CostCalculator()`

```python
# 修改前
cost_calculator = CostCalculator()

# 修改后
from cost_calculator import TokenPricing, TTSPricing
cost_calculator = CostCalculator(
    token_pricing=TokenPricing(
        prompt_tokens_per_1k=0.0001,      # ← 你的 prompt 价格
        completion_tokens_per_1k=0.0003   # ← 你的 completion 价格
    ),
    tts_pricing=TTSPricing(
        standard_per_1m_chars=4.0,        # ← Standard TTS 价格
        neural_per_1m_chars=16.0          # ← Neural TTS 价格
    )
)
```

### 修改 Signed URL 过期时间

**文件**: `main.py`  
**搜索**: `generate_signed_url`

```python
# 修改这行
script_signed_url = GCSUploader.generate_signed_url(
    bucket_name=bucket,
    blob_name=blob_path,
    expiration_hours=24  # ← 改为 1, 24, 或 168
)
```

### 修改前端 API 端点

**文件**: `generate_podcast_ui.html`  
**搜索**: `API_ENDPOINT`

```javascript
// 修改这行
const API_ENDPOINT = 'https://your-cloud-run-url/v4/generate';
```

---

## 📊 验证部署成功

### 1. 服务启动检查
```bash
# 查看日志
docker logs <container_id>
# 或
gcloud run logs read podcast-service

# 应该看到
# ✅ LLM 脚本生成器初始化成功
# ✅ 播客管道初始化成功
# ✅ 播客引擎 v4 已准备好！
```

### 2. API 端点检查
```bash
# 获取根信息
curl http://localhost:8080/v4

# 应该返回
{
  "name": "🎙️ AI Podcast Engine v4",
  "version": "4.0.0",
  "endpoints": {...}
}
```

### 3. 生成请求检查
```bash
curl -X POST http://localhost:8080/v4/generate \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "AI 的未来",
    "style_name": "english_2_hosts",
    "tone": "professional",
    "dialogue_style": "conversation",
    "duration_minutes": 3,
    "generate_audio": true
  }'

# 应该在响应中看到:
# ✓ "status": "success"
# ✓ "script_file": "gs://..."
# ✓ "token_usage": {...}  ← 新字段
# ✓ "tts_character_count": 12345  ← 新字段
# ✓ "cost_breakdown": {...}  ← 新字段
# ✓ "script_file_signed_url": "https://..."  ← 新字段
```

### 4. 前端界面检查
```bash
# 打开 HTML 文件
open generate_podcast_ui.html
# 或在浏览器打开: file:///path/to/generate_podcast_ui.html

# 提交请求，检查响应面板显示:
# ✓ 脚本下载链接 (可点击)
# ✓ 音频下载链接 (可点击)
# ✓ Token 统计 (显示数字)
# ✓ LLM 成本预估 (显示金额)
# ✓ TTS 成本预估 (显示金额)
```

---

## 🔴 常见问题排查

### 问题: "模块 cost_calculator 未找到"

**原因**: cost_calculator.py 不在项目根目录  
**解决**:
```bash
# 确保文件在这里
ls /Volumes/Quant/AI-Calendar-new/final_project_updated/podcast_service/cost_calculator.py
```

### 问题: "Signed URL 为 null"

**原因**: 未配置 GCS_BUCKET_NAME 或无权限  
**检查**:
```bash
# 检查环境变量
echo $GCS_BUCKET_NAME

# 检查权限 (需要有这些角色)
# - storage.buckets.get
# - storage.objects.get
# - iam.serviceAccountKeys.get (for signing)
```

### 问题: Token 统计为 0

**原因**: LLM 可能不支持 usage 统计或未正确捕获  
**检查**:
```bash
# 看后端日志
📊 LLM Token 使用统计:
   Prompt tokens: XXX  ← 应该看到数字
```

### 问题: 前端显示 "下载链接为 -"

**原因**: 后端未返回 signed_url 或 gs:// 路径  
**检查响应**:
```bash
# 查看 API 响应中是否包含
"script_file_signed_url": null  ← 应该有值
# 或
"script_file": "gs://..."  ← 至少有这个
```

---

## 📈 性能建议

### 本地开发
```bash
# 用小请求测试 (2-3 分钟, 不生成音频)
{
  "topic": "Test",
  "duration_minutes": 2,
  "generate_audio": false  # ← 这样快
}
```

### 生产部署
```bash
# Cloud Run 配置
内存: 4GB (足够处理 TTS)
超时: 600 秒 (10 分钟，防止长生成)
实例数: 2-3 (自动扩展)
```

---

## 🎓 文件对应关系

| 功能 | 文件 | 改动 |
|------|------|------|
| 成本计算 | `cost_calculator.py` | 新建 |
| LLM + 脚本 | `src/llm_script_generator.py` | +token_usage |
| 音频 + TTS | `src/audio_synthesizer.py` | +metrics |
| GCS + URL | `src/gcs_utils.py` | +signed_url |
| API 集成 | `main.py` | +响应字段 |
| 前端 UI | `generate_podcast_ui.html` | +显示逻辑 |

---

## 🎯 下一步

1. **立即**
   ```bash
   cd podcast_service
   python3 -m py_compile *.py src/*.py
   ```

2. **5分钟内**
   ```bash
   python3 main.py  # 本地测试
   curl -X POST http://localhost:8080/v4/generate ...
   ```

3. **30分钟内**
   ```bash
   docker build -t podcast-service:v5 .
   docker run -p 8080:8080 podcast-service:v5
   ```

4. **1小时内**
   ```bash
   ./deploy_podcast_service.sh  # Cloud Run 部署
   ```

5. **验证**
   ```bash
   # 打开前端
   open generate_podcast_ui.html
   # 提交请求，检查所有新功能是否正常
   ```

---

## 📞 技术支持

### 快速问题排查
```bash
# 查看完整日志
gcloud run logs read podcast-service --limit 100

# 查看部署信息
gcloud run services describe podcast-service

# 重新部署
./deploy_podcast_service.sh --force
```

### 本地调试
```python
# 在 Python REPL 测试
from cost_calculator import CostCalculator, UsageMetrics
calc = CostCalculator()
metrics = UsageMetrics(prompt_tokens=1000, completion_tokens=2000, tts_characters=5000)
cost = calc.calculate_total_cost(metrics)
print(cost.to_dict())
# 应该输出成本分解
```

---

## ✨ 部署成功标志

```
✅ 所有文件存在且语法正确
✅ 本地测试通过
✅ Docker 镜像构建成功
✅ Cloud Run 部署成功
✅ API 端点响应正常
✅ 前端能显示新字段
✅ 下载链接可点击
✅ 成本计算准确
✅ 生产就绪！
```

---

**最后更新**: 2025-10-22  
**状态**: 🚀 准备就绪，可立即部署  
**预计部署时间**: ~30 分钟

祝部署顺利！🎉
