# 📌 播客服务升级方案 - 执行摘要
## 2025-10-22

---

## 🎯 目标

实现播客服务的**前后端完整集成**，让用户能够：
1. ✅ 看到生成的脚本和音频的**可点击下载链接**
2. ✅ 看到**LLM Token 使用统计**和费用估算
3. ✅ 看到 **Google TTS 字符数和费用估算**
4. ✅ 理解每个播客生成的**成本构成**

---

## 📊 当前状态

### ✅ 前端：已完成

| 功能 | 状态 | 文件 |
|------|------|------|
| GCS 路径显示 | ✅ | `generate_podcast_ui.html` |
| 脚本下载链接 | ✅ | 自动转 gs:// → https |
| 音频下载链接 | ✅ | 自动转 gs:// → https |
| Token 统计显示 | ✅ | 读取 `token_usage` 字段 |
| LLM 成本计算 | ✅ | 用户可配置 $/1k tokens |
| TTS 成本计算 | ✅ | 用户可配置 $/1M chars |

**代码行数**: ~100 行新增 HTML/CSS + ~150 行新增 JS

---

### ❌ 后端：需要升级

| 功能 | 当前状态 | 需要的改动 |
|------|---------|----------|
| Token 追踪 | ❌ 不返回 | 从 LLM 响应提取 |
| TTS 字符计数 | ❌ 不计算 | 从脚本计算 |
| 成本计算 | ❌ 无 | 新增 cost_calculator 模块 |
| Signed URLs | ❌ 无 | GCSUploader 生成 |
| 响应模型 | ❌ 缺字段 | 添加 8 个新字段 |

---

## 🔧 后端升级工作清单

### Phase 1: 核心指标收集 (优先级: ⭐⭐⭐)

**工作量**: 70 分钟 | **影响**: 前端 token 和 LLM 成本显示

#### 1.1 创建成本计算模块
```python
# 新建文件: podcast_service/src/cost_calculator.py
def calculate_llm_cost(token_usage, model="gpt-4o-mini"):
    # GPT-4o-mini: input $0.15/1M, output $0.60/1M
    pass

def calculate_tts_cost(tts_metrics, engine="google-cloud"):
    # Google TTS: $4.00 / 1M characters
    pass
```

**估计时间**: 10 分钟  
**技能**: Python 基础

---

#### 1.2 升级 LLMScriptGenerator
**文件**: `podcast_service/src/llm_script_generator.py`

**改动**: 返回 token 使用信息

```python
# 当前
return script

# 改为
return script, token_usage  # tuple
```

**关键代码位置**: 第 ~250 行，在调用 OpenAI API 后

```python
# ✅ 从响应中提取
usage_dict = {
    'prompt_tokens': response.usage.prompt_tokens,
    'completion_tokens': response.usage.completion_tokens,
    'total_tokens': response.usage.total_tokens
}
```

**估计时间**: 15 分钟  
**技能**: Python 基础

---

#### 1.3 升级 AudioSynthesizer
**文件**: `podcast_service/src/audio_synthesizer.py`

**改动**: 添加方法返回 TTS 指标

```python
def generate_from_script_with_metrics(...):
    """返回 (output_path, tts_metrics)"""
    output_path = self.generate_from_script(...)
    tts_metrics = {
        'character_count': 计算脚本中所有字符数,
        'billable_seconds': 预计时长,
        'segments_count': 段落数
    }
    return output_path, tts_metrics
```

**关键代码**:
```python
# 计算字符数
total_chars = sum(
    len(remove_ssml_tags(seg.get('text', ''))) 
    for seg in script_data['segments']
)
```

**估计时间**: 15 分钟  
**技能**: Python 基础

---

#### 1.4 升级 main.py
**文件**: `podcast_service/main.py`

**改动 A**: 更新响应模型

```python
class GeneratePodcastResponse(BaseModel):
    # 既有字段 ...
    
    # ✅ 新增字段
    token_usage: Optional[Dict[str, int]] = None
    estimated_llm_cost_usd: Optional[float] = None
    tts_character_count: Optional[int] = None
    estimated_tts_cost_usd: Optional[float] = None
    script_file_signed_url: Optional[str] = None
    audio_file_signed_url: Optional[str] = None
```

**改动 B**: 在 `generate_podcast_v4()` 中收集指标

