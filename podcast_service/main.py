#!/usr/bin/env python3
"""
播客引擎 v4 - 升级版
完整支持从任意 topic/content → LLM 脚本生成 → TTS 音频输出

特性：
1. 接收任意 topic（不局限于新闻）
2. 使用 OpenAI GPT-4-mini 生成播客脚本
3. 支持多种风格模板
4. 自动语音合成
5. 完整的 REST API

API 端点：
- POST /v4/generate - 从 topic 生成播客
- GET /v4/styles - 列出所有可用样式
- GET /v4/tones - 列出所有可用语调
- GET /v4/scripts/{id} - 获取已生成的脚本
"""

import os
import json
import logging
import hashlib
from typing import Dict, List, Optional, Any
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from enum import Enum
from google.cloud import texttospeech
import re
import math

# 自动加载环境变量（必须在导入其他模块之前）
from src.env_config import load_env, get_config
from src.gcs_utils import GCSUploader
from cost_calculator import CostCalculator, UsageMetrics

try:
    config = load_env(auto_create=True)
except Exception as e:
    print(f"❌ 环境配置加载失败: {e}")
    print("💡 请设置 OPENAI_API_KEY 环境变量或在 .env 文件中配置")
    raise SystemExit(1)

from src.llm_script_generator import (
    LLMScriptGenerator, 
    PodcastTone, 
    DialogueStyle,
    PodcastScript
)
from src.podcast_pipeline import get_llm_language_code  # ✅ 导入语言映射函数
from src.podcast_pipeline import PodcastPipeline
from src.duration_control import (
    count_words,
    calculate_max_words,
    calculate_optimal_tts_params,
    truncate_audio,
    add_duration_constraints_to_prompt,
    enforce_duration_limit,
)

# ============================================================================
# 日志配置
# ============================================================================

