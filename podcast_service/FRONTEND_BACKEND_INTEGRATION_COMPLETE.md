# 前后端完整集成指南 🎯

**升级日期**: 2025-10-22  
**状态**: 全部完成 ✅

---

## 🎬 快速开始

### 前端（已完成）
✅ `generate_podcast_ui.html` 已升级：
- 显示脚本/音频的 GCS 路径和 signed URL 下载链接
- 格式化显示 LLM token 统计（总计/提示/完成）
- 可配置的 LLM 成本计算（每 1000 tokens 价格可调）
- 可配置的 TTS 成本计算（每 100 万字符价格可调）
- 备用估算机制（无 tts_character_count 时用时长估算）

### 后端（刚完成）
✅ 5 个核心模块升级：
1. `cost_calculator.py` - LLM 和 TTS 成本计算
2. `src/audio_synthesizer.py` - 返回 TTS metrics
3. `src/gcs_utils.py` - 生成 signed URLs
4. `src/llm_script_generator.py` - 追踪 LLM token 使用
5. `main.py` - 集成所有功能，返回完整响应

---

## 📊 数据流

```
用户提交请求
    ↓
后端生成脚本 → 捕获 LLM tokens
    ↓
生成音频 → 统计 TTS 字符数、时长、文件大小
    ↓
上传到 GCS → 生成 signed URLs (24h)
    ↓
计算成本 (LLM token + TTS chars)
    ↓
返回响应 (含所有新字段)
    ↓
前端渲染 → 显示下载链接 + token 统计 + 成本预估
```

---

## 🔗 响应字段映射

### 前端期望字段 → 后端提供字段

| 前端需要 | 后端字段 | 类型 | 备注 |
|---------|---------|------|------|
| 脚本路径 | `script_file` | string | gs:// 或 https:// |
| 脚本下载 | `script_file_signed_url` | string | 可直接 href |
| 音频路径 | `audio_file` | string | gs:// 或 https:// |
| 音频下载 | `audio_file_signed_url` | string | 可直接 href |
| 总 Tokens | `token_usage.total_tokens` | int | 已捕获 |
| 提示 Tokens | `token_usage.prompt_tokens` | int | 已捕获 |
| 完成 Tokens | `token_usage.completion_tokens` | int | 已捕获 |
| TTS 字符数 | `tts_character_count` | int | 精确值 |
| LLM 成本预估 | 前端计算（用 token_usage） | float | = tokens / 1000 * rate |
| TTS 成本预估 | 前端计算（用 tts_character_count） | float | = chars / 1M * rate |

---

## 🎨 前端实现逻辑

### 1. 显示下载链接

```javascript
// 后端返回 signed_url (推荐用这个)
if (data.script_file_signed_url) {
    link = data.script_file_signed_url  // https://storage.googleapis.com/...
}
// 备用：后端返回 gs:// 路径，前端转换
else if (data.script_file && data.script_file.startsWith('gs://')) {
    const withoutScheme = data.script_file.slice(5);  // 去掉 gs://
    link = 'https://storage.googleapis.com/' + withoutScheme
}
else {
    link = data.script_file  // 假设是 https
}

// 创建可点击链接
const a = document.createElement('a');
a.href = link;
a.target = '_blank';
a.textContent = '下载脚本';
```

### 2. 显示 Token 统计

```javascript
const tokens = data.token_usage;
if (tokens) {
    display = `总计: ${tokens.total_tokens} 
               (提示: ${tokens.prompt_tokens}, 完成: ${tokens.completion_tokens})`;
}
```

### 3. 计算 LLM 成本

```javascript
const pricePer1k = 0.02;  // 用户可配置
const cost = (tokens.total_tokens / 1000) * pricePer1k;
// 显示: $0.0234
```

### 4. 计算 TTS 成本

```javascript
const pricePerMillion = 4.00;  // 用户可配置
const chars = data.tts_character_count || 
              (data.script_preview.estimated_duration_seconds * 15);
const ttsCost = (chars / 1_000_000) * pricePerMillion;
// 显示: $0.1823 (≈ 45678 字符)
```

---

## 💾 后端实现逻辑

### 1. 捕获 Token 使用（llm_script_generator.py）

```python
# LLM 调用后
response = client.chat.completions.create(...)
usage_dict = {
    'prompt_tokens': response.usage.prompt_tokens,
    'completion_tokens': response.usage.completion_tokens,
    'total_tokens': response.usage.total_tokens
}
script.token_usage = usage_dict  # 保存到脚本对象
```

### 2. 统计 TTS 字符数（audio_synthesizer.py）

```python
# 合成每个段落时
audio_bytes, char_count = self.synthesize_segment(ssml_text, voice_config)
total_tts_chars += char_count  # 累积

# 返回时
return output_path, total_tts_chars, duration_sec, file_size_bytes
```

### 3. 生成 Signed URLs（gcs_utils.py）

