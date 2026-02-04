# 🎙️ AI播客生成服务 - Production Build

## 📖 简介

这是一个基于OpenAI GPT和Google Cloud TTS的**AI播客生成服务**，可以将任何新闻文章或文本内容转换成多语言、多人物的专业播客。

**核心特性：**
- ✅ 基于真实内容生成（不会编造信息）
- ✅ 支持8种播客风格模板（英文、中文、韩文、日文、双语）
- ✅ 多讲话人对话生成
- ✅ 真人语音合成（Google Cloud TTS）
- ✅ MP3音频输出
- ✅ 完整的Token追踪和成本分析

## 🚀 快速开始

### 本地运行

```bash
# 1. 克隆或进入项目
cd podcast_service

# 2. 创建虚拟环境
python -m venv venv
source venv/bin/activate  # macOS/Linux
# 或 venv\Scripts\activate  # Windows

# 3. 安装依赖
pip install -r requirements.txt

# 4. 设置环境变量
cp .env.example .env
# 编辑.env文件，填入您的API密钥

# 5. 运行服务
uvicorn main:app --host 0.0.0.0 --port 8080
```

### Docker运行

```bash
# 1. 构建镜像
docker build -t podcast-service .

# 2. 运行容器
docker run -e OPENAI_API_KEY=sk-xxx \
  -e GOOGLE_APPLICATION_CREDENTIALS=/app/key.json \
  -p 8080:8080 \
  podcast-service
```

### Cloud Run部署

```bash
# 详见 CLOUD_RUN_DEPLOY.md
```

## 📚 API使用

### 访问Web界面

```
http://localhost:8080
```

### API文档

```
http://localhost:8080/docs     # Swagger UI
http://localhost:8080/redoc    # ReDoc
```

### 生成播客

**请求：**
```bash
curl -X POST http://localhost:8080/v4/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "topic": "如何识别山寨币市场操纵",
    "style_name": "english_4_panel",
    "tone": "professional",
    "dialogue_style": "conversation",
    "duration_minutes": 5,
    "source_content": "Paste your news content here...",
    "generate_audio": false
  }'
```

**响应：**
```json
{
  "status": "success",
  "script_file": "data/generated_scripts/podcast_xxx.json",
  "audio_file": "data/generated_podcasts/podcast_xxx.mp3",
  "script_preview": {
    "title": "播客标题",
    "num_segments": 16,
    "estimated_duration_seconds": 300.0
  },
  "token_usage": {
    "prompt_tokens": 1500,
    "completion_tokens": 2000,
    "total_tokens": 3500
  }
}
```

## 📋 支持的模板

| 模板 | 讲话人 | 语言 | 时长 |
|------|--------|------|------|
| `english_2_hosts` | 2人 | 🇺🇸 | 5min |
| `english_3_experts` | 3人 | 🇺🇸 | 8min |
| `english_4_panel` | 4人 | 🇺🇸 | 10min |
| `korean_2_hosts` | 2人 | 🇰🇷 | 5min |
| `korean_3_experts` | 3人 | 🇰🇷 | 8min |
| `chinese_2_hosts` | 2人 | 🇨🇳 | 5min |
| `japanese_4_panel` | 4人 | 🇯🇵 | 8min |
| `english_korean_bilingual` | 2人 | 🇺🇸🇰🇷 | 6min |

详见 `TEMPLATES_GUIDE.md`

## 🔧 环境变量

```env
# 必需
OPENAI_API_KEY=sk-xxxxxxxxxxxxx
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account-key.json

# 可选
ENVIRONMENT=production
LOG_LEVEL=INFO
PORT=8080
MAX_WORKERS=4
REQUEST_TIMEOUT=300
```

## 📁 项目结构

```
podcast_service/
├── main.py                      # FastAPI应用入口
├── src/
│   ├── llm_script_generator.py # LLM脚本生成
│   ├── audio_synthesizer.py     # 音频合成
│   ├── podcast_pipeline.py      # 完整流程
│   └── settings.py              # 配置管理
├── config/
│   └── podcast_style_templates.yaml  # 风格模板
├── data/
│   ├── generated_scripts/       # 生成的脚本JSON
│   └── generated_podcasts/      # 生成的MP3音频
├── requirements.txt             # Python依赖
├── Dockerfile                   # Docker配置
├── .dockerignore                # Docker忽略规则
├── .env.example                 # 环境变量模板
├── CLOUD_RUN_DEPLOY.md         # Cloud Run部署指南
└── README.md                    # 本文件
```

## 💰 成本估算

使用gpt-4o-mini + Google Cloud TTS：

| 场景 | Token数 | OpenAI成本 | TTS成本 | 总成本 |
|------|---------|-----------|--------|--------|
| 5分钟播客 | 3,500 | $0.012 | $0.08 | $0.092 |
| 10分钟播客 | 6,000 | $0.020 | $0.16 | $0.180 |

**按月预估（1000个播客）：**
- OpenAI: ~$12
- Google TTS: ~$80
- Cloud Run: ~$20
- **总计: ~$112/月**

## 🔍 监控和日志

### 本地日志
```bash
# 查看实时日志
tail -f logs/podcast_service.log

# 分析错误
grep ERROR logs/podcast_service.log
```

### Cloud Run监控
```bash
# 查看日志
gcloud run services logs read podcast-service --limit 50

# 查看指标
# https://console.cloud.google.com/run/detail/us-central1/podcast-service
```

## 🐛 故障排查

### 问题：API调用超时
```
解决方案：增加timeout，或检查网络连接
```

### 问题：Token不足
```
解决方案：检查OpenAI配额和余额
```

### 问题：Google TTS错误
```
解决方案：确认服务账户密钥有效，且启用了Text-to-Speech API
```

## 📖 完整文档

- **部署指南**: `CLOUD_RUN_DEPLOY.md`
- **模板说明**: `TEMPLATES_GUIDE.md`
- **使用指南**: `USAGE_GUIDE.md`
- **常见问题**: `FAQ.md` (如存在)

## 🔐 安全提示

### 1. API密钥保护
- ✅ 使用Secret Manager存储敏感信息
- ✅ 不要在代码中硬编码密钥
- ✅ 定期轮换密钥

### 2. 请求验证
- ✅ 实施速率限制
- ✅ 验证输入内容
- ✅ 记录所有API调用

### 3. 数据安全
- ✅ 使用HTTPS传输
- ✅ 加密存储敏感数据
- ✅ 定期清理过期文件

## 📞 支持

如有问题，请：
1. 查看日志和错误信息
2. 检查API配额和余额
3. 查看相关文档
4. 联系技术支持

## 📄 许可证

MIT License

## 👥 贡献

欢迎提交Issue和Pull Request！

---

**准备好了？** [开始部署到Cloud Run](CLOUD_RUN_DEPLOY.md)
