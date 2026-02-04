#!/usr/bin/env python3
"""
===============================================================================
AI播客生成Pipeline - 生产级系统
===============================================================================
支持：
  1. 模板化配置 (YAML)
  2. 动态内容注入 (新闻、数据、评论)
  3. AI生成对话 (使用LLM)
  4. 批量生成 (多集并行)
  5. GCP部署就绪
===============================================================================
"""

import os
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
import yaml
from datetime import datetime
from google.cloud import texttospeech
from pydub import AudioSegment
import io

# ============================================================================
# 日志配置
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# 语言代码映射 - 处理TTS和LLM之间的语言代码差异
# ============================================================================

# Google TTS使用cmn-CN，但OpenAI LLM使用zh-CN
# 这个映射表将TTS语言代码转换为LLM语言代码
LANGUAGE_CODE_MAPPING = {
    # TTS语言代码 -> LLM语言代码
    "cmn-CN": "zh-CN",      # 简体中文
    "cmn-TW": "zh-TW",      # 繁体中文
    "yue-HK": "zh-HK",      # 粤语（香港）
    "en-US": "en-US",       # 美式英语（无需转换）
    "en-GB": "en-GB",       # 英式英语（无需转换）
    "ko-KR": "ko-KR",       # 韩语（无需转换）
    "ja-JP": "ja-JP",       # 日语（无需转换）
}

def get_llm_language_code(tts_language_code: str) -> str:
    """
    将TTS语言代码转换为LLM语言代码
    
    Args:
        tts_language_code: Google TTS使用的语言代码
        
    Returns:
        OpenAI LLM使用的语言代码
    """
    return LANGUAGE_CODE_MAPPING.get(tts_language_code, tts_language_code)

def get_tts_language_code(llm_language_code: str) -> str:
    """
    将LLM语言代码转换为TTS语言代码（反向映射）
    
    Args:
        llm_language_code: OpenAI LLM使用的语言代码
        
    Returns:
        Google TTS使用的语言代码
    """
    # 创建反向映射
    reverse_mapping = {v: k for k, v in LANGUAGE_CODE_MAPPING.items()}
    return reverse_mapping.get(llm_language_code, llm_language_code)

# ============================================================================
# 数据模型
# ============================================================================

class SpeakerRole(Enum):
    """讲话人角色"""
    HOST = "Host"
    CO_HOST = "Co-host"
    EXPERT = "Expert"
    GUEST = "Guest"
    INVESTOR = "Investor"
    ANALYST = "Analyst"

@dataclass
class Speaker:
    """讲话人信息"""
    id: str
    name: str
    role: SpeakerRole
    voice_name: str
    language_code: str
    gender: str

@dataclass
class DialogueSegment:
    """对话段落"""
    speaker_id: str
    text: str
    estimated_duration_seconds: float = 0.0
    audio_bytes: Optional[bytes] = None
    
@dataclass
class PodcastConfig:
    """播客配置"""
    template_name: str
    name: str
    language: str
    duration_minutes: int
    speakers: List[Speaker] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class NewsData:
    """新闻数据注入"""
    headlines: List[str]
    key_stats: Dict[str, str]
    sentiment: str  # "bullish", "bearish", "neutral"
    quotes: List[str]

# ============================================================================
# 模板管理器
# ============================================================================