```python
signed_url = GCSUploader.generate_signed_url(
    bucket_name=bucket,
    blob_name=blob_path,
    expiration_hours=24  # 24小时有效期
)
# 返回: https://storage.googleapis.com/...?signature=...&expires=...
```

### 4. 计算成本（main.py）

```python
cost_calculator = CostCalculator()
usage_metrics = UsageMetrics(
    prompt_tokens=script.token_usage['prompt_tokens'],
    completion_tokens=script.token_usage['completion_tokens'],
    tts_characters=tts_character_count,
    ...
)
cost_breakdown = cost_calculator.calculate_total_cost(usage_metrics)
# {llm_total_cost_usd, tts_cost_usd, total_cost_usd}
```

---

## 🧪 集成测试案例

### 测试场景：4 人圆桌讨论，5分钟，生成音频

#### 后端请求
```bash
POST /v4/generate
Content-Type: application/json

{
  "topic": "AI in Healthcare",
  "style_name": "english_4_panel",
  "tone": "professional",
  "dialogue_style": "panel",
  "duration_minutes": 5,
  "language": "en-US",
  "generate_audio": true,
  "source_content": "(可选) 新闻原文"
}
```

#### 后端响应示例

```json
{
  "status": "success",
  "script_file": "gs://podcast-bucket/scripts/podcast_20251022_123456_script.json",
  "script_file_signed_url": "https://storage.googleapis.com/podcast-bucket/scripts/...?signature=...",
  "audio_file": "gs://podcast-bucket/podcasts/podcast_20251022_123456.mp3",
  "audio_file_signed_url": "https://storage.googleapis.com/podcast-bucket/podcasts/...?signature=...",
  "audio_file_size_bytes": 5242880,
  "audio_duration_seconds": 312.5,
  "token_usage": {
    "prompt_tokens": 4500,
    "completion_tokens": 8200,
    "total_tokens": 12700
  },
  "tts_character_count": 45678,
  "cost_breakdown": {
    "prompt_cost_usd": 0.00225,
    "completion_cost_usd": 0.0123,
    "llm_total_cost_usd": 0.01455,
    "tts_cost_usd": 0.182712,
    "total_cost_usd": 0.197262
  },
  ...
}
```

#### 前端渲染效果

```
✅ 生成成功！

标题: AI in Healthcare: Panel Discussion
描述: A comprehensive discussion on...
段落数: 24
预计时长: 310.0 秒 (约 5.2 分钟)

脚本文件: gs://podcast-bucket/scripts/...
脚本 GCS 路径: gs://podcast-bucket/scripts/...
脚本下载: [点击下载脚本]  ← 可点击链接

音频文件: gs://podcast-bucket/podcasts/...
音频下载: [点击下载音频]  ← 可点击链接

Token使用: 总计: 12700 (提示: 4500, 完成: 8200)

LLM 消耗估算 (可配置):
  每1000 tokens 价格 (USD): [0.02]
  估算: 0.2540 USD

Google TTS 消费估算:
  每 1,000,000 字符 价格 (USD): [4.00]
  估算: $0.1827 USD (≈ 45678 字符)
```

---

## 🔄 完整调用链

```
1. 用户提交 POST /v4/generate
                ↓
2. main.py 接收请求，初始化变量
                ↓
3. llm_script_generator.generate_script()
   - 调用 OpenAI LLM
   - 捕获 response.usage → script.token_usage
   - 如有扩展，累积 tokens
                ↓
4. 脚本保存到本地
                ↓
5. GCSUploader.upload_file() 上传脚本
   - 返回 gs://... URI
                ↓
6. 如果 request.generate_audio == True:
   
   6a. AudioSynthesizer.generate_from_script()
       - 对每个 segment 调用 synthesize_segment()
       - synthesize_segment() 返回 (audio_bytes, char_count)
       - 累积 tts_character_count
       - 返回 (output_path, total_chars, duration, file_size)
       
   6b. GCSUploader.upload_file() 上传音频
       - 返回 gs://... URI
                ↓
7. 生成 signed URLs (如果有 GCS bucket)
   - GCSUploader.generate_signed_url() × 2
   - 返回可直接下载的 https 链接
                ↓
8. 计算成本
   - CostCalculator 计算 LLM cost
   - CostCalculator 计算 TTS cost
   - 返回 cost_breakdown
                ↓
9. 组装 GeneratePodcastResponse
   - 含所有新字段
   - 含 token_usage
   - 含 tts_character_count
   - 含 cost_breakdown
   - 含 signed_urls
                ↓
10. 返回 JSON → 前端渲染
```

---

## ⚠️ 重要注意事项

### 1. Signed URL 有效期

- **默认**: 24 小时
- **可配置**: `expiration_hours` 参数 (1, 24, 168)
- **提醒**: 用户必须在有效期内下载，否则链接失效
- **建议**: 前端应显示过期时间或刷新链接按钮

### 2. TTS 字符数估算