```python
# 1. 获取脚本 + token 信息
script, token_usage = script_generator.generate_script(...)  # 返回 tuple

# 2. 计算 LLM 成本
from src.cost_calculator import calculate_llm_cost
llm_cost = calculate_llm_cost(token_usage, script_generator.model)

# 3. 获取音频 + TTS 指标
output_path, tts_metrics = synthesizer.generate_from_script_with_metrics(...)

# 4. 计算 TTS 成本
from src.cost_calculator import calculate_tts_cost
tts_cost = calculate_tts_cost(tts_metrics)

# 5. 构建响应
response = GeneratePodcastResponse(
    # ... 既有字段 ...
    token_usage=token_usage,
    estimated_llm_cost_usd=llm_cost,
    tts_character_count=tts_metrics['character_count'],
    estimated_tts_cost_usd=tts_cost,
)
```

**估计时间**: 20 分钟  
**技能**: Python FastAPI 基础

---

#### 1.5 本地测试
```bash
# 启动服务
export OPENAI_API_KEY="sk-..."
python podcast_service/main.py

# 测试请求
curl -X POST http://localhost:8080/v4/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "topic": "AI发展",
    "duration_minutes": 2,
    "generate_audio": false
  }'

# 验证返回
# ✅ token_usage 不为空
# ✅ estimated_llm_cost_usd > 0
# ✅ generation_time_seconds 合理
```

**估计时间**: 10 分钟

---

### Phase 2: 下载优化 (优先级: ⭐⭐)

**工作量**: 30 分钟 | **影响**: 前端下载链接可点击

#### 2.1 升级 GCSUploader
**文件**: `podcast_service/src/gcs_utils.py`

**改动**: 返回 signed URLs

```python
# 当前
return f"gs://bucket/path"

# 改为
return gs_uri, signed_url
# 其中 signed_url 是 https://storage.googleapis.com/... 格式
```

**关键代码**:
```python
from datetime import timedelta
from google.cloud import storage

blob = bucket.blob(destination_path)
blob.upload_from_filename(local_path)

signed_url = blob.generate_signed_url(
    version="v4",
    expiration=timedelta(hours=1),
    method="GET"
)

return f"gs://bucket/path", signed_url
```

**前置条件**:
- `GOOGLE_APPLICATION_CREDENTIALS` 环境变量已配置
- 服务账号有 `storage.objects.get` 权限

**估计时间**: 10 分钟  
**技能**: Python 基础 + GCS API

---

#### 2.2 集成到 main.py
**改动**: 调用方改为处理 tuple

```python
# 当前
script_uri = GCSUploader.upload_file(...)

# 改为
script_uri, script_signed_url = GCSUploader.upload_file(...)
```

**两个位置需要改**:
- 脚本上传 (~第 450-465 行)
- 音频上传 (~第 490-510 行)

**估计时间**: 10 分钟

---

#### 2.3 GCS 集成测试
```bash
# 设置环境变量
export GCS_BUCKET_NAME="my-podcast-bucket"
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account.json"

# 部署到 Cloud Run
gcloud builds submit --config cloudbuild.yaml

# 测试
curl -X POST https://podcast-service-xxx.run.app/v4/generate \
  -H 'Content-Type: application/json' \
  -d '{"topic": "AI", "generate_audio": true}'

# 验证
# ✅ script_file_signed_url 包含 https:// URL
# ✅ audio_file_signed_url 包含 https:// URL
# ✅ 能在浏览器中直接点击下载
```

**估计时间**: 10 分钟

---

## 📋 完整改动清单

### 新建文件

```
podcast_service/src/cost_calculator.py (180 行)
```

### 修改文件

| 文件 | 改动数 | 复杂度 |
|------|--------|--------|
| `src/llm_script_generator.py` | 1 处 | ⭐ |
| `src/audio_synthesizer.py` | +1 新方法 | ⭐ |
| `src/gcs_utils.py` | 1 处修改 | ⭐⭐ |
| `main.py` | 6 处 | ⭐⭐ |

**总行数**: ~250 行新增 + ~80 行修改

---

## 🚀 实施路线图

### Week 1: Phase 1 核心指标
- **Day 1-2**: 创建 `cost_calculator.py` + 本地测试
- **Day 3-4**: 升级 LLM token 跟踪
- **Day 5**: 升级 TTS 指标收集
- **Day 6-7**: main.py 整合 + 本地测试

**里程碑**: 前端能显示 token 和 LLM 成本

---

