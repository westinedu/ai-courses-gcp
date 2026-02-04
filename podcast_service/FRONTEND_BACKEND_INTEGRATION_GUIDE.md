# 🎙️ 播客服务前后端集成完整方案
## 2025年10月22日

---

## 📋 概述

这个文档汇总了前端 UI 升级和后端需要配合的所有改动。前端已完成升级，后端需要进行分阶段实施。

### 最终效果

用户生成播客后，将看到：

```
✅ 生成成功！

标题: AI发展趋势讨论
描述: 三位专家讨论当下AI技术的最新进展
段落数: 18
预计时长: 300.0 秒 (约 5.0 分钟)

脚本文件: gs://my-bucket/scripts/podcast_xxx.json
脚本 GCS 路径: gs://my-bucket/scripts/podcast_xxx.json
脚本下载: [下载脚本] ← 可点击下载

音频文件: gs://my-bucket/audio/podcast_xxx.mp3
音频下载: [下载音频] ← 可点击下载

Token使用: 总计: 2,900 (提示: 1,300, 完成: 1,600)

LLM 消耗估算 (可配置):
  每1000 tokens 价格 (USD): 0.02
  估算: 0.0058 USD

Google TTS 消费估算:
  每 1,000,000 字符 价格 (USD): 4.00
  估算: $0.0180 USD (≈ 4,500 字符)
```

---

## 🎯 前端状态

### ✅ 已完成

文件: `podcast_service/generate_podcast_ui.html`

**新增功能**:
1. ✅ 显示 GCS 路径 (`result-script-gcs`)
2. ✅ 脚本下载链接 (`result-script-download`) - 点击下载
3. ✅ 音频下载链接 (`result-audio-download`) - 点击下载
4. ✅ Token 使用统计 (总数/提示词/补全)
5. ✅ LLM 成本估算 (用户可配置 $/1k tokens 价格)
6. ✅ Google TTS 成本估算 (用户可配置 $/1M chars 价格)

**关键代码**:
- 自动转换 `gs://bucket/path` 为 `https://storage.googleapis.com/bucket/path`（可直接下载）
- 实时计算 LLM 成本: `(total_tokens / 1000) * price_per_1k`
- TTS 成本两种计算方式:
  - 如果后端返回 `tts_character_count`: `(chars / 1M) * price_per_1M`
  - 否则估算: 假设每秒平均 15 字符

---

## 🔧 后端需要的升级

### Phase 1: 关键指标收集 (优先级: ⭐⭐⭐)

#### 需求 1: LLM Token 使用统计
**当前**: ❌ 未返回
**升级**: ✅ 从 OpenAI API 响应中提取并返回

```python
# 后端返回
{
  "token_usage": {
    "prompt_tokens": 1300,
    "completion_tokens": 1600,
    "total_tokens": 2900
  },
  "estimated_llm_cost_usd": 0.00159  # 基于 gpt-4o-mini 定价
}
```

**涉及文件**:
- `podcast_service/src/llm_script_generator.py` - 提取 token 信息
- `podcast_service/src/cost_calculator.py` - 新建，计算成本
- `podcast_service/main.py` - 集成到响应

**预计工作量**: 30 分钟

---

#### 需求 2: Google TTS 字符计数
**当前**: ❌ 未跟踪
**升级**: ✅ 从脚本中计算实际发送给 TTS 的字符数

```python
# 后端返回
{
  "tts_character_count": 4500,          # 实际字符数
  "estimated_tts_cost_usd": 0.018,      # 基于 $4/1M chars
  "tts_billable_seconds": 270           # 计费时长
}
```

**涉及文件**:
- `podcast_service/src/audio_synthesizer.py` - 添加指标计算
- `podcast_service/main.py` - 集成到响应

**预计工作量**: 20 分钟

---

### Phase 2: 下载优化 (优先级: ⭐⭐)

#### 需求 3: Signed URLs for 直接下载
**当前**: ❌ 返回本地路径或 `gs://...` 路径（浏览器无法直接下载）
**升级**: ✅ 返回可浏览器直接下载的 HTTPS signed URLs