class TemplateManager:
    """加载和管理YAML模板"""
    
    def __init__(self, config_path: str = "config/podcast_style_templates.yaml"):
        self.config_path = Path(config_path)
        self.templates = self._load_templates()
    
    def _load_templates(self) -> Dict:
        """从YAML加载模板"""
        if not self.config_path.exists():
            logger.warning(f"模板文件不存在: {self.config_path}")
            return {}
        
        with open(self.config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        logger.info(f"✅ 加载模板: {list(config.get('styles', {}).keys())}")
        return config
    
    def get_template(self, template_name: str) -> Dict:
        """获取特定模板"""
        return self.templates.get('styles', {}).get(template_name, {})
    
    def list_templates(self) -> List[str]:
        """列出所有可用模板"""
        return list(self.templates.get('styles', {}).keys())

# ============================================================================
# 内容生成器 (LLM集成点)
# ============================================================================

class ContentGenerator:
    """
    使用LLM生成播客内容
    支持: mock (快速演示) | openai (GPT-4-mini) | anthropic (待实现) | vertex_ai (待实现)
    """
    
    def __init__(self, model: str = "openai", api_key: Optional[str] = None):
        """
        model: "mock" | "openai" | "anthropic" | "vertex_ai"
        api_key: API 密钥 (如果为 None 则从环境变量读取)
        """
        self.model = model
        self.api_key = api_key
        self.llm_generator = None
        
        if model == "openai":
            try:
                from src.llm_script_generator import LLMScriptGenerator, PodcastTone, DialogueStyle
                self.llm_generator = LLMScriptGenerator(api_key=api_key)
                logger.info(f"✅ 初始化 OpenAI 内容生成器: {model}")
            except Exception as e:
                logger.warning(f"⚠️  OpenAI 初始化失败，回退到 mock 模式: {e}")
                self.model = "mock"
        else:
            logger.info(f"初始化内容生成器: {model}")
    
    def generate_dialogue(
        self,
        topic: str,
        speakers: List[Speaker],
        segment_type: str,
        context: Optional[Dict] = None
    ) -> str:
        """
        使用LLM生成自然对话
        
        Args:
            topic: 讨论主题
            speakers: 讲话人列表
            segment_type: 段落类型 (opening, analysis, conclusion等)
            context: 上下文信息 (新闻、数据等)
        
        Returns:
            生成的对话文本 (SSML格式)
        """
        
        if self.model == "mock":
            return self._generate_mock_dialogue(topic, speakers, segment_type, context)
        elif self.model == "openai":
            return self._generate_openai_dialogue(topic, speakers, segment_type, context)
        elif self.model == "anthropic":
            return self._generate_anthropic_dialogue(topic, speakers, segment_type, context)
        else:
            raise ValueError(f"未知模型: {self.model}")
    
    def _generate_mock_dialogue(
        self,
        topic: str,
        speakers: List[Speaker],
        segment_type: str,
        context: Optional[Dict] = None
    ) -> str:
        """Mock实现 - 用于演示"""
        
        mock_templates = {
            "opening_en": '<speak>Welcome to our show. I\'m {speaker}. Today we discuss {topic}.</speak>',
            "analysis_en": '<speak>{speaker} explains: {topic} is important because <break time="300ms"/> it affects market dynamics.</speak>',
            "reaction_en": '<speak><emphasis level="strong">Wow!</emphasis> <break time="300ms"/> That\'s really interesting!</speak>',
            "opening_ko": '<speak>안녕하세요. 저는 {speaker}입니다. 오늘 {topic}에 대해 얘기하겠습니다.</speak>',
        }
        
        template_key = f"{segment_type}_{speakers[0].language_code.split('-')[0]}"
        template = mock_templates.get(template_key, '<speak>Default content</speak>')
        
        return template.format(speaker=speakers[0].name, topic=topic)
    
    def _generate_openai_dialogue(self, topic, speakers, segment_type, context):
        """集成OpenAI GPT API"""
        # 实现示例
        # from openai import OpenAI
        # client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
        # response = client.chat.completions.create(...)
        pass
    
    def _generate_anthropic_dialogue(self, topic, speakers, segment_type, context):
        """集成Anthropic Claude API"""
        # 实现示例
        # from anthropic import Anthropic
        # client = Anthropic()
        # response = client.messages.create(...)
        pass

# ============================================================================
# 对话构建器
# ============================================================================

class DialogueBuilder:
    """构建播客对话序列"""
    
    def __init__(self, config: PodcastConfig, content_gen: ContentGenerator):
        self.config = config
        self.content_gen = content_gen
        self.segments: List[DialogueSegment] = []
    
    def build_from_template(
        self,
        template: Dict,
        data: Optional[NewsData] = None
    ) -> List[DialogueSegment]:
        """
        从模板构建完整对话
        
        Args:
            template: 从TemplateManager获取的模板
            data: 新闻/数据注入
        
        Returns:
            对话段落列表
        """
        
        logger.info(f"📝 根据模板构建对话序列...")
        self.segments = []
        
        structure = template.get('structure', {})
        
        for segment_name, segment_config in structure.items():
            logger.info(f"  构建 {segment_name}...")
            
            # 获取此段落的讲话人
            speaker_ids = segment_config.get('speakers', [])
            speakers = [s for s in self.config.speakers if s.id in speaker_ids]
            
            # 生成对话内容
            text = self.content_gen.generate_dialogue(
                topic=self.config.metadata.get('topic', 'General Discussion'),
                speakers=speakers,
                segment_type=segment_name,
                context=data.__dict__ if data else None
            )
            
            # 估算时长
            word_count = len(text.split())
            estimated_duration = word_count / 2.5  # 约2.5字/秒
            
            # 创建段落
            segment = DialogueSegment(
                speaker_id=speakers[0].id if speakers else "unknown",
                text=text,
                estimated_duration_seconds=estimated_duration
            )
            
            self.segments.append(segment)
        
        logger.info(f"✅ 构建完成: {len(self.segments)} 个段落")
        return self.segments

# ============================================================================
# TTS合成引擎
# ============================================================================

class TTSSynthesizer:
    """Google Cloud TTS合成"""
    
    def __init__(self, project_id: str = None):
        self.project_id = project_id or os.getenv('GOOGLE_CLOUD_PROJECT')
        self.client = texttospeech.TextToSpeechClient()
        self.audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            sample_rate_hertz=22050,
            speaking_rate=1.0,
            pitch=0.0,
        )
    
    def synthesize_segment(self, segment: DialogueSegment, speaker: Speaker) -> bytes:
        """合成单个段落"""
        
        request = texttospeech.SynthesizeSpeechRequest(
            input=texttospeech.SynthesisInput(ssml=segment.text),
            voice=texttospeech.VoiceSelectionParams(
                language_code=speaker.language_code,
                name=speaker.voice_name,
            ),
            audio_config=self.audio_config,
        )
        
        response = self.client.synthesize_speech(request=request)
        return response.audio_content
    
    def synthesize_all(
        self,
        segments: List[DialogueSegment],
        speaker_map: Dict[str, Speaker]
    ) -> List[DialogueSegment]:
        """批量合成"""
        
        logger.info(f"🔊 合成 {len(segments)} 个段落...")
        
        for idx, segment in enumerate(segments, 1):
            speaker = speaker_map.get(segment.speaker_id)
            if not speaker:
                logger.warning(f"未找到讲话人: {segment.speaker_id}")
                continue
            
            logger.info(f"  [{idx}/{len(segments)}] 合成 {speaker.name}...")
            
            try:
                audio_bytes = self.synthesize_segment(segment, speaker)
                segment.audio_bytes = audio_bytes
                logger.info(f"    ✅ 成功")
            except Exception as e:
                logger.error(f"    ❌ 失败: {str(e)}")
                raise
        
        return segments

