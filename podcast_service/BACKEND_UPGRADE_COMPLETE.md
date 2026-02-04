# 后端升级完成 ✅

**完成时间**: 2025-10-22  
**所有代码实现完毕** - 无文档，纯代码！

---

## 📋 升级概览

后端已全面升级以配合前端新增的功能需求。所有改动已实现并通过语法检查。

### 升级的5个核心模块

| # | 模块 | 文件 | 功能 | 状态 |
|---|------|------|------|------|
| 1 | 成本计算器 | `cost_calculator.py` (新建) | 计算 LLM token 成本和 Google TTS 成本 | ✅ 完成 |
| 2 | 音频合成器 | `src/audio_synthesizer.py` | 返回 TTS 字符数、音频时长、文件大小 | ✅ 完成 |
| 3 | GCS 工具 | `src/gcs_utils.py` | 生成签名 URL (1h/24h/7天可选) | ✅ 完成 |
| 4 | 脚本生成器 | `src/llm_script_generator.py` | 保存 LLM token 使用统计 | ✅ 完成 |
| 5 | 主应用 | `main.py` | 集成所有数据，返回完整响应 | ✅ 完成 |

---

## 🔧 详细改动

### 1️⃣ 新建: `cost_calculator.py`

**目的**: 统一管理所有计费逻辑

```python
# 提供的类/函数:
- TokenPricing: LLM pricing 配置 (prompt/completion 分别定价)
- TTSPricing: Google TTS pricing 配置 (standard/neural/wavenet)
- UsageMetrics: 捕获的使用指标数据类
- CostBreakdown: 成本分解结果
- CostCalculator: 主计算器
  - calculate_llm_cost(prompt_tokens, completion_tokens) → USD
  - calculate_tts_cost(character_count, voice_type) → USD
  - calculate_total_cost(metrics, voice_type) → CostBreakdown
  - estimate_tts_characters_from_duration(duration_seconds) → int
```

**配置示例** (可在代码中调整):
```python
TokenPricing(
    prompt_tokens_per_1k=0.0005,        # $0.0005 per 1k prompt tokens
    completion_tokens_per_1k=0.0015     # $0.0015 per 1k completion tokens
)

TTSPricing(
    standard_per_1m_chars=4.0,          # $4 per 1M chars (standard voices)
    neural_per_1m_chars=16.0            # $16 per 1M chars (neural voices)
)
```

---

### 2️⃣ 升级: `src/audio_synthesizer.py`

**改变点**:

1. **返回值更新** (第一处):
   - `synthesize_segment()` 现在返回 `(audio_bytes, char_count)` 而非仅 `audio_bytes`
   - 在合成中统计 SSML 文本中的实际字符数（去掉标签）

2. **返回值更新** (第二处):
   - `generate_from_script()` 现在返回 `tuple(output_path, tts_chars, duration_sec, file_size_bytes)`
   - 之前仅返回 `Path`
   - 新返回的数据包括:
     - `tts_character_count`: TTS 处理的字符数
     - `audio_duration_seconds`: 实际音频时长（秒）
     - `audio_file_size_bytes`: 文件大小（字节）

**日志增强**:
```
[5/5] 合成 Speaker 2 (en-US-Neural2-F)
              ✅ 成功 (12.3s, 3456字符)  ← 新增字符数显示
...
   TTS字符数: 45678                        ← 新增总统计
   音频时长: 312.5秒                       ← 新增
```

---

### 3️⃣ 升级: `src/gcs_utils.py`

**新增方法**:

```python
@classmethod
def generate_signed_url(
    cls,
    bucket_name: str,
    blob_name: str,
    expiration_hours: int = 1,  # 1, 24, 或 168 (7天)
) -> str:
    """生成 V4 签名 URL（可下载）"""
    # 返回格式: https://storage.googleapis.com/bucket/path?signature=...&expiration=...
```

**支持的过期时间**:
- `1`: 1小时（演示/测试）
- `24`: 24小时（推荐生产使用）
- `168`: 7天（长期存档）

**错误处理**: 若过期时间不在上述之列，默认使用 1 小时并发出警告

---

### 4️⃣ 升级: `src/llm_script_generator.py`

**改变点**:

1. **数据模型更新**:
   ```python
   @dataclass
   class PodcastScript:
       ...
       token_usage: Optional[Dict[str, int]] = None
       # 格式: {
       #   'prompt_tokens': int,
       #   'completion_tokens': int,
       #   'total_tokens': int
       # }
   ```

