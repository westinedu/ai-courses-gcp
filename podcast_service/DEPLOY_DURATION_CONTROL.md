# Podcast Service 时长控制功能部署指南

## 📋 更新内容

本次更新添加了精确的时长控制功能，确保 YouTube Shorts 等短视频音频严格控制在目标时长内。

## 🚀 部署步骤

### 1. 代码变更文件

已修改的文件：
- `main.py` - 集成时长控制逻辑
- `src/audio_synthesizer.py` - 支持动态语速
- `src/duration_control.py` - 新增时长控制模块（新文件）

### 2. 依赖检查

确保 `requirements.txt` 包含以下依赖：
```
pydub>=0.25.1
```

已包含，无需修改。

### 3. 部署到 Cloud Run

```bash
# 1. 进入项目目录
cd /path/to/podcast_service

# 2. 部署到 Cloud Run
gcloud run deploy podcast-service \
  --source . \
  --region us-central1 \
  --platform managed \
  --allow-unauthenticated

# 或使用 deploy 脚本
chmod +x deploy_podcast_service.sh
./deploy_podcast_service.sh
```

### 4. 验证部署

```bash
# 测试生成 45 秒 YouTube Shorts
curl -X POST https://your-podcast-service-url/v4/generate \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "AAPL 技术分析",
    "source_content": "Apple股票当前信号BUY，置信度42%。RSI 57处于中性区域，MACD显示多头动能减弱。",
    "style_name": "chinese_2_hosts",
    "duration_minutes": 1,
    "max_words": 120,
    "language": "cmn-CN",
    "generate_audio": true
  }'
```

## 📊 功能说明

### 时长控制三级策略

```
┌─────────────────────────────────────────────────────────┐
│  1️⃣ Prompt 约束（预防）                                  │
│     - 强制字数限制（中文 ~140 字/45秒）                   │
│     - 禁止寒暄和过渡句                                    │
│     - 要求开门见山                                        │
├─────────────────────────────────────────────────────────┤
│  2️⃣ TTS 语速调整（缓解）                                 │
│     - 字数稍多：1.2x 语速                                │
│     - 字数多很多：1.5-1.8x 语速                          │
│     - Google Cloud TTS speaking_rate 参数                │
├─────────────────────────────────────────────────────────┤
│  3️⃣ 音频截断（保底）                                     │
│     - 超过目标时长自动截断                                │
│     - 添加 1.5 秒淡出效果                                 │
└─────────────────────────────────────────────────────────┘
```

### API 参数

| 参数 | 类型 | 说明 |
|:---|:---|:---|
| `duration_minutes` | int | 目标时长（分钟），shorts 建议 1 |
| `max_words` | int | 最大字数限制（可选）|
| `language` | str | 语言代码，如 "cmn-CN" |

### 语言语速配置

| 语言 | 正常语速 | 45秒最大字数（含15%余量）|
|:---|:---|:---|
| 中文 (cmn-CN) | 220 字/分钟 | ~140 字 |
| 日文 (ja-JP) | 200 字/分钟 | ~127 字 |
| 韩文 (ko-KR) | 210 字/分钟 | ~133 字 |
| 英文 (en-US) | 140 词/分钟 | ~89 词 |

## 🧪 测试

```bash
# 运行测试脚本
python3 test_duration_control.py
```

## 🔧 故障排除

### 音频截断不生效

检查 `pydub` 是否安装：
```bash
pip install pydub
```

### TTS 语速不生效

检查 Google Cloud TTS speaking_rate 参数范围：-1.0 到 1.0

### 字数统计不准确

中日韩使用 `unicodedata.category()` 识别标点，英文使用空格分词。

## 📈 效果预期

| 场景 | 之前 | 之后 |
|:---|:---|:---|
| 45秒 Shorts | 经常超时 60+ 秒 | 严格控制在 45 秒内 |
| 1分钟播客 | 经常 80+ 秒 | 控制在 60 秒内 |
| 超长脚本 | 音频被截断（突兀）| 先压缩再生成（自然）|

## 📝 后续优化建议

1. **LLM 压缩**：当字数超限时，调用 LLM 自动压缩脚本
2. **字数预估**：生成前预估字数，超限时提前警告
3. **音频波形**：截断时检测句子边界，避免截断在句子中间