```python
# 后端返回
{
  "script_file": "gs://my-bucket/scripts/podcast_xxx.json",
  "script_file_signed_url": "https://storage.googleapis.com/my-bucket/scripts/podcast_xxx.json?X-Goog-Algorithm=...",
  "audio_file": "gs://my-bucket/audio/podcast_xxx.mp3",
  "audio_file_signed_url": "https://storage.googleapis.com/my-bucket/audio/podcast_xxx.mp3?X-Goog-Algorithm=...",
}
```

**优势**:
- 用户无需配置 GCS 身份验证即可下载
- Signed URL 有效期 1 小时，自动过期
- 前端无需额外代码，直接 `<a href="...">下载</a>`

**涉及文件**:
- `podcast_service/src/gcs_utils.py` - 生成 signed URLs
- `podcast_service/main.py` - 集成到响应

**预计工作量**: 20 分钟

---

## 📊 后端实施步骤

### 第1步: 创建成本计算模块

**文件**: `podcast_service/src/cost_calculator.py` (新建)

```python
def calculate_llm_cost(token_usage, model="gpt-4o-mini"):
    """计算 LLM 成本"""
    # GPT-4o-mini: input $0.15/1M, output $0.60/1M
    pricing = LLM_PRICING[model]
    cost = (
        token_usage['prompt_tokens'] * pricing['input'] +
        token_usage['completion_tokens'] * pricing['output']
    )
    return round(cost, 6)

def calculate_tts_cost(tts_metrics, engine="google-cloud"):
    """计算 TTS 成本"""
    # Google TTS: $4.00 / 1M characters
    cost = tts_metrics['character_count'] * TTS_PRICING[engine]
    return round(cost, 6)
```

**完整代码**: 见 `BACKEND_UPGRADE_PATCHES.md` 补丁 4

---

### 第2步: 升级 LLMScriptGenerator

**文件**: `podcast_service/src/llm_script_generator.py`

**改动**: 返回 token 使用信息

```python
# 当前
return script

# 升级
return script, token_usage  # tuple
```

**关键位置**: ~第 250-270 行

**完整补丁**: 见 `BACKEND_UPGRADE_PATCHES.md` 补丁 1

---

### 第3步: 升级 AudioSynthesizer

**文件**: `podcast_service/src/audio_synthesizer.py`

**改动**: 添加 `generate_from_script_with_metrics()` 方法返回 TTS 指标

```python
def generate_from_script_with_metrics(...):
    output_path = self.generate_from_script(...)
    tts_metrics = self._calculate_tts_metrics(script_data)
    return output_path, tts_metrics

def _calculate_tts_metrics(script_data):
    # 计算发送给 TTS 的总字符数
    total_chars = sum(len(clean_text(seg)) for seg in script_data['segments'])
    # 计算计费时长
    billable_seconds = round(script_data['estimated_duration_seconds'])
    return {
        'character_count': total_chars,
        'billable_seconds': billable_seconds,
        'segments_count': len(script_data['segments'])
    }
```

**完整补丁**: 见 `BACKEND_UPGRADE_PATCHES.md` 补丁 2

---

### 第4步: 升级 GCSUploader

**文件**: `podcast_service/src/gcs_utils.py`

**改动**: `upload_file()` 生成 signed URLs

```python
# 当前
return f"gs://{bucket_name}/{destination_path}"

# 升级
signed_url = blob.generate_signed_url(
    version="v4",
    expiration=timedelta(hours=1),
    method="GET"
)
return gs_uri, signed_url
```

**完整补丁**: 见 `BACKEND_UPGRADE_PATCHES.md` 补丁 3

---

### 第5步: 升级 main.py

**文件**: `podcast_service/main.py`

**改动**:
1. 更新 `GeneratePodcastResponse` 模型
2. 导入 `cost_calculator` 模块
3. 修改 `generate_podcast_v4()` 收集指标
4. 构建完整响应

**关键改动点**:

```python
# 1. 导入
from src.cost_calculator import calculate_llm_cost, calculate_tts_cost

# 2. 响应模型
class GeneratePodcastResponse(BaseModel):
    # ... 既有字段 ...
    token_usage: Optional[Dict[str, int]] = None              # ✅ 新增
    estimated_llm_cost_usd: Optional[float] = None            # ✅ 新增
    tts_character_count: Optional[int] = None                 # ✅ 新增
    estimated_tts_cost_usd: Optional[float] = None            # ✅ 新增
    script_file_signed_url: Optional[str] = None              # ✅ 新增
    audio_file_signed_url: Optional[str] = None               # ✅ 新增

# 3. 生成脚本（获取 token 信息）
script, token_usage = script_generator.generate_script(...)
llm_cost = calculate_llm_cost(token_usage, script_generator.model)

# 4. 生成音频（获取 TTS 指标）
output_path, tts_metrics = synthesizer.generate_from_script_with_metrics(...)
tts_cost = calculate_tts_cost(tts_metrics, "google-cloud")

# 5. 生成 signed URLs
script_uri, script_signed_url = GCSUploader.upload_file(...)
audio_uri, audio_signed_url = GCSUploader.upload_file(...)

# 6. 构建响应
response = GeneratePodcastResponse(
    # ... 既有字段 ...
    token_usage=token_usage,                      # ✅ 新增
    estimated_llm_cost_usd=llm_cost,              # ✅ 新增
    tts_character_count=tts_metrics['character_count'],  # ✅ 新增
    estimated_tts_cost_usd=tts_cost,              # ✅ 新增
    script_file_signed_url=script_signed_url,     # ✅ 新增
    audio_file_signed_url=audio_signed_url,       # ✅ 新增
)
```

**完整补丁**: 见 `BACKEND_UPGRADE_PATCHES.md` 补丁 5

---

## 🧪 测试清单

### 本地测试 (无 GCS)

```bash
# 1. 启动服务
export OPENAI_API_KEY="sk-..."
cd podcast_service
python main.py

# 2. 生成仅脚本
curl -X POST http://localhost:8080/v4/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "topic": "AI发展",
    "duration_minutes": 2,
    "generate_audio": false
  }'

# 3. 验证返回
# - status: success
# - script_file: 本地路径
# - token_usage: 不为空
# - estimated_llm_cost_usd: > 0
# - script_file_signed_url: null (本地无 GCS)
```

**预期响应**:
```json
{
  "status": "success",
  "script_file": "/path/to/podcast_xxx_script.json",
  "script_file_signed_url": null,
  "token_usage": {
    "prompt_tokens": 1200,
    "completion_tokens": 1500,
    "total_tokens": 2700
  },
  "estimated_llm_cost_usd": 0.00135,
  "tts_character_count": null,
  "estimated_tts_cost_usd": null,
  "generation_time_seconds": 45.2
}
```

---

### Cloud Run 部署测试 (with GCS)

```bash
# 1. 设置环境变量
export GCS_BUCKET_NAME="my-podcast-bucket"
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account.json"

# 2. 部署到 Cloud Run
gcloud builds submit --config cloudbuild.yaml

# 3. 测试
curl -X POST https://podcast-service-xxx.run.app/v4/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "topic": "加密货币市场",
    "duration_minutes": 3,
    "generate_audio": true
  }'
```

**预期响应**:
```json
{
  "status": "success",
  "script_file": "gs://my-bucket/generated_scripts/podcast_xxx.json",
  "script_file_signed_url": "https://storage.googleapis.com/my-bucket/generated_scripts/podcast_xxx.json?X-Goog-Algorithm=...",
  "audio_file": "gs://my-bucket/generated_podcasts/podcast_xxx.mp3",
  "audio_file_signed_url": "https://storage.googleapis.com/my-bucket/generated_podcasts/podcast_xxx.mp3?X-Goog-Algorithm=...",
  "token_usage": {
    "prompt_tokens": 1300,
    "completion_tokens": 1600,
    "total_tokens": 2900
  },
  "estimated_llm_cost_usd": 0.00159,
  "tts_character_count": 4500,
  "estimated_tts_cost_usd": 0.0180,
  "tts_billable_seconds": 270,
  "generation_time_seconds": 125.3
}
```