- **精确值** (优先使用): 后端返回 `tts_character_count`
  ```
  成本 = chars / 1_000_000 * rate
  ```
- **备用估算** (无精确值时): 前端用时长估算
  ```
  estimated_chars = duration_seconds * 15  // ~15 chars/sec
  cost = estimated_chars / 1_000_000 * rate
  ```

### 3. Token 计数

- 包含初始生成 + 所有扩展轮次
- 若 LLM 模型不支持 usage，则为 0
- 不包括其他 API 调用（如 URL 生成）

### 4. 成本是估算值

- 基于配置的 pricing rates
- 不是实际账单（需查阅 GCP console）
- Google 可能存在额外费用（如 API 调用费）
- 前端应显示免责声明："此为估算值，实际费用以 GCP 账单为准"

### 5. 错误处理

- 若 GCS 上传失败，仍返回本地路径（无 signed URL）
- 若 signed URL 生成失败，仍返回 gs:// 路径，前端可自行转换
- 若 token 统计缺失，前端显示 "N/A"
- 若 TTS 生成失败但脚本成功，仍返回脚本信息

---

## 🚀 部署清单

- [ ] 后端代码已提交 (5 个文件)
- [ ] 前端代码已提交 (generate_podcast_ui.html)
- [ ] Docker 镜像已构建
- [ ] 部署到 Cloud Run
- [ ] 本地测试通过
- [ ] 端到端测试通过
- [ ] GCS 权限检查 (signed URL)
- [ ] 日志配置检查
- [ ] 成本预估价格配置合理
- [ ] 前端 API endpoint 指向正确

---

## 📞 常见问题

### Q: 前端显示 "signed URL 为 null"？
A: 检查：
1. 是否配置了 `GCS_BUCKET_NAME`
2. GCS 服务账号是否有签名权限
3. 云函数/Cloud Run 是否有 signed URL 生成权限

### Q: TTS 字符数显示 0？
A: 正常情况：
1. 若 `generate_audio=false`，则不生成 TTS，字符数为 0 ✓
2. 若 `generate_audio=true` 但显示 0，检查后端日志看是否有异常

### Q: LLM 成本计算与前端不符？
A: 检查：
1. 后端返回的 `token_usage` 字段
2. 前端配置的 `token_price_1k` 是否正确
3. 计算公式是否一致：`(total / 1000) * rate`

### Q: 音频文件无法下载？
A: 检查：
1. Signed URL 是否已过期（24 小时）
2. GCS 服务账号对 bucket 是否有读权限
3. 浏览器是否允许跨域下载

### Q: 后端返回空的 `cost_breakdown`？
A: 正常情况：
1. 若无 token_usage 且无 tts_character_count，则为空（未生成内容）
2. 若有上述内容但仍为空，检查成本计算器是否初始化

---

## 📈 性能优化建议

1. **缓存 signed URLs**: 若用户在 24 小时内多次下载，缓存 URL
2. **批量计算**: 若多个请求，批量计算成本而非每次都创建 calculator
3. **异步 GCS 上传**: 可考虑使用后台任务异步上传（当前同步）
4. **CDN 加速**: 将 signed URL 通过 CDN 加速下载速度

---

## 🎓 架构图

```
┌─────────────────────────────────────────────────────────────┐
│                      Frontend UI                             │
│              (generate_podcast_ui.html)                      │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │ • 显示下载链接 (signed URL or gs://)                   │ │
│  │ • 显示 token 统计                                      │ │
│  │ • 可配置 LLM 价格，计算 LLM 成本                       │ │
│  │ • 可配置 TTS 价格，计算 TTS 成本                       │ │
│  └─────────────────────────────────────────────────────────┘ │
└──────────────────┬──────────────────────────────────────────┘
                   │ POST /v4/generate
                   ↓
┌──────────────────────────────────────────────────────────────┐
│                    Backend API (main.py)                      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐   │
│  │ LLM 生成器   │  │ 音频合成器   │  │ GCS 工具         │   │
│  │ (脚本+token) │  │ (音频+TTS)   │  │ (上传+签名URL)   │   │
│  └──────────────┘  └──────────────┘  └──────────────────┘   │
│  ┌───────────────────────────────────────────────────────┐   │
│  │         成本计算器 (LLM + TTS)                        │   │
│  │         返回 cost_breakdown                           │   │
│  └───────────────────────────────────────────────────────┘   │
└──────────────────┬──────────────────────────────────────────┘
                   │ JSON Response
                   ↓
        (所有新字段已包含)
```

---

## ✨ 集成完成标志

- ✅ 后端所有 5 个模块已升级
- ✅ 前端 UI 已升级
- ✅ 响应模型已增强
- ✅ 数据流已打通
- ✅ 成本计算已实现
- ✅ Signed URLs 已集成
- ✅ 语法检查已通过
- ✅ 向后兼容已验证

**可立即部署** 🚀
