# 🎯 Podcast Service - 快速参考卡

## ⚡ 一键部署

```bash
cd podcast_service
./deploy_podcast_service.sh
```

## 📁 关键文件位置

| 文件 | 位置 | 用途 |
|------|------|------|
| **Web UI** | `generate_podcast_ui.html` | 浏览器界面 |
| **FastAPI 应用** | `main.py` | HTTP 服务 |
| **CLI 工具** | `generate_from_news.py` | 命令行 |
| **LLM 脚本生成** | `src/llm_script_generator.py` | 脚本生成 |
| **音频合成** | `src/audio_synthesizer.py` | 语音合成 |
| **API 密钥** | `.env` 🔐 | 认证 |
| **GCP 密钥** | `*.json` 🔐 | Google Cloud |
| **部署脚本** | `deploy_podcast_service.sh` | 云部署 |
| **模板** | `config/podcast_style_templates.yaml` | 播客样式 |

## 📚 文档指南

```
1. README.md                   ← 开始这里 (快速概览)
   └─> QUICK_START_DEPLOY.md   ← 然后这里 (部署步骤)
       └─> CLOUD_RUN_DEPLOY.md ← 详细说明
```

## 🚀 三种使用方式

### 1️⃣ Web UI (推荐用户)
```
访问: http://localhost:8080
用途: 通过浏览器直接生成播客
```

### 2️⃣ REST API (推荐集成)
```bash
curl -X POST http://localhost:8080/v4/generate \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "AI News",
    "content": "...",
    "podcast_style": "english_2_hosts",
    "target_duration": 300,
    "generate_audio": true
  }'
```

### 3️⃣ 命令行工具 (推荐自动化)
```bash
python generate_from_news.py news.txt \
  --duration 5 \
  --style english_2_hosts \
  --generate-audio
```

## 🔧 常用命令

| 命令 | 作用 |
|------|------|
| `./deploy_podcast_service.sh` | 部署到 Cloud Run |
| `uvicorn main:app --reload` | 本地开发 (热重载) |
| `python generate_from_news.py file.txt` | 命令行生成 |
| `gcloud run services logs read podcast-service` | 查看日志 |
| `gcloud run services describe podcast-service` | 查看服务信息 |
| `gcloud run services delete podcast-service` | 删除服务 |

## 📊 支持的播客模板

| 语言 | 模板 | 描述 |
|------|------|------|
| 🇺🇸 英文 | `english_2_hosts` | 2 人对话 |
| | `english_3_hosts` | 3 人讨论 |
| | `english_4_panel` | 4 人座谈 |
| 🇰🇷 韩文 | `korean_2_hosts` | 2 人对话 |
| | `korean_3_hosts` | 3 人讨论 |
| 🇨🇳 中文 | `chinese_2_hosts` | 2 人对话 |
| 🇯🇵 日文 | `japanese_4_hosts` | 4 人座谈 |
| 🌍 双语 | `bilingual_eng_cn` | 英中双语 |

## ✅ 本地快速启动

```bash
# 1. 创建虚拟环境
python -m venv venv

# 2. 激活虚拟环境
source venv/bin/activate  # macOS/Linux
# 或
venv\Scripts\activate  # Windows

# 3. 安装依赖
pip install -r requirements.txt

# 4. 启动服务
uvicorn main:app --reload

# 5. 访问
打开浏览器 → http://localhost:8080
```

## 🔐 敏感文件

⚠️ **保护这两个文件！**

| 文件 | 内容 | 用途 |
|------|------|------|
| `.env` | OpenAI API 密钥 | LLM 认证 |
| `*.json` | GCP 服务账户密钥 | Google Cloud 认证 |

✅ 已配置 `.gitignore` 自动保护这些文件

## 📋 部署前检查清单

```
[ ] Python 3.10+ 已安装
[ ] .env 文件包含有效的 OpenAI API 密钥
[ ] GCP 密钥文件 (*.json) 存在且有效
[ ] gcloud CLI 已安装和认证
[ ] Docker 已安装 (如果要容器化)
[ ] 所有文件都已复制到 podcast_service
[ ] deploy_podcast_service.sh 可执行
```

## 🆘 常见问题

### 本地运行失败？
```bash
# 1. 检查 Python 版本
python --version  # 需要 3.10+

# 2. 检查虚拟环境激活
which python  # 应该显示 venv 路径

# 3. 重新安装依赖
pip install --upgrade -r requirements.txt
```

### 部署失败？
```bash
# 1. 检查 gcloud 认证
gcloud auth login

# 2. 检查项目配置
gcloud config get-value project

# 3. 检查 API 启用
gcloud services list --enabled | grep run
```

### API 密钥无效？
```bash
# 检查 .env 文件
cat .env | grep OPENAI_API_KEY

# 检查是否以 sk- 开头
# 如果不是，更新为正确的密钥
```

## 📞 获取帮助

1. **查看文档**: `ls -la *.md` 然后阅读相关文档
2. **查看日志**: `gcloud run services logs read podcast-service`
3. **查看部署脚本输出**: 运行脚本时会显示详细错误
4. **阅读 WHY_THESE_FILES.md**: 理解每个文件的用途

## 💾 数据目录

生成的文件会保存到：

```
data/
├── generated_scripts/     # 播客脚本 (JSON)
└── generated_podcasts/    # 播客音频 (MP3)
```

## 🎬 完整工作流程

```
1. 用户提交内容
    ↓
2. LLM 生成脚本 (with 令牌计数)
    ↓
3. 脚本长度检查 (< 85% 触发扩展)
    ↓
4. Google Cloud TTS 合成音频
    ↓
5. 返回脚本和音频
    ↓
6. 用户下载结果
```

## 📞 快速联系

- **文档**: 查看 `*.md` 文件
- **问题**: 查看 `WHY_THESE_FILES.md`
- **部署**: 查看 `QUICK_START_DEPLOY.md`
- **检查**: 使用 `DEPLOYMENT_CHECKLIST.md`

---

**更新时间**: 2025-10-21  
**状态**: ✅ 生产就绪  
**准备好部署了吗？运行: `./deploy_podcast_service.sh`**