2. **Token 累积**:
   - 初始生成时捕获 token 使用
   - 每次扩展时累加额外 token
   - 最后在返回前设置 `script.token_usage`

3. **日志输出**:
   ```
   📊 LLM Token 使用统计:
      Prompt tokens: 1234
      Completion tokens: 5678
      Total tokens: 6912
   ```

4. **保存方式**:
   - Token 统计保存到脚本 JSON 文件中（在 `token_usage` 字段）
   - 同时保存到 `metadata['usage']` 用于审计

---

### 5️⃣ 升级: `main.py`

**改变点**:

1. **导入新模块**:
   ```python
   from cost_calculator import CostCalculator, UsageMetrics
   ```

2. **响应模型增强** (GeneratePodcastResponse):
   ```python
   script_file_signed_url: Optional[str] = None  # Signed download link
   audio_file_signed_url: Optional[str] = None   # Signed download link
   audio_file_size_bytes: Optional[int] = None   # File size in bytes
   audio_duration_seconds: Optional[float] = None  # Actual audio duration
   token_usage: Optional[Dict[str, int]] = None  # LLM token stats
   tts_character_count: Optional[int] = None     # Characters processed by TTS
   cost_breakdown: Optional[Dict[str, float]] = None  # {llm_cost_usd, tts_cost_usd, total_cost_usd}
   ```

3. **新增端点逻辑** (POST /v4/generate):
   ```
   6️⃣ 生成音频 (返回 tuple 被解包)
   7️⃣ 生成 signed URLs
   8️⃣ 计算成本
   9️⃣ 组装完整响应
   🔟 返回
   ```

4. **新增日志**:
   ```
   ✅ 生成脚本签名 URL (24小时有效期)
   ✅ 生成音频签名 URL (24小时有效期)
   💰 成本估算 (使用 Neural TTS):
      LLM 成本: $0.012345
      TTS 成本: $0.054321
      总成本: $0.066666
   ```

---

## 📊 新的 POST /v4/generate 响应示例

```json
{
  "status": "success",
  "podcast_name": "english_4_panel_20251022_123456",
  "podcast_id": "podcast_20251022_123456",
  "topic": "AI 在医疗行业的应用",
  "style": "english_4_panel",
  "tone": "professional",
  "dialogue_style": "panel",
  "duration_minutes": 5,
  "language": "en-US",
  "num_speakers": 4,
  "script_file": "gs://my-bucket/generated_scripts/script.json",
  "script_file_signed_url": "https://storage.googleapis.com/my-bucket/generated_scripts/script.json?GoogleAccessId=...&Signature=...&Expires=...",
  "audio_file": "gs://my-bucket/generated_podcasts/podcast.mp3",
  "audio_file_signed_url": "https://storage.googleapis.com/my-bucket/generated_podcasts/podcast.mp3?GoogleAccessId=...&Signature=...&Expires=...",
  "audio_file_size_bytes": 5242880,
  "audio_duration_seconds": 312.5,
  "script_preview": {
    "title": "AI in Healthcare: Panel Discussion",
    "description": "...",
    "num_segments": 24,
    "estimated_duration_seconds": 310.0,
    "first_segment": {
      "speaker": "Dr. Sarah",
      "text": "Welcome everyone to our discussion on AI in healthcare..."
    }
  },
  "token_usage": {
    "prompt_tokens": 4500,
    "completion_tokens": 8200,
    "total_tokens": 12700
  },
  "tts_character_count": 45678,
  "cost_breakdown": {
    "prompt_cost_usd": 0.002250,
    "completion_cost_usd": 0.012300,
    "llm_total_cost_usd": 0.014550,
    "tts_cost_usd": 0.182712,
    "total_cost_usd": 0.197262
  },
  "message": "✅ 播客脚本生成成功! 包含 24 个段落，预计 310 秒。\n🎵 音频文件已生成: podcast_20251022_123456.mp3",
  "timestamp": "2025-10-22T12:34:56.789000",
  "generation_time_seconds": 45.3
}
```

---

## 🔌 前后端集成要点

### 前端已支持 ✅
- 显示脚本 GCS 路径和 gs:// → https 转换
- 显示 signed URLs 作为可点击下载链接
- 格式化显示 token 使用（总计/提示/完成）
- 计算并显示 LLM 成本（可配置每 1k token 价格）
- 计算并显示 TTS 成本（可配置每 100 万字符价格）
- 备用估算：若无 `tts_character_count`，用音频时长估算（15 chars/sec）

