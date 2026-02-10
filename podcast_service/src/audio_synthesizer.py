#!/usr/bin/env python3
"""
音频合成器 - 将播客脚本合成为 MP3 音频
使用 Google Cloud Text-to-Speech 和 pydub 的方式
（复用昨天验证过的技术）
"""

import os
import io
import logging
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime

from google.cloud import texttospeech
from pydub import AudioSegment

# Import cost calculator
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from cost_calculator import UsageMetrics

# ============================================================================
# 日志配置
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# 数据模型
# ============================================================================

@dataclass
class SpeakerVoiceConfig:
    """讲话人声音配置"""
    speaker_id: str
    speaker_name: str
    language_code: str
    voice_name: str
    ssml_gender: texttospeech.SsmlVoiceGender

# ============================================================================
# 音频合成器
# ============================================================================

class AudioSynthesizer:
    """
    使用 Google Cloud TTS 和 pydub 合成播客音频
    这是昨天验证过的成功方法
    """
    
    def __init__(self, project_id: str = None, speaking_rate: float = 1.0):
        """初始化合成器
        
        Args:
            project_id: Google Cloud 项目 ID
            speaking_rate: 语速 (0.5 = 慢速, 1.0 = 正常, 1.5 = 快速, 最大 2.0)
        """
        self.project_id = project_id or os.getenv('GOOGLE_CLOUD_PROJECT', 'able-engine-466308-q2')
        self.client = texttospeech.TextToSpeechClient()
        self.output_dir = Path('data/generated_podcasts')
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # 音频配置（支持动态语速）
        self.audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            sample_rate_hertz=22050,
            speaking_rate=speaking_rate,
            pitch=0.0,
        )
        
        logger.info(f"✅ 音频合成器初始化完成 (项目: {self.project_id}, 语速: {speaking_rate}x)")
    
    def synthesize_segment(self, ssml_text: str, voice_config: SpeakerVoiceConfig) -> tuple:
        """
        合成单个音频段落
        
        Args:
            ssml_text: SSML 格式的文本
            voice_config: 讲话人声音配置
        
        Returns:
            (MP3字节数据, 字符数)
        """
        try:
            request = texttospeech.SynthesizeSpeechRequest(
                input=texttospeech.SynthesisInput(ssml=ssml_text),
                voice=texttospeech.VoiceSelectionParams(
                    language_code=voice_config.language_code,
                    name=voice_config.voice_name,
                    ssml_gender=voice_config.ssml_gender,
                ),
                audio_config=self.audio_config,
            )
            
            response = self.client.synthesize_speech(request=request)
            # Count characters in SSML text (excluding SSML tags)
            import re
            text_only = re.sub(r'<[^>]+>', '', ssml_text)
            char_count = len(text_only)
            return response.audio_content, char_count
        
        except Exception as e:
            logger.error(f"❌ 合成失败 ({voice_config.speaker_name}): {e}")
            raise
    
    def generate_from_script(
        self,
        script_data: Dict,
        podcast_name: str = None,
        speaker_voice_map: Dict[str, SpeakerVoiceConfig] = None
    ) -> tuple:
        """
        从脚本数据生成完整播客 MP3
        
        Args:
            script_data: LLMScriptGenerator 生成的脚本 JSON
            podcast_name: 播客名称（自动生成如果为空）
            speaker_voice_map: 讲话人到声音的映射
        
        Returns:
            (生成的MP3文件路径, TTS字符数, 音频时长秒, 文件大小字节)
        """
        
        # 生成文件名：podcast_{内容描述}_{时间戳}
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        if not podcast_name:
            # 从脚本标题生成描述（去除特殊字符，只保留字母、数字、下划线）
            title = script_data.get('title', 'podcast')
            # 将标题转为小写，替换空格和特殊字符为下划线
            description = ''.join(
                c if c.isalnum() or c == '_' else '_' 
                for c in title.lower()
            )
            # 移除连续的下划线
            description = '_'.join(filter(None, description.split('_')))
            podcast_name = f"podcast_{description}_{timestamp}"
        else:
            podcast_name = f"podcast_{podcast_name}_{timestamp}"
        
        logger.info(f"\n🎙️ 开始生成播客音频...")
        logger.info(f"   标题: {script_data.get('title')}")
        logger.info(f"   主题: {script_data.get('topic')}")
        logger.info(f"   讲话人数: {script_data.get('num_speakers')}")
        logger.info(f"   时长: ~{script_data.get('estimated_duration_seconds'):.0f}秒")
        logger.info(f"   文件名: podcast_{podcast_name}.mp3")
        logger.info("")
        
        # 默认声音映射（如果未提供）
        if not speaker_voice_map:
            speaker_voice_map = self._get_default_voice_map(
                script_data.get('language', 'en-US'),
                script_data.get('num_speakers', 2)
            )
        
        # 合成所有段落
        segments_audio = []
        segments = script_data.get('segments', [])
        total_tts_chars = 0
        
        logger.info(f"正在合成 {len(segments)} 个对话段落...\n")
        
        for idx, segment in enumerate(segments, 1):
            speaker_id = segment.get('speaker_id')
            speaker_name = segment.get('speaker_name')
            ssml_text = segment.get('ssml_text')
            
            if not ssml_text:
                logger.warning(f"[{idx}/{len(segments)}] ⚠️  无 SSML 文本: {speaker_name}")
                continue
            
            # 获取声音配置
            voice_config = speaker_voice_map.get(speaker_id)
            if not voice_config:
                logger.warning(f"[{idx}/{len(segments)}] ⚠️  无声音配置: {speaker_id}")
                continue
            
            logger.info(f"[{idx}/{len(segments)}] 合成 {speaker_name} ({voice_config.voice_name})")
            
            try:
                # 合成 - 现在返回 (audio_bytes, char_count)
                audio_bytes, char_count = self.synthesize_segment(ssml_text, voice_config)
                total_tts_chars += char_count
                audio = AudioSegment.from_mp3(io.BytesIO(audio_bytes))
                segments_audio.append(audio)
                
                duration_sec = len(audio) / 1000
                logger.info(f"              ✅ 成功 ({duration_sec:.1f}s, {char_count}字符)\n")
            
            except Exception as e:
                logger.error(f"              ❌ 失败: {e}\n")
                raise
        
        if not segments_audio:
            raise ValueError("没有成功合成的音频段落")
        
        logger.info(f"✅ 所有段落合成完成\n")
        
        # 合并所有段落
        logger.info("📦 合并音频段落...")
        merged = self._merge_segments(segments_audio)
        
        # 导出 MP3
        output_file = self.output_dir / f"{podcast_name}.mp3"
        merged.export(str(output_file), format='mp3', bitrate='192k')
        
        # 统计信息
        duration_sec = len(merged) / 1000
        file_size_bytes = output_file.stat().st_size
        file_size_mb = file_size_bytes / (1024 * 1024)
        
        logger.info(f"✅ 播客生成成功!")
        logger.info(f"   输出文件: {output_file.name}")
        logger.info(f"   文件大小: {file_size_mb:.2f} MB")
        logger.info(f"   实际时长: {int(duration_sec // 60)}分{int(duration_sec % 60)}秒")
        logger.info(f"   TTS字符数: {total_tts_chars}")
        logger.info("")
        
        return output_file, total_tts_chars, duration_sec, file_size_bytes
    
    def _merge_segments(self, segments: List[AudioSegment], pause_ms: int = 200) -> AudioSegment:
        """
        合并音频段落
        
        Args:
            segments: 音频段落列表
            pause_ms: 段落间停顿时长（毫秒）
        
        Returns:
            合并后的音频
        """
        if not segments:
            raise ValueError("没有音频段落可合并")
        
        silence = AudioSegment.silent(duration=pause_ms)
        
        merged = segments[0]
        for segment in segments[1:]:
            merged += silence
            merged += segment
        
        return merged
    
    def _get_default_voice_map(
        self,
        language_code: str = 'en-US',
        num_speakers: int = 2
    ) -> Dict[str, SpeakerVoiceConfig]:
        """
        获取默认声音映射
        
        Args:
            language_code: 语言代码 (如 'en-US', 'ko-KR', 'zh-CN')
            num_speakers: 讲话人数量
        
        Returns:
            讲话人 ID 到声音配置的映射
        """
        
        voice_maps = {
            'en-US': {
                'speaker_1': SpeakerVoiceConfig(
                    speaker_id='speaker_1',
                    speaker_name='Speaker 1',
                    language_code='en-US',
                    voice_name='en-US-Neural2-I',  # 男性
                    ssml_gender=texttospeech.SsmlVoiceGender.MALE,
                ),
                'speaker_2': SpeakerVoiceConfig(
                    speaker_id='speaker_2',
                    speaker_name='Speaker 2',
                    language_code='en-US',
                    voice_name='en-US-Neural2-F',  # 女性
                    ssml_gender=texttospeech.SsmlVoiceGender.FEMALE,
                ),
                'speaker_3': SpeakerVoiceConfig(
                    speaker_id='speaker_3',
                    speaker_name='Speaker 3',
                    language_code='en-US',
                    voice_name='en-US-Neural2-J',  # 男性
                    ssml_gender=texttospeech.SsmlVoiceGender.MALE,
                ),
            },
            'ko-KR': {
                'speaker_1': SpeakerVoiceConfig(
                    speaker_id='speaker_1',
                    speaker_name='Speaker 1',
                    language_code='ko-KR',
                    voice_name='ko-KR-Neural2-A',  # 男性
                    ssml_gender=texttospeech.SsmlVoiceGender.MALE,
                ),
                'speaker_2': SpeakerVoiceConfig(
                    speaker_id='speaker_2',
                    speaker_name='Speaker 2',
                    language_code='ko-KR',
                    voice_name='ko-KR-Neural2-B',  # 女性
                    ssml_gender=texttospeech.SsmlVoiceGender.FEMALE,
                ),
                'speaker_3': SpeakerVoiceConfig(
                    speaker_id='speaker_3',
                    speaker_name='Speaker 3',
                    language_code='ko-KR',
                    voice_name='ko-KR-Neural2-C',  # 男性
                    ssml_gender=texttospeech.SsmlVoiceGender.MALE,
                ),
            },
            'zh-CN': {
                'speaker_1': SpeakerVoiceConfig(
                    speaker_id='speaker_1',
                    speaker_name='Speaker 1',
                    language_code='zh-CN',
                    voice_name='cmn-CN-Neural2-A',  # 男性
                    ssml_gender=texttospeech.SsmlVoiceGender.MALE,
                ),
                'speaker_2': SpeakerVoiceConfig(
                    speaker_id='speaker_2',
                    speaker_name='Speaker 2',
                    language_code='zh-CN',
                    voice_name='cmn-CN-Neural2-B',  # 女性
                    ssml_gender=texttospeech.SsmlVoiceGender.FEMALE,
                ),
                'speaker_3': SpeakerVoiceConfig(
                    speaker_id='speaker_3',
                    speaker_name='Speaker 3',
                    language_code='zh-CN',
                    voice_name='cmn-CN-Neural2-D',  # 男性
                    ssml_gender=texttospeech.SsmlVoiceGender.MALE,
                ),
            },
        }
        
        # 获取该语言的映射，或使用英文作为备选
        voice_map_for_lang = voice_maps.get(language_code, voice_maps['en-US'])
        
        # 返回需要的讲话人数量
        result = {}
        for i in range(1, num_speakers + 1):
            speaker_id = f'speaker_{i}'
            if speaker_id in voice_map_for_lang:
                result[speaker_id] = voice_map_for_lang[speaker_id]
        
        return result