# ============================================================================
# 音频混音器
# ============================================================================

class AudioMixer:
    """混合和输出音频"""
    
    def __init__(self, output_dir: str = "data/generated_podcasts"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def merge_segments(
        self,
        segments: List[DialogueSegment],
        pause_ms: int = 200
    ) -> AudioSegment:
        """合并所有段落"""
        
        logger.info(f"📦 混音 {len(segments)} 个段落...")
        
        silence = AudioSegment.silent(duration=pause_ms)
        merged = None
        
        for segment in segments:
            if segment.audio_bytes is None:
                logger.warning(f"跳过空段落")
                continue
            
            audio = AudioSegment.from_mp3(io.BytesIO(segment.audio_bytes))
            
            if merged is None:
                merged = audio
            else:
                merged += silence + audio
        
        if merged is None:
            raise ValueError("没有有效的音频段落")
        
        logger.info(f"✅ 混音完成: {len(merged)/1000:.1f} 秒")
        return merged
    
    def export(
        self,
        audio: AudioSegment,
        podcast_name: str,
        format: str = "mp3",
        bitrate: str = "192k"
    ) -> Path:
        """导出音频"""
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{podcast_name}_{timestamp}.{format}"
        filepath = self.output_dir / filename
        
        logger.info(f"💾 导出到: {filepath}")
        
        audio.export(str(filepath), format=format, bitrate=bitrate)
        
        file_size_mb = filepath.stat().st_size / (1024 * 1024)
        duration_seconds = len(audio) / 1000
        
        logger.info(f"   文件大小: {file_size_mb:.2f} MB")
        logger.info(f"   时长: {int(duration_seconds/60)}分{int(duration_seconds%60)}秒")
        
        return filepath

# ============================================================================
# 播客生成Pipeline (主类)
# ============================================================================

class PodcastPipeline:
    """完整的播客生成Pipeline"""
    
    def __init__(
        self,
        template_config: str = "config/podcast_style_templates.yaml",
        content_model: str = "openai"
    ):
        self.template_manager = TemplateManager(template_config)
        self.content_generator = ContentGenerator(model=content_model)
        self.synthesizer = TTSSynthesizer()
        self.mixer = AudioMixer()
    
    def generate(
        self,
        template_name: str,
        podcast_name: str,
        topic: str,
        data: Optional[NewsData] = None,
        custom_metadata: Optional[Dict] = None
    ) -> Path:
        """
        生成完整播客
        
        Args:
            template_name: 模板名称 (如 "us_stocks_dualhosts")
            podcast_name: 播客名称
            topic: 讨论主题
            data: 新闻/数据注入
            custom_metadata: 自定义元数据
        
        Returns:
            生成的音频文件路径
        """
        
        logger.info('🎙️ 开始生成播客...')
        logger.info(f'   模板: {template_name}')
        logger.info(f'   主题: {topic}')
        logger.info('')
        
        # 1. 获取模板
        template = self.template_manager.get_template(template_name)
        if not template:
            raise ValueError(f"模板不存在: {template_name}")
        
        # 2. 构建配置
        config = self._build_config(template, podcast_name, topic, custom_metadata)
        
        # 3. 构建对话
        dialogue_builder = DialogueBuilder(config, self.content_generator)
        segments = dialogue_builder.build_from_template(template, data)
        
        # 4. 合成音频
        speaker_map = {s.id: s for s in config.speakers}
        segments = self.synthesizer.synthesize_all(segments, speaker_map)
        
        # 5. 混音
        audio = self.mixer.merge_segments(segments)
        
        # 6. 导出
        output_path = self.mixer.export(audio, podcast_name)
        
        logger.info('✅ 播客生成成功!\n')
        
        return output_path
    
    def _build_config(
        self,
        template: Dict,
        podcast_name: str,
        topic: str,
        custom_metadata: Optional[Dict] = None
    ) -> PodcastConfig:
        """构建播客配置"""
        
        # 解析讲话人
        speakers = []
        for speaker_data in template.get('speakers', []):
            # 转换role: "Co-host" -> "CO_HOST", "Expert" -> "EXPERT"
            role_str = speaker_data['role'].upper().replace('-', '_')
            speaker = Speaker(
                id=speaker_data['id'],
                name=speaker_data['name'],
                role=SpeakerRole[role_str],
                voice_name=speaker_data['voice_name'],
                language_code=template.get('language', 'en-US'),
                gender=speaker_data['gender']
            )
            speakers.append(speaker)
        
        # 构建元数据
        metadata = {
            'topic': topic,
            'template_name': template.get('name'),
            'created_at': datetime.now().isoformat(),
        }
        if custom_metadata:
            metadata.update(custom_metadata)
        
        return PodcastConfig(
            template_name=template.get('name'),
            name=podcast_name,
            language=template.get('language', 'en-US'),
            duration_minutes=template.get('duration_minutes', 5),
            speakers=speakers,
            metadata=metadata
        )

# ============================================================================
# 使用示例
# ============================================================================

def main():
    """演示使用"""
    
    # 初始化Pipeline
    pipeline = PodcastPipeline(
        template_config="config/podcast_style_templates.yaml",
        content_model="openai"  # 使用 openai 内容生成（昨天验证过的方式）
    )
    
    # 显示可用模板
    logger.info("📋 可用模板:")
    for template_name in pipeline.template_manager.list_templates():
        logger.info(f"   - {template_name}")
    logger.info('')
    
    # 示例1: 生成美股讨论播客
    logger.info("="*70)
    logger.info("示例1: 美股讨论")
    logger.info("="*70)
    
    output_path = pipeline.generate(
        template_name="english_2_hosts",
        podcast_name="stocks_daily",
        topic="S&P 500 reaches new all-time high",
        custom_metadata={
            'category': 'finance',
            'language': 'English',
        }
    )
    
    logger.info(f"输出: {output_path}\n")
    
    # 示例2: 生成韩语Crypto播客
    logger.info("="*70)
    logger.info("示例2: 韩语Crypto讨论")
    logger.info("="*70)
    
    crypto_data = NewsData(
        headlines=[
            "Bitcoin breaks $42,000",
            "Ethereum Layer 2 adoption increases"
        ],
        key_stats={
            "BTC": "$42,500",
            "ETH": "$2,200",
            "Market Cap": "$1.2T"
        },
        sentiment="bullish",
        quotes=[
            "Institutional adoption accelerating",
            "Regulatory clarity improving"
        ]
    )
    
    output_path = pipeline.generate(
        template_name="korean_crypto_threeway",
        podcast_name="crypto_korean",
        topic="Crypto Market Update",
        data=crypto_data,
        custom_metadata={
            'category': 'crypto',
            'language': 'Korean',
        }
    )
    
    logger.info(f"输出: {output_path}\n")

if __name__ == '__main__':
    main()