### 后端现在提供 ✅
- 所有上述字段的准确数据
- 自动生成 signed URLs（24小时有效）
- 准确的 TTS 字符数统计
- 准确的音频时长和文件大小
- 准确的 token 计数（包括扩展轮次的累积）
- 预计算的成本分解（可选）

---

## 🚀 部署检查清单

- [x] 所有 5 个文件已修改/创建
- [x] 所有 Python 文件通过语法检查
- [x] 新的数据类型定义清晰
- [x] 向后兼容（老客户端仍可工作，只是不会收到新字段）
- [x] 错误处理完善（signed URL 生成失败不会中断响应）
- [x] 日志清晰详细
- [x] 成本计算可配置

---

## 📝 配置调整

### 修改 LLM Token 价格

编辑 `main.py` 中的 `calculate_total_cost()` 调用前：

```python
calculator = CostCalculator(
    token_pricing=TokenPricing(
        prompt_tokens_per_1k=0.0001,      # 你的价格
        completion_tokens_per_1k=0.0003
    )
)
```

### 修改 TTS 价格

```python
calculator = CostCalculator(
    tts_pricing=TTSPricing(
        standard_per_1m_chars=4.0,
        neural_per_1m_chars=16.0,
        wavenet_per_1m_chars=16.0
    )
)
```

### 修改 Signed URL 过期时间

在 `main.py` 的 signed URL 生成处：

```python
script_signed_url = GCSUploader.generate_signed_url(
    bucket_name=bucket,
    blob_name=blob_path,
    expiration_hours=168  # 改为 1, 24, 或 168
)
```

---

## ✅ 验证步骤

### 本地测试
```bash
# 1. 检查语法
python3 -m py_compile cost_calculator.py main.py src/*.py

# 2. 导入检查
python3 -c "from cost_calculator import CostCalculator; print('✅ OK')"

# 3. 运行服务
python3 main.py
```

### API 测试
```bash
# POST /v4/generate 应该返回包含新字段的响应
curl -X POST http://localhost:8080/v4/generate \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "Test",
    "style_name": "english_2_hosts",
    "duration_minutes": 2,
    "generate_audio": true
  }'
```

### 前端测试
1. 打开 `generate_podcast_ui.html`
2. 填表提交请求
3. 验证响应中包含：
   - ✅ `script_file_signed_url` (clickable)
   - ✅ `audio_file_signed_url` (clickable)
   - ✅ `token_usage` (显示在 UI 中)
   - ✅ `tts_character_count`
   - ✅ `cost_breakdown` (用于前端成本显示)

---

## 🎯 接下来的步骤

1. **Docker 构建** (使用现有 Dockerfile)
   ```bash
   docker build -t podcast-service:v5 .
   ```

2. **部署到 Cloud Run**
   ```bash
   ./deploy_podcast_service.sh
   ```

3. **前端部署** (更新 UI endpoint 如需要)
   - 复制 `generate_podcast_ui.html` 到前端服务器/CDN

4. **监控** (检查日志)
   ```bash
   gcloud run logs read podcast-service
   ```

---

## 📞 故障排除

| 问题 | 解决方案 |
|------|---------|
| Import Error: `cost_calculator` | 确保 `cost_calculator.py` 在项目根目录 |
| Signed URL 生成失败 | 检查 GCS 服务账号权限，确保有 `storage.buckets.get` 权限 |
| Token 统计为 0 | 检查 LLM 响应中是否包含 `usage` 字段，某些模型可能不支持 |
| 音频生成返回旧格式 | 确保已替换 `src/audio_synthesizer.py` 的 `generate_from_script` 方法 |
| 前端显示 `-` (cost/tokens) | 可能响应中不包含对应字段，检查后端日志 |

---

## 📚 文件清单

### 新建
- `cost_calculator.py` (265 行)

### 已修改
- `main.py` (+80 行, import + 响应模型 + 成本计算 + signed URLs)
- `src/audio_synthesizer.py` (+2 处返回值修改)
- `src/llm_script_generator.py` (+1 处 token 保存)
- `src/gcs_utils.py` (+40 行 signed URL 方法)

### 无变化
- `src/podcast_pipeline.py`
- `src/env_config.py`
- `requirements.txt` (无新依赖)
- `Dockerfile`

---

**完成日期**: 2025-10-22  
**总代码行数增加**: ~425 行  
**向后兼容性**: ✅ 完全兼容