logging.basicConfig(
    level=config.log_level,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# Pydantic 模型
# ============================================================================

class GeneratePodcastRequest(BaseModel):
    """播客生成请求"""
    topic: str = Field(..., description="播客主题/内容，可以是任何话题")
    source_content: Optional[str] = Field(default=None, description="源内容（新闻文章、研究报告等）- 提供后LLM将基于此内容生成脚本，不会编造事实")
    style_name: str = Field(default="english_2_hosts", description="样式模板名称")
    tone: str = Field(default="professional", description="语调风格")
    dialogue_style: str = Field(default="conversation", description="对话风格")
    duration_minutes: int = Field(default=5, description="目标时长（分钟）")
    duration_seconds: Optional[int] = Field(
        default=None,
        description="目标时长（秒）。用于精确控制（例如 Shorts 45 秒），优先级高于 duration_minutes。",
    )
    max_words: Optional[int] = Field(default=None, description="最大字数限制（可选，用于精确控制时长）")
    language: str = Field(default="en-US", description="语言代码")
    podcast_name: str = Field(default=None, description="播客名称（自动生成如果为空）")
    speaker_names: Optional[List[str]] = Field(default=None, description="讲话人名字")
    num_speakers: Optional[int] = Field(default=None, description="讲话人数量")
    additional_context: Optional[str] = Field(default=None, description="额外背景信息")
    custom_instructions: Optional[str] = Field(default=None, description="自定义生成指令")
    generate_audio: bool = Field(default=False, description="是否生成 MP3 音频文件（使用 Google Cloud TTS）")
    tts_engine: str = Field(default="google-cloud", description="TTS 引擎选择 (google-cloud)")
    cache_key_prefix: Optional[str] = Field(
        default=None,
        description="可选：GCS 存储 key 前缀（例如 stockflow/us/AAPL/2026-02-04/zh/chinese_2_hosts/dur5）。已配置 GCS_BUCKET_NAME 时将写入 <prefix>/{script.json,audio.mp3,manifest.json}。若启用 use_cache，则会先查 manifest.json 命中则直接返回。",
    )
    use_cache: bool = Field(default=True, description="当已配置 GCS_BUCKET_NAME 且存在 <prefix>/manifest.json 时，是否直接命中返回（不重新生成）。")
    manifest_params: Optional[Dict[str, Any]] = Field(
        default=None,
        description="可选：写入 manifest.json 的参数（用于把调用方传入的 days/horizon/bt/dur/variant 等写清楚）。不会影响缓存命中逻辑。",
    )

    class Config:
        example = {
            "topic": "加密货币市场最新动态",
            "source_content": "Bitcoin跌破$105K，市场恐慌情绪蔓延...",
            "style_name": "english_2_hosts",
            "tone": "professional",
            "dialogue_style": "conversation",
            "duration_minutes": 5,
            "language": "en-US",
            "additional_context": "目标听众是加密货币投资者",
            "generate_audio": True
        }

class GeneratePodcastResponse(BaseModel):
    """播客生成响应"""
    status: str
    podcast_name: str
    podcast_id: str
    topic: str
    style: str
    tone: str
    dialogue_style: str
    duration_minutes: int
    language: str
    num_speakers: int
    script_file: str  # 可以是 gs:// 路径或 signed URL
    script_file_signed_url: Optional[str] = None  # Signed URL for download
    output_file: Optional[str] = None
    audio_file: Optional[str] = None  # 可以是 gs:// 路径或 signed URL
    audio_file_signed_url: Optional[str] = None  # Signed URL for download
    audio_file_size_bytes: Optional[int] = None  # 音频文件大小（字节）
    audio_duration_seconds: Optional[float] = None  # 实际音频时长
    script_preview: Optional[Dict[str, Any]] = None
    token_usage: Optional[Dict[str, int]] = None  # {total_tokens, prompt_tokens, completion_tokens}
    tts_character_count: Optional[int] = None  # TTS 处理的字符数
    cost_breakdown: Optional[Dict[str, float]] = None  # {llm_cost_usd, tts_cost_usd, total_cost_usd}
    message: str
    timestamp: datetime
    generation_time_seconds: float
    cached: Optional[bool] = None
    cache_key_prefix: Optional[str] = None


def _sanitize_cache_prefix(prefix: str) -> str:
    v = (prefix or "").strip().strip("/")
    if not v:
        raise ValueError("cache_key_prefix 不能为空")
    if ".." in v or v.startswith(".") or v.startswith("/"):
        raise ValueError("cache_key_prefix 非法")
    # Allow only a safe subset for GCS object paths: letters/digits plus "._-/".
    # Note: place "-" at the end to avoid regex character range issues.
    # Underscore is allowed in GCS object names and is commonly used in style names.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,512}", v.replace("_", "-")):
        raise ValueError("cache_key_prefix 包含不支持字符")
    v = re.sub(r"/{2,}", "/", v)
    return v

def _derive_cache_prefix_from_request(request: "GeneratePodcastRequest") -> str:
    """
    当调用方未提供 cache_key_prefix（或不合法）时，派生一个稳定且按日期分目录的前缀。
    注意：若调用方希望“同一 ticker / 同一天 / 同语言”固定复用，应由调用方显式传入包含日期/标识的 cache_key_prefix。
    """
    basis = {
        "topic": request.topic,
        "source_content": request.source_content,
        "style_name": request.style_name,
        "tone": request.tone,
        "dialogue_style": request.dialogue_style,
        "duration_minutes": request.duration_minutes,
        "language": request.language,
        "additional_context": request.additional_context,
        "custom_instructions": request.custom_instructions,
        "generate_audio": request.generate_audio,
        "tts_engine": request.tts_engine,
    }
    canonical = json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    date_key = datetime.utcnow().date().isoformat()
    derived = f"stockflow/auto/v1/{date_key}/{request.language}/{request.style_name}/dur{request.duration_minutes}/{digest}"
    return _sanitize_cache_prefix(derived)

class ScriptResponse(BaseModel):
    """脚本响应"""
    podcast_id: str
    podcast_name: str
    topic: str
    script: Dict[str, Any]
    created_at: datetime

# ============================================================================
# FastAPI 应用
# ============================================================================

app = FastAPI(
    title="🎙️ AI Podcast Engine v4",
    description="AI 播客引擎 - 从任意话题自动生成播客",
    version="4.0.0"
)

# 全局组件
script_generator: LLMScriptGenerator = None
podcast_pipeline: PodcastPipeline = None
generated_scripts_dir = Path("data/generated_scripts")
gcs_bucket_name: Optional[str] = None

# ============================================================================
# CORS配置 - 允许前端访问
# ============================================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境建议限制具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# 初始化
# ============================================================================

@app.on_event("startup")
async def startup_event():
    """应用启动时初始化"""
    global script_generator, podcast_pipeline, gcs_bucket_name
    
    logger.info("🚀 初始化播客引擎 v4...")
    
    # 获取配置
    config = get_config()
    logger.info(f"📋 使用配置: {config.llm_model} | API Port: {config.api_port}")
    gcs_bucket_name = config.gcs_bucket_name or None
    if gcs_bucket_name:
        logger.info(f"☁️ 文件将上传到 GCS 存储桶: {gcs_bucket_name}")
    else:
        logger.warning("⚠️ 未配置 GCS_BUCKET_NAME，生成文件仅保存在容器本地。")
    
    # 初始化 LLM 脚本生成器
    try:
        script_generator = LLMScriptGenerator(model=config.llm_model)
        logger.info("✅ LLM 脚本生成器初始化成功")
    except ValueError as e:
        logger.error(f"❌ LLM 脚本生成器初始化失败: {e}")
        logger.error("💡 请检查: OPENAI_API_KEY 环境变量是否正确设置")
        raise SystemExit(1)
    except Exception as e:
        logger.error(f"❌ LLM 脚本生成器初始化失败: {e}")
        raise
    
    # 初始化播客管道
    try:
        podcast_pipeline = PodcastPipeline()
        logger.info("✅ 播客管道初始化成功")
    except Exception as e:
        logger.error(f"❌ 播客管道初始化失败: {e}")
        # 这不是致命错误，TTS 是可选的
        logger.warning("⚠️  将继续运行（TTS 功能可能不可用）")
    
    # 创建输出目录
    generated_scripts_dir.mkdir(parents=True, exist_ok=True)
    
    logger.info("✅ 播客引擎 v4 已准备好！")

# ============================================================================
# Web界面路由
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def read_root():
    """提供Web界面"""
    html_file = Path(__file__).parent / "generate_podcast_ui.html"
    if html_file.exists():
        return FileResponse(html_file)
    else:
        return HTMLResponse(content="""
        <html>
            <body>
                <h1>播客生成器API</h1>
                <p>Web界面文件未找到。请访问 <a href="/docs">/docs</a> 查看API文档。</p>
            </body>
        </html>
        """)

# ============================================================================
# REST API 端点
# ============================================================================

@app.get("/v4/health")
async def health_check():
    """健康检查"""
    return {
        "status": "healthy",
        "version": "4.0.0",
        "components": {
            "llm_generator": script_generator is not None,
            "podcast_pipeline": podcast_pipeline is not None
        },
        "gcs": {
            "enabled": bool(gcs_bucket_name),
            "bucket": gcs_bucket_name,
        },
    }

@app.get("/v4/tones")
async def list_tones():
    """列出所有可用的语调风格"""
    tones = [
        {
            "value": tone.value,
            "description": {
                "professional": "专业严肃的语调",
                "casual": "随意轻松的对话",
                "educational": "教育性讲解",
                "entertaining": "娱乐性内容",
                "investigative": "调查深度分析",
                "storytelling": "故事叙述风格",
                "humorous": "幽默轻松",
                "debate": "辩论讨论"
            }.get(tone.value, tone.value)
        }
        for tone in PodcastTone
    ]
    
    return {
        "status": "success",
        "count": len(tones),
        "tones": tones
    }

@app.get("/v4/dialogue-styles")
async def list_dialogue_styles():
    """列出所有可用的对话风格"""
    styles = [
        {
            "value": style.value,
            "description": {
                "monologue": "单人独白",
                "interview": "采访对话",
                "debate": "辩论讨论",
                "conversation": "随意对话",
                "narration": "旁白解说",
                "panel": "专家论坛"
            }.get(style.value, style.value)
        }
        for style in DialogueStyle
    ]
    
    return {
        "status": "success",
        "count": len(styles),
        "dialogue_styles": styles
    }

@app.post("/v4/generate", response_model=GeneratePodcastResponse)
async def generate_podcast_v4(request: GeneratePodcastRequest):
    """
    从任意 topic 生成完整播客
    
    支持：
    - 任何主题（旅游、技术、生活、娱乐等）
    - 多种语言
    - 可自定义的风格和语调
    - 自动 LLM 脚本生成
    - 可选的 TTS 音频输出
    
    返回：
    - 生成的播客脚本
    - 可选的 MP3 音频文件
    """
    
    start_time = datetime.now()
    
    logger.info("="*80)
    logger.info(f"🎬 生成播客请求")
    logger.info(f"   Topic: {request.topic[:60]}...")
    logger.info(f"   Style: {request.style_name}")
    logger.info(f"   Tone: {request.tone}")
    logger.info(f"   Language: {request.language}")
    logger.info(f"   Duration: {request.duration_minutes} min")
    logger.info(f"   use_cache: {request.use_cache}")
    logger.info(f"   cache_key_prefix(raw): {request.cache_key_prefix!r}")
    logger.info("="*80)
    
    try:
        # 0️⃣ GCS 统一存储前缀（按 cache_key_prefix / 派生前缀）+ 可选缓存命中（manifest.json）
        cache_prefix = None
        if gcs_bucket_name:
            if request.cache_key_prefix:
                try:
                    cache_prefix = _sanitize_cache_prefix(request.cache_key_prefix)
                except Exception as e:
                    logger.warning(f"⚠️ cache_key_prefix 非法，回退到派生前缀: {e}")
                    cache_prefix = _derive_cache_prefix_from_request(request)
            else:
                cache_prefix = _derive_cache_prefix_from_request(request)

            logger.info(f"   cache_key_prefix(effective): {cache_prefix}")

            if request.use_cache:
                try:
                    manifest_blob = f"{cache_prefix}/manifest.json"
                    if GCSUploader.blob_exists(gcs_bucket_name, manifest_blob):
                        manifest = GCSUploader.download_json(gcs_bucket_name, manifest_blob)
                        script_blob = str(manifest.get("script_blob") or "")
                        audio_blob = str(manifest.get("audio_blob") or "")

                        script_uri = f"gs://{gcs_bucket_name}/{script_blob}" if script_blob else ""
                        audio_uri = f"gs://{gcs_bucket_name}/{audio_blob}" if audio_blob else None

                        script_signed_url = (
                            GCSUploader.generate_signed_url(gcs_bucket_name, script_blob, expiration_hours=24)
                            if script_blob
                            else None
                        )
                        audio_signed_url = (
                            GCSUploader.generate_signed_url(gcs_bucket_name, audio_blob, expiration_hours=24)
                            if audio_blob
                            else None
                        )

                        elapsed = (datetime.now() - start_time).total_seconds()
                        return GeneratePodcastResponse(
                            status="success",
                            podcast_name=str(manifest.get("podcast_name") or ""),
                            podcast_id=str(manifest.get("podcast_id") or ""),
                            topic=str(manifest.get("topic") or request.topic),
                            style=str(manifest.get("style") or request.style_name),
                            tone=str(manifest.get("tone") or request.tone),
                            dialogue_style=str(manifest.get("dialogue_style") or request.dialogue_style),
                            duration_minutes=int(manifest.get("duration_minutes") or request.duration_minutes),
                            language=str(manifest.get("language") or request.language),
                            num_speakers=int(manifest.get("num_speakers") or 0),
                            script_file=script_uri,
                            script_file_signed_url=script_signed_url,
                            audio_file=audio_uri,
                            audio_file_signed_url=audio_signed_url,
                            audio_file_size_bytes=manifest.get("audio_file_size_bytes"),
                            audio_duration_seconds=manifest.get("audio_duration_seconds"),
                            script_preview=manifest.get("script_preview"),
                            token_usage=manifest.get("token_usage"),
                            tts_character_count=manifest.get("tts_character_count"),
                            cost_breakdown=manifest.get("cost_breakdown"),
                            message="✅ cache hit",
                            timestamp=datetime.now(),
                            generation_time_seconds=elapsed,
                            cached=True,
                            cache_key_prefix=cache_prefix,
                        )
                except Exception as cache_err:
                    logger.warning(f"⚠️ 缓存检查失败，继续实时生成: {cache_err}")

        # 1️⃣ 生成播客 ID 和名称
        podcast_id = f"podcast_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        podcast_name = request.podcast_name or f"{request.style_name}_{podcast_id}"
        
        logger.info(f"\n1️⃣ 准备生成参数...")
        logger.info(f"   Podcast ID: {podcast_id}")
        logger.info(f"   Name: {podcast_name}")
        
        # 2️⃣ 验证语调和对话风格
        try:
            tone = PodcastTone[request.tone.upper()]
        except KeyError:
            tone = PodcastTone.PROFESSIONAL
            logger.warning(f"⚠️  未知的语调 '{request.tone}'，使用默认值")
        
        try:
            dialogue_style = DialogueStyle[request.dialogue_style.upper()]
        except KeyError:
            dialogue_style = DialogueStyle.CONVERSATION
            logger.warning(f"⚠️  未知的对话风格 '{request.dialogue_style}'，使用默认值")
        
        # 3️⃣ 从 style template 获取讲话人信息
        logger.info(f"\n2️⃣ 加载 style template: {request.style_name}")
        from src.podcast_pipeline import TemplateManager
        tm = TemplateManager("config/podcast_style_templates.yaml")
        style_template = tm.get_template(request.style_name)
        
        # 获取讲话人数量、名称、语言和角色信息
        num_speakers = request.num_speakers
        speaker_names = request.speaker_names
        template_language = request.language
        speaker_roles = []  # 角色信息
        speaker_genders = []  # 性别信息
        template_speaker_ids = []  # Template 中的讲话人 ID
        
        if style_template and style_template.get('speakers'):
            # 从 style template 中提取讲话人信息
            template_speakers = style_template['speakers']
            num_speakers = len(template_speakers)
            
            # 提取角色信息、性别和语言
            speaker_roles = [s.get('role', 'Guest') for s in template_speakers]
            speaker_genders = [s.get('gender', 'MALE') for s in template_speakers]
            template_speaker_ids = [s.get('id') for s in template_speakers]
            template_language = style_template.get('language', request.language)
            
            # 🔑 重要：不使用 template 中的通用名字（如 "Host 1"），
            # 而是让 LLM 根据角色和性别生成合适的名字
            # 我们只提取性别，让 LLM 生成真实的名字
            speaker_names = None  # 不提供固定的名字，让 LLM 生成
            
            logger.info(f"   ✅ 已从 template 获取讲话人配置")
            logger.info(f"   角色: {speaker_roles}")
            logger.info(f"   性别: {speaker_genders}")
            logger.info(f"   语言: {template_language}")
        else:
            # 回退：根据样式名推断数量
            style_mapping = {
                "english_2_hosts": 2,
                "english_3_experts": 3,
                "english_4_panel": 4,
                "korean_2_hosts": 2,
                "korean_3_experts": 3,
                "japanese_4_panel": 4,
                "chinese_2_hosts": 2,
            }
            num_speakers = num_speakers or style_mapping.get(request.style_name, 2)
            logger.info(f"   ℹ️  使用样式映射，讲话人数: {num_speakers}")
        
        logger.info(f"\n3️⃣ 调用 LLM 生成脚本...")
        logger.info(f"   Tone: {tone.value}")
        logger.info(f"   Dialogue Style: {dialogue_style.value}")
        logger.info(f"   Speakers: {len(speaker_names) if speaker_names else num_speakers} people")
        logger.info(f"   Language: {template_language}")
        
        # ⏱️ 计算时长控制参数（支持秒级）
        target_duration_seconds = (
            int(request.duration_seconds)
            if request.duration_seconds is not None
            else int(request.duration_minutes) * 60
        )
        target_duration_seconds = max(10, min(900, target_duration_seconds))
        max_words = request.max_words or calculate_max_words(target_duration_seconds, template_language)
        logger.info(f"   Target Duration: {target_duration_seconds}s")
        logger.info(f"   Max Words: {max_words}")
        
        # 构建讲话人配置信息，直接从 template 获取，无需硬编码
        if speaker_roles and speaker_genders and template_speaker_ids:
            # 直接传递 template 中的讲话人完整配置给 LLM
            speaker_config = []
            for speaker_id, role, gender in zip(template_speaker_ids, speaker_roles, speaker_genders):
                speaker_config.append(f"  - speaker_id: {speaker_id}, role: {role}, gender: {gender}")
            speaker_config_text = "\n".join(speaker_config)
            
            # 让 LLM 根据完整的讲话人配置灵活生成对话
            base_custom_inst = f"""Generate a podcast dialogue with the following speaker configuration:

Speaker Roles:
{speaker_config_text}

CRITICAL REQUIREMENTS:
1. Generate speaker names based on their roles and genders
2. Use speaker_id as "speaker_1", "speaker_2", "speaker_3", "speaker_4" etc.
3. Each speaker MUST participate actively in the dialogue - rotate speaker IDs to ensure all speakers speak multiple times
4. Different speakers must have distinct names, personalities, and perspectives appropriate to their roles
5. Create natural back-and-forth dialogue between all speakers
6. Each role should be distinguished by their expertise and perspective (e.g., Host moderates, Co-host adds commentary, Guests provide perspectives)
7. Do NOT assign all speech to one speaker
8. Dialogue should flow naturally and realistically"""
            # 添加时长约束
            custom_inst = add_duration_constraints_to_prompt(
                base_custom_inst, max_words, target_duration_seconds, template_language
            )
        else:
            base_custom_inst = request.custom_instructions or "Generate natural dialogue with distinct personalities for each speaker."
            # 添加时长约束
            custom_inst = add_duration_constraints_to_prompt(
                base_custom_inst, max_words, target_duration_seconds, template_language
            )
        
        # 4️⃣ 使用 LLM 生成脚本（考虑角色、性别和语言）
        # 如果提供了 source_content，将其添加到 additional_context 中
        final_context = request.additional_context or ""
        if request.source_content:
            logger.info(f"   📰 使用源内容生成 (长度: {len(request.source_content)} 字符)")
            source_prefix = "\n\n【重要：基于以下真实内容生成播客】\n"
            final_context = source_prefix + request.source_content + "\n\n" + final_context
        
        # ✅ 语言代码映射：将TTS语言代码转换为LLM语言代码
        # Google TTS使用 "cmn-CN"，但OpenAI LLM使用 "zh-CN"
        llm_language = get_llm_language_code(template_language)
        if llm_language != template_language:
            logger.info(f"   🔄 语言代码映射: {template_language} (TTS) → {llm_language} (LLM)")
        
        script: PodcastScript = script_generator.generate_script(
            topic=request.topic,
            num_speakers=num_speakers,
            duration_minutes=max(1, int(math.ceil(target_duration_seconds / 60))),
            language=llm_language,  # ✅ 使用映射后的LLM语言代码
            tone=tone,
            dialogue_style=dialogue_style,
            speaker_names=speaker_names,  # 现在是 None，让 LLM 生成
            template_speaker_ids=template_speaker_ids,  # ✅ 传递 template 中的讲话人 ID
            additional_context=final_context,  # ✅ 包含源内容
            custom_instructions=custom_inst
        )
        
        logger.info(f"✅ 脚本生成成功")
        logger.info(f"   标题: {script.title}")
        logger.info(f"   段落数: {len(script.segments)}")
        logger.info(f"   预计时长: {script.estimated_duration_seconds:.1f}秒")
        
        # ⏱️ 强制控制脚本长度：保持 Google TTS 原语速（speaking_rate=1.0），因此必须通过压缩脚本满足时长。
        def _risk_line(lang_code: str) -> str:
            lc = str(lang_code or "").lower()
            if lc.startswith("cmn"):
                return "非投资建议，高风险。"
            if lc.startswith("ja"):
                return "投資助言ではありません。リスクがあります。"
            if lc.startswith("ko"):
                return "투자 조언이 아닙니다. 고위험입니다."
            return "Not financial advice. High risk."

        def _words_for_segment_text(seg_text: str) -> int:
            return count_words(str(seg_text or ""), template_language)

        def _truncate_text_to_words(text: str, allowed_words: int) -> str:
            if allowed_words <= 0:
                return ""
            t = str(text or "").strip()
            if not t:
                return ""
            if str(template_language).lower().startswith(("cmn", "ja", "ko")):
                # CJK: approximate by characters (count_words already strips punctuation/spaces).
                # Keep it simple: truncate by visible length.
                return t[:allowed_words].strip()
            parts = t.split()
            return " ".join(parts[:allowed_words]).strip()

        # Count by spoken text (exclude speaker names/IDs, exclude JSON overhead).
        original_spoken_words = sum(_words_for_segment_text(s.text) for s in script.segments)
        logger.info(f"   Spoken word/char count: {original_spoken_words} (limit={max_words})")

        if original_spoken_words > max_words:
            logger.warning(f"⚠️  脚本长度超限，进行确定性压缩以适配 {target_duration_seconds}s（speaking_rate=1.0）...")

            risk = _risk_line(template_language)
            risk_words = _words_for_segment_text(risk)
            budget = max(1, max_words - risk_words)

            new_segments = []
            used = 0
            for seg in script.segments:
                seg_text = str(seg.text or "").strip()
                if not seg_text:
                    continue
                seg_words = _words_for_segment_text(seg_text)
                if used + seg_words <= budget:
                    new_segments.append(seg)
                    used += seg_words
                    continue

                remaining = budget - used
                truncated = _truncate_text_to_words(seg_text, remaining)
                if truncated:
                    # Rebuild SSML for truncated segment (safe minimal SSML).
                    escaped = (
                        truncated.replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;")
                    )
                    seg.text = truncated
                    seg.ssml_text = f"<speak>{escaped}</speak>"
                    new_segments.append(seg)
                    used += _words_for_segment_text(truncated)
                break

            # Append risk line as a final segment (short, consistent).
            if new_segments:
                last = new_segments[-1]
                escaped_risk = (
                    risk.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                try:
                    from src.llm_script_generator import ScriptSegment
                    new_segments.append(
                        ScriptSegment(
                            speaker_id=last.speaker_id,
                            speaker_name=last.speaker_name,
                            text=risk,
                            ssml_text=f"<speak>{escaped_risk}</speak>",
                            duration_seconds=0.0,
                            segment_type="closing",
                            notes="risk_disclaimer",
                        )
                    )
                except Exception:
                    # Fallback: if dataclass import fails for any reason, skip adding.
                    pass

            script.segments = new_segments
            final_spoken_words = sum(_words_for_segment_text(s.text) for s in script.segments)
            # Recompute rough duration estimate at natural speech rate.
            rate = 220 if str(template_language).lower().startswith("cmn") else 140
            try:
                # Match duration_control's SPEECH_RATE lookup style but keep it simple here.
                from src.duration_control import SPEECH_RATE as _SR
                rate = int(_SR.get(template_language, rate))
            except Exception:
                pass
            script.estimated_duration_seconds = (final_spoken_words / max(1, rate)) * 60

            logger.info(f"   Compressed spoken count: {original_spoken_words} -> {final_spoken_words} (limit={max_words})")
        
        # 5️⃣ 保存脚本
        logger.info(f"\n4️⃣ 保存脚本...")
        
        script_path = generated_scripts_dir / f"{podcast_id}_script.json"
        script_generator.save_script(script, str(script_path))
        logger.info(f"✅ 脚本已保存: {script_path}")
        with open(script_path, 'r', encoding='utf-8') as f:
            script_data = json.load(f)

        script_uri = str(script_path)
        if gcs_bucket_name:
            try:
                if not cache_prefix:
                    raise RuntimeError("GCS 已启用但未生成 cache_key_prefix")
                script_blob = f"{cache_prefix}/script.json"
                script_uri = GCSUploader.upload_file(
                    local_path=script_path,
                    bucket_name=gcs_bucket_name,
                    destination_path=script_blob,
                )
                logger.info(f"☁️ 脚本已上传至 GCS: {script_uri}")
            except Exception as upload_err:
                logger.error(f"❌ 脚本上传 GCS 失败: {upload_err}", exc_info=True)
        else:
            logger.debug("GCS_BUCKET_NAME 未配置，跳过脚本上传。")
        
        # 6️⃣ 可选：生成音频（使用 Google Cloud TTS）
        audio_file = None
        audio_uri = None
        tts_character_count = 0
        audio_duration_seconds = None
        audio_file_size_bytes = None
        
        if request.generate_audio:
            logger.info(f"\n5️⃣ 生成 MP3 音频（使用 Google Cloud TTS）...")
            try:
                # 使用生成的脚本来合成音频（昨天验证过的方式）
                from src.audio_synthesizer import AudioSynthesizer, SpeakerVoiceConfig
                
                logger.info(f"  初始化音频合成器...")
                
                # Product requirement: keep Google TTS at natural/default speed.
                synthesizer = AudioSynthesizer(speaking_rate=1.0)
                
                logger.info(f"  脚本已加载，开始合成音频...")
                
                # 从 style_template 构建讲话人声音映射
                # 脚本中使用的是 speaker_1, speaker_2 等，需要映射到 template 中定义的讲话人
                speaker_voice_map = None
                if style_template and style_template.get('speakers'):
                    speaker_voice_map = {}
                    template_speakers = style_template['speakers']
                    
                    for idx, speaker in enumerate(template_speakers):
                        # 建立映射：speaker_1 -> template 的第 1 个讲话人, speaker_2 -> 第 2 个, 等等
                        generic_speaker_id = f"speaker_{idx + 1}"
                        template_speaker_id = speaker.get('id')
                        cfg = SpeakerVoiceConfig(
                            speaker_id=generic_speaker_id,
                            speaker_name=speaker.get('name', f'Speaker {idx + 1}'),
                            language_code=style_template.get('language', 'en-US'),
                            voice_name=speaker.get('voice_name', 'en-US-Neural2-I'),
                            ssml_gender=texttospeech.SsmlVoiceGender.MALE if speaker.get('gender', 'MALE') == 'MALE' else texttospeech.SsmlVoiceGender.FEMALE,
                        )

                        # Store under both keys: generic (speaker_1) and template id (host_male)
                        speaker_voice_map[generic_speaker_id] = cfg
                        if template_speaker_id:
                            speaker_voice_map[template_speaker_id] = cfg

                        logger.info(f"    [{idx+1}/{len(template_speakers)}] {generic_speaker_id} / {template_speaker_id} -> {speaker.get('name')} ({speaker.get('voice_name')})")
                    
                    logger.info(f"  ✅ 已从 template 配置 {len(speaker_voice_map)} 个讲话人声音")
                
                # 使用合成器生成音频（返回 tuple: path, tts_chars, duration, file_size）
                output_path, tts_character_count, audio_duration_seconds, audio_file_size_bytes = synthesizer.generate_from_script(
                    script_data=script_data,
                    podcast_name=None,  # None 会自动从脚本标题生成名称
                    speaker_voice_map=speaker_voice_map
                )
                
                if output_path and Path(output_path).exists():
                    audio_file = str(output_path)
                    file_size_mb = audio_file_size_bytes / 1024 / 1024
                    logger.info(f"✅ 音频已生成: {output_path}")
                    logger.info(f"   文件大小: {file_size_mb:.2f} MB")
                    logger.info(f"   TTS字符数: {tts_character_count}")
                    logger.info(f"   音频时长: {audio_duration_seconds:.1f}秒")
                    
                    # ⏱️ 音频截断（如果超过目标时长）
                    if audio_duration_seconds and audio_duration_seconds > target_duration_seconds:
                        logger.warning(f"⚠️  音频时长 {audio_duration_seconds:.1f}s 超过目标 {target_duration_seconds}s，进行截断...")
                        truncated_path = str(Path(output_path).parent / f"{Path(output_path).stem}_truncated.mp3")
                        audio_file = truncate_audio(
                            input_path=output_path,
                            output_path=truncated_path,
                            target_duration=target_duration_seconds,
                            fade_out=1.5
                        )
                        # 更新时长和大小
                        if audio_file != output_path:
                            audio_duration_seconds = target_duration_seconds
                            audio_file_size_bytes = Path(audio_file).stat().st_size
                            output_path = audio_file
                            logger.info(f"✅ 音频已截断至 {target_duration_seconds}s")

                    audio_uri = audio_file
                    if gcs_bucket_name:
                        try:
                            if not cache_prefix:
                                raise RuntimeError("GCS 已启用但未生成 cache_key_prefix")
                            audio_blob = f"{cache_prefix}/audio.mp3"
                            audio_uri = GCSUploader.upload_file(
                                local_path=Path(output_path),
                                bucket_name=gcs_bucket_name,
                                destination_path=audio_blob,
                            )
                            logger.info(f"☁️ 音频已上传至 GCS: {audio_uri}")
                        except Exception as upload_err:
                            logger.error(f"❌ 音频上传 GCS 失败: {upload_err}", exc_info=True)
                    else:
                        logger.debug("GCS_BUCKET_NAME 未配置，跳过音频上传。")
                else:
                    logger.warning(f"⚠️  音频文件未创建")
            
            except Exception as e:
                logger.error(f"❌ 音频生成出错: {e}")
                import traceback
                traceback.print_exc()
                # When audio is explicitly requested, fail the request instead of returning success with no audio.
                raise HTTPException(status_code=500, detail=f"audio_generation_failed: {e}")
        
        # 7️⃣ 生成 signed URLs（如果文件在 GCS 中）
        script_signed_url = None
        audio_signed_url = None
        
        if gcs_bucket_name and script_uri.startswith('gs://'):
            try:
                bucket_and_path = script_uri.replace('gs://', '')
                bucket, blob_path = bucket_and_path.split('/', 1)
                script_signed_url = GCSUploader.generate_signed_url(
                    bucket_name=bucket,
                    blob_name=blob_path,
                    expiration_hours=24
                )
                logger.info(f"✅ 生成脚本签名 URL (24小时有效期)")
            except Exception as e:
                logger.error(f"❌ 生成脚本签名 URL 失败: {e}")
                # 权限问题应该在部署时解决，不使用备用方案
        
        if gcs_bucket_name and audio_uri and audio_uri.startswith('gs://'):
            try:
                bucket_and_path = audio_uri.replace('gs://', '')
                bucket, blob_path = bucket_and_path.split('/', 1)
                audio_signed_url = GCSUploader.generate_signed_url(
                    bucket_name=bucket,
                    blob_name=blob_path,
                    expiration_hours=24
                )
                logger.info(f"✅ 生成音频签名 URL (24小时有效期)")
            except Exception as e:
                logger.error(f"❌ 生成音频签名 URL 失败: {e}")
                # 权限问题应该在部署时解决，不使用备用方案
        
        # 8️⃣ 计算运行时间
        elapsed = (datetime.now() - start_time).total_seconds()
        
        # 9️⃣ 计算成本估算
        cost_calculator = CostCalculator()
        cost_breakdown = None
        if script.token_usage or tts_character_count > 0:
            usage_metrics = UsageMetrics(
                prompt_tokens=script.token_usage.get('prompt_tokens', 0) if script.token_usage else 0,
                completion_tokens=script.token_usage.get('completion_tokens', 0) if script.token_usage else 0,
                total_tokens=script.token_usage.get('total_tokens', 0) if script.token_usage else 0,
                tts_characters=tts_character_count,
                tts_duration_seconds=audio_duration_seconds,
                audio_file_size_bytes=audio_file_size_bytes
            )
            cost = cost_calculator.calculate_total_cost(usage_metrics, voice_type="neural")
            cost_breakdown = cost.to_dict()
            logger.info(f"💰 成本估算 (使用 Neural TTS):")
            logger.info(f"   LLM 成本: ${cost.llm_total_cost_usd:.6f}")
            logger.info(f"   TTS 成本: ${cost.tts_cost_usd:.6f}")
            logger.info(f"   总成本: ${cost.total_cost_usd:.6f}")
        
        # 🔟 准备响应
        logger.info(f"\n✅ 生成完成! (耗时 {elapsed:.1f}秒)")
        
        audio_display_path = audio_uri or audio_file

        response = GeneratePodcastResponse(
            status="success",
            podcast_name=podcast_name,
            podcast_id=podcast_id,
            topic=request.topic,
            style=request.style_name,
            tone=tone.value,
            dialogue_style=dialogue_style.value,
            duration_minutes=request.duration_minutes,
            language=request.language,
            num_speakers=num_speakers,
            script_file=script_uri,
            script_file_signed_url=script_signed_url,
            audio_file=audio_uri if audio_uri else audio_file,
            audio_file_signed_url=audio_signed_url,
            audio_file_size_bytes=audio_file_size_bytes,
            audio_duration_seconds=audio_duration_seconds,
            script_preview={
                "title": script.title,
                "description": script.description,
                "num_segments": len(script.segments),
                "estimated_duration_seconds": script.estimated_duration_seconds,
                "first_segment": {
                    "speaker": script.segments[0].speaker_name,
                    "text": script.segments[0].text[:100] + "..."
                } if script.segments else None
            },
            token_usage=script.token_usage,
            tts_character_count=tts_character_count if tts_character_count > 0 else None,
            cost_breakdown=cost_breakdown,
            message=f"✅ 播客脚本生成成功! 包含 {len(script.segments)} 个段落，预计 {script.estimated_duration_seconds:.0f} 秒。" + 
                   (f"\n🎵 音频文件已生成: {audio_display_path.split('/')[-1]}" if audio_display_path else ""),
            timestamp=datetime.now(),
            generation_time_seconds=elapsed,
            cached=False if cache_prefix else None,
            cache_key_prefix=cache_prefix,
        )

        # 11️⃣ 写入 manifest（统一目录索引）
        if gcs_bucket_name and cache_prefix:
            try:
                script_blob = f"{cache_prefix}/script.json"
                audio_blob = f"{cache_prefix}/audio.mp3" if (audio_uri and str(audio_uri).startswith("gs://")) else ""
                manifest = {
                    "version": 1,
                    "podcast_id": podcast_id,
                    "podcast_name": podcast_name,
                    "topic": request.topic,
                    "style": request.style_name,
                    "tone": tone.value,
                    "dialogue_style": dialogue_style.value,
                    "duration_minutes": request.duration_minutes,
                    "language": request.language,
                    "num_speakers": num_speakers,
                    "script_blob": script_blob,
                    "audio_blob": audio_blob,
                    "script_preview": response.script_preview,
                    "token_usage": response.token_usage,
                    "tts_character_count": response.tts_character_count,
                    "cost_breakdown": response.cost_breakdown,
                    "audio_duration_seconds": response.audio_duration_seconds,
                    "audio_file_size_bytes": response.audio_file_size_bytes,
                    "created_at": datetime.now().isoformat(),
                    "stockflow_params": request.manifest_params or None,
                }
                GCSUploader.upload_json(gcs_bucket_name, f"{cache_prefix}/manifest.json", manifest)
                logger.info(f"✅ 已写入 manifest: gs://{gcs_bucket_name}/{cache_prefix}/manifest.json")
            except Exception as manifest_err:
                logger.error(f"❌ 写入缓存 manifest 失败: {manifest_err}", exc_info=True)
        
        return response
        
    except Exception as e:
        logger.error(f"❌ 生成失败: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"播客生成失败: {str(e)}"
        )

@app.get("/v4/scripts/{podcast_id}", response_model=ScriptResponse)
async def get_script(podcast_id: str):
    """获取已生成的脚本"""
    
    script_path = generated_scripts_dir / f"{podcast_id}_script.json"
    
    if not script_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"脚本不存在: {podcast_id}"
        )
    
    with open(script_path, 'r', encoding='utf-8') as f:
        script_data = json.load(f)
    
    return ScriptResponse(
        podcast_id=podcast_id,
        podcast_name=script_data.get("title", "Unknown"),
        topic=script_data.get("topic", ""),
        script=script_data,
        created_at=datetime.now()
    )

@app.get("/v4")
async def root():
    """API 根端点"""
    return {
        "name": "🎙️ AI Podcast Engine v4",
        "version": "4.0.0",
        "description": "从任意话题自动生成播客",
        "endpoints": {
            "health": "/v4/health",
            "generate": "POST /v4/generate",
            "tones": "/v4/tones",
            "dialogue_styles": "/v4/dialogue-styles",
            "get_script": "/v4/scripts/{podcast_id}"
        },
        "example_request": {
            "topic": "如何在加州旅游中避免常见的旅游陷阱",
            "style_name": "english_2_hosts",
            "tone": "entertaining",
            "dialogue_style": "conversation",
            "duration_minutes": 5,
            "language": "en-US"
        }
    }


# ============================================================================
# 应用运行
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8080,
        log_level="info"
    )