**验证**:
- [ ] Signed URLs 可在浏览器中点击下载文件
- [ ] 下载的文件内容正确
- [ ] Token 统计数字合理
- [ ] 成本计算正确
- [ ] 日志中显示所有指标

---

### 前端集成测试

```bash
# 1. 打开 HTML 文件
open podcast_service/generate_podcast_ui.html

# 2. 提交表单
- Topic: "AI发展趋势"
- Duration: 3 分钟
- Style: english_4_panel
- Generate Audio: 勾选

# 3. 验证显示
- [ ] 脚本下载链接可点击并下载文件
- [ ] 音频下载链接可点击并下载文件
- [ ] Token 统计正确显示
- [ ] LLM 成本按照输入的价格计算正确
- [ ] TTS 字符数和成本显示
```

---

## 📈 完成度追踪

### Phase 1: 关键指标 (预计 70 分钟)
- [ ] 创建 `cost_calculator.py` (10 分钟)
- [ ] 升级 `llm_script_generator.py` (15 分钟)
- [ ] 升级 `audio_synthesizer.py` (15 分钟)
- [ ] 升级 `main.py` (20 分钟)
- [ ] 本地测试 (10 分钟)

### Phase 2: 下载优化 (预计 30 分钟)
- [ ] 升级 `gcs_utils.py` (10 分钟)
- [ ] 集成到 `main.py` (10 分钟)
- [ ] GCS 集成测试 (10 分钟)

### Phase 3: 前端集成 (预计 20 分钟)
- [ ] 测试所有新增字段
- [ ] 验证链接可点击下载
- [ ] 验证费用计算正确

**总计**: ~120 分钟 (2 小时)

---

## 📞 常见问题

### Q1: 如果没有升级后端，前端会发生什么？
A: 前端会正常工作，但会显示：
- `script_file_signed_url`: null（无下载链接）
- `token_usage`: 不显示
- `estimated_llm_cost_usd`: 不显示
- `estimated_tts_cost_usd`: 不显示或使用前端估算

### Q2: Signed URLs 的安全性如何？
A: 
- 有效期仅 1 小时，自动过期
- 包含加密签名，无法伪造
- 只允许 GET 请求（只读）
- 生成 URL 时需要 GCS 凭证（安全可控）

### Q3: 定价如何更新？
A: 当前定价硬编码在 `cost_calculator.py` 中。建议后续优化：
- 读取 `config/pricing.yaml` 配置文件
- 从 Google Cloud API 获取实时定价
- 支持按用户/项目配置不同定价

### Q4: Token 使用统计有延迟吗？
A: 无延迟。OpenAI API 响应中直接包含 `usage` 对象，实时返回。

### Q5: TTS 字符计数的准确性？
A: 
- 如果使用 SSML 格式，移除标签后计数（准确）
- Google 按实际字符计费，计数准确
- 建议定期与 GCS 账单对比验证

---

## 📚 相关文档

- `BACKEND_UPGRADE_PLAN.md` - 详细升级规划和架构
- `BACKEND_UPGRADE_PATCHES.md` - 代码补丁和具体改动
- `podcast_service/README.md` - API 文档和使用指南
- `podcast_service/generate_podcast_ui.html` - 前端 UI 代码

---

## 🎉 后续优化建议

1. **实时价格同步**: 从 Google Cloud 定价 API 获取最新定价
2. **成本预测**: 用户输入话题后预先估算成本
3. **成本预算**: 添加每个用户/请求的成本上限检查
4. **分析仪表板**: 记录历史成本数据用于分析和优化
5. **多语言定价**: 不同语言的 TTS 定价支持
6. **缓存优化**: 相同输入跳过重复生成，返回缓存结果
7. **成本分摊**: 支持按项目/用户统计成本

---

**最后更新**: 2025-10-22  
**版本**: v1.0  
**状态**: 📋 待实施