### Week 2: Phase 2 下载优化
- **Day 1-2**: 升级 GCSUploader signed URLs
- **Day 3-4**: main.py 集成
- **Day 5-7**: Cloud Run 部署测试

**里程碑**: 前端下载链接可点击，完整功能上线

---

## 💡 关键技术点

### 1. Token 追踪

OpenAI SDK 返回的响应对象包含 `usage`:

```python
response = client.chat.completions.create(...)

# ✅ 获取 usage
response.usage.prompt_tokens       # 输入 tokens
response.usage.completion_tokens   # 输出 tokens
response.usage.total_tokens        # 总计
```

**注意**: 仅在 API 调用完成后才可用

---

### 2. TTS 字符计数

需要清理 SSML 标签后计数:

```python
import re

ssml_text = "<speak>Hello <break time=\"500ms\"/> world</speak>"
clean_text = re.sub(r'<[^>]+>', '', ssml_text)
char_count = len(clean_text)  # 11 (not 61)
```

---

### 3. Signed URLs 有效期

```python
from datetime import timedelta

# 生成有效期 1 小时的 signed URL
signed_url = blob.generate_signed_url(
    version="v4",
    expiration=timedelta(hours=1),
    method="GET"
)

# 过期后会 403，无法下载
```

---

### 4. 定价更新策略

当前硬编码在代码中，后续优化建议：

```yaml
# config/pricing.yaml
pricing:
  llm:
    gpt-4o-mini:
      input: 0.00000015   # $/token
      output: 0.00000060
  tts:
    google-cloud: 0.000004  # $/character
```

---

## ✅ 验收标准

### Phase 1 完成标准

- [ ] `cost_calculator.py` 创建且测试通过
- [ ] `llm_script_generator` 返回 token_usage
- [ ] `AudioSynthesizer` 返回 tts_metrics
- [ ] `GeneratePodcastResponse` 包含 8 个新字段
- [ ] `main.py` 正确收集和计算所有指标
- [ ] 本地测试返回正确的 token 和成本数据
- [ ] 前端显示 token 统计和 LLM 成本

### Phase 2 完成标准

- [ ] `GCSUploader` 返回 signed URLs
- [ ] `main.py` 正确处理 signed URLs
- [ ] Cloud Run 部署成功
- [ ] Signed URLs 在浏览器中可点击并下载文件
- [ ] Signed URLs 1 小时后过期
- [ ] 前端显示完整的下载链接

---

## 🔍 可能的问题和解决方案

### 问题 1: `response.usage` 为 None
**原因**: OpenAI SDK 版本过低或配置问题  
**解决**: 更新 OpenAI SDK 到最新版本
```bash
pip install --upgrade openai
```

---

### 问题 2: Signed URL 生成失败
**原因**: GCS 凭证缺失或权限不足  
**解决**: 检查 `GOOGLE_APPLICATION_CREDENTIALS`
```bash
# 验证凭证
gcloud auth list
gcloud config get-value account
```

---

### 问题 3: 成本计算和实际账单不符
**原因**: 定价可能已更新  
**解决**: 与 Google Cloud 定价页面对比
- LLM: https://openai.com/api/pricing/
- TTS: https://cloud.google.com/text-to-speech/pricing

---

### 问题 4: 前端显示 NaN 或 -
**原因**: 后端未返回相应字段  
**解决**: 检查响应 JSON 是否包含 token_usage, tts_metrics 等

---

## 📚 参考资源

1. **OpenAI API 文档**
   - Token 计数: https://platform.openai.com/docs/guides/tokens
   - 定价: https://openai.com/pricing

2. **Google Cloud 文档**
   - Signed URLs: https://cloud.google.com/storage/docs/access-control/signing-urls-with-helpers
   - TTS 定价: https://cloud.google.com/text-to-speech/pricing

3. **本项目文档**
   - `BACKEND_UPGRADE_PLAN.md` - 详细设计
   - `BACKEND_UPGRADE_PATCHES.md` - 代码补丁
   - `FRONTEND_BACKEND_INTEGRATION_GUIDE.md` - 集成指南

---

## 📞 需要帮助?

1. **代码问题**: 参考 `BACKEND_UPGRADE_PATCHES.md` 的补丁
2. **API 问题**: 检查 OpenAI/GCS 官方文档
3. **部署问题**: 查看 Cloud Run 日志
   ```bash
   gcloud run logs podcast-service --limit=50
   ```

---

**状态**: 📋 就绪等待实施  
**创建日期**: 2025-10-22  
**版本**: v1.0
