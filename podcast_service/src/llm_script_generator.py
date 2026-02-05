#!/usr/bin/env python3
"""
LLM 播客脚本生成器
使用 OpenAI GPT-4-mini 从任意 topic/content 生成播客脚本
支持多语言、多讲话人、多种格式

功能：
1. 从自由形式的内容生成结构化的播客脚本
2. 支持不同的讲话人数量和角色
3. 生成 SSML 格式的语音友好文本
4. 支持自定义语调、风格、时长等
"""

import os
import json
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import openai
from openai import OpenAI

# ============================================================================
# 日志配置
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# 枚举定义
# ============================================================================

class PodcastTone(Enum):
    """播客语调"""
    PROFESSIONAL = "professional"       # 专业严肃
    CASUAL = "casual"                   # 随意轻松
    EDUCATIONAL = "educational"         # 教育性
    ENTERTAINING = "entertaining"       # 娱乐性
    INVESTIGATIVE = "investigative"     # 调查性
    STORYTELLING = "storytelling"       # 故事叙述
    HUMOROUS = "humorous"               # 幽默
    DEBATE = "debate"                   # 辩论

class DialogueStyle(Enum):
    """对话风格"""
    MONOLOGUE = "monologue"             # 单人独白
    INTERVIEW = "interview"             # 采访对话
    DEBATE = "debate"                   # 辩论讨论
    CONVERSATION = "conversation"       # 随意对话
    NARRATION = "narration"             # 旁白解说
    PANEL = "panel"                     # 专家论坛

# ============================================================================
# 数据模型
# ============================================================================

@dataclass
class ScriptSegment:
    """脚本段落"""
    speaker_id: str
    speaker_name: str
    text: str              # 原始文本
    ssml_text: str         # SSML 格式
    duration_seconds: float
    segment_type: str      # "opening", "main", "closing" 等
    notes: Optional[str] = None

@dataclass
class PodcastScript:
    """完整播客脚本"""
    topic: str
    title: str
    description: str
    language: str
    tone: PodcastTone
    dialogue_style: DialogueStyle
    num_speakers: int
    estimated_duration_seconds: float
    segments: List[ScriptSegment] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    token_usage: Optional[Dict[str, int]] = None  # {prompt_tokens, completion_tokens, total_tokens}
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        result = {
            'topic': self.topic,
            'title': self.title,
            'description': self.description,
            'language': self.language,
            'tone': self.tone.value,
            'dialogue_style': self.dialogue_style.value,
            'num_speakers': self.num_speakers,
            'estimated_duration_seconds': self.estimated_duration_seconds,
            'segments': [
                {
                    'speaker_id': seg.speaker_id,
                    'speaker_name': seg.speaker_name,
                    'text': seg.text,
                    'ssml_text': seg.ssml_text,
                    'duration_seconds': seg.duration_seconds,
                    'segment_type': seg.segment_type,
                    'notes': seg.notes
                }
                for seg in self.segments
            ],
            'metadata': self.metadata
        }
        if self.token_usage:
            result['token_usage'] = self.token_usage
        return result

# ============================================================================
# LLM 脚本生成器
# ============================================================================

class LLMScriptGenerator:
    """使用 OpenAI LLM 生成播客脚本"""
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gpt-4o-mini"
    ):
        """
        初始化生成器
        
        Args:
            api_key: OpenAI API key (默认从环境变量读取)
            model: 使用的模型 (默认 gpt-4o-mini - 轻量级且高效)
        """
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY 环境变量未设置")
        
        self.model = model
        self.client = OpenAI(api_key=self.api_key)
        
        logger.info(f"✅ LLM 脚本生成器初始化完成 (model={model})")

    def _is_gpt5_model(self) -> bool:
        return self.model.lower().startswith("gpt-5")

    def _chat_completions_create_by_model(
        self,
        messages: List[Dict[str, str]],
        max_output_tokens: int,
        temperature: float = 0.7,
        top_p: float = 0.95,
    ):
        """
        按模型分开逻辑：
        - gpt-5 系列：只使用 max_completion_tokens，不传 temperature/top_p
        - 非 gpt-5：使用 max_tokens + temperature/top_p
        """
        if self._is_gpt5_model():
            return self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_completion_tokens=max_output_tokens,
            )
        return self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_output_tokens,
        )
    
    def generate_script(
        self,
        topic: str,
        num_speakers: int = 2,
        duration_minutes: int = 5,
        language: str = "en-US",
        tone: PodcastTone = PodcastTone.PROFESSIONAL,
        dialogue_style: DialogueStyle = DialogueStyle.CONVERSATION,
        speaker_names: Optional[List[str]] = None,
        template_speaker_ids: Optional[List[str]] = None,
        additional_context: Optional[str] = None,
        custom_instructions: Optional[str] = None
    ) -> PodcastScript:
        """
        从 topic 生成播客脚本
        
        Args:
            topic: 播客主题/内容
            num_speakers: 讲话人数量
            duration_minutes: 目标时长（分钟）
            language: 语言代码 (en-US, zh-CN, ko-KR 等)
            tone: 语调风格
            dialogue_style: 对话风格
            speaker_names: 讲话人名字列表
            additional_context: 额外背景信息
            custom_instructions: 自定义生成指令
        
        Returns:
            PodcastScript 对象
        """
        
        logger.info(f"🎙️ 开始生成播客脚本...")
        logger.info(f"   Topic: {topic[:60]}...")
        logger.info(f"   Speakers: {num_speakers}")
        logger.info(f"   Duration: {duration_minutes} min")
        logger.info(f"   Language: {language}")
        logger.info(f"   Tone: {tone.value}")
        logger.info(f"   Style: {dialogue_style.value}")
        
        # IMPORTANT:
        # If caller does not provide `speaker_names` (None) we should NOT
        # auto-fill them here. Previously we auto-generated placeholder
        # names which then forced the LLM to reuse generic names. Keep
        # `speaker_names` as None so the LLM can generate realistic human
        # names based on role/gender when instructed.
        
        # 构建提示词
        system_prompt = self._build_system_prompt(
            tone, dialogue_style, language
        )
        
        user_prompt = self._build_user_prompt(
            topic=topic,
            num_speakers=num_speakers,
            duration_minutes=duration_minutes,
            language=language,
            speaker_names=speaker_names,
            additional_context=additional_context,
            custom_instructions=custom_instructions
        )
        
        logger.info(f"\n📝 调用 LLM 生成脚本...")
        
        # 调用 OpenAI API（按模型分支）
        try:
            response = self._chat_completions_create_by_model(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_output_tokens=8000,
                temperature=0.7,
                top_p=0.95,
            )
            
            # Extract response content
            script_json = response.choices[0].message.content
            logger.info(f"✅ LLM 响应收到")

            # 提取 token 使用统计
            usage_dict = None
            if hasattr(response, 'usage') and response.usage:
                try:
                    # OpenAI SDK 返回的 usage 是一个对象，转换为字典
                    usage_obj = response.usage
                    usage_dict = {
                        'prompt_tokens': usage_obj.prompt_tokens,
                        'completion_tokens': usage_obj.completion_tokens,
                        'total_tokens': usage_obj.total_tokens
                    }
                    logger.info(f"📊 LLM Token 使用统计:")
                    logger.info(f"   Prompt tokens: {usage_dict['prompt_tokens']}")
                    logger.info(f"   Completion tokens: {usage_dict['completion_tokens']}")
                    logger.info(f"   Total tokens: {usage_dict['total_tokens']}")
                except Exception as e:
                    logger.warning(f"⚠️ 无法解析 token 使用统计: {e}")
            
            # 解析 JSON 响应
            script_data = json.loads(script_json)

            # 构建 PodcastScript 对象
            script = self._parse_script_response(
                script_data,
                topic=topic,
                tone=tone,
                dialogue_style=dialogue_style,
                language=language,
                num_speakers=num_speakers,
                template_speaker_ids=template_speaker_ids
            )

            # 将 usage 信息放入脚本 metadata（以便保存/审计）
            if usage_dict:
                script.metadata['usage'] = usage_dict
            
            logger.info(f"✅ 脚本初次生成完成")
            logger.info(f"   段落数: {len(script.segments)}")
            logger.info(f"   预计时长: {script.estimated_duration_seconds:.1f} 秒")
            
            # 检查是否达到目标时长，如果不够则进行扩展
            target_duration = duration_minutes * 60
            expansion_attempts = 0
            max_expansions = 3
            
            while script.estimated_duration_seconds < target_duration * 0.85 and expansion_attempts < max_expansions:
                expansion_attempts += 1
                logger.info(f"\n🔄 内容长度不足，进行第 {expansion_attempts} 次扩展...")
                logger.info(f"   当前时长: {script.estimated_duration_seconds:.1f}s")
                logger.info(f"   目标时长: {target_duration}s")
                
                # 调用扩展方法
                expanded_script = self._expand_script(
                    script, target_duration, language, tone, dialogue_style,
                    template_speaker_ids=template_speaker_ids
                )
                
                # 累积token使用统计
                if hasattr(expanded_script, 'metadata') and 'usage' in expanded_script.metadata:
                    expansion_usage = expanded_script.metadata['usage']
                    if usage_dict:
                        # 累加token统计
                        usage_dict['prompt_tokens'] += expansion_usage.get('prompt_tokens', 0)
                        usage_dict['completion_tokens'] += expansion_usage.get('completion_tokens', 0)
                        usage_dict['total_tokens'] += expansion_usage.get('total_tokens', 0)
                        script.metadata['usage'] = usage_dict
                    
                    logger.info(f"📊 扩展轮次Token统计:")
                    logger.info(f"   +Prompt tokens: {expansion_usage.get('prompt_tokens', 0)}")
                    logger.info(f"   +Completion tokens: {expansion_usage.get('completion_tokens', 0)}")
                    logger.info(f"   累计Total tokens: {usage_dict.get('total_tokens', 0)}")
                
                script = expanded_script
                logger.info(f"✅ 扩展完成，新时长: {script.estimated_duration_seconds:.1f}s")
            
            if expansion_attempts > 0:
                logger.info(f"\n✅ 经过 {expansion_attempts} 次扩展后生成完成")
            logger.info(f"   最终段落数: {len(script.segments)}")
            logger.info(f"   最终预计时长: {script.estimated_duration_seconds:.1f} 秒")
            
            # 将累积的 token 使用统计设置到脚本对象
            if usage_dict:
                script.token_usage = usage_dict
                logger.info(f"📊 最终累积 Token 统计: {usage_dict['total_tokens']} tokens")
            
            return script
            
        except json.JSONDecodeError as e:
            logger.error(f"❌ JSON 解析失败: {e}")
            logger.error(f"原始响应: {script_json}")
            raise ValueError("LLM 返回的不是有效的 JSON 格式")
        
        except Exception as e:
            logger.error(f"❌ LLM 调用失败: {e}")
            raise

    def _build_system_prompt(
        self,
        tone: PodcastTone,
        dialogue_style: DialogueStyle,
        language: str
    ) -> str:
        """构建系统提示词"""
        
        tone_desc = {
            PodcastTone.PROFESSIONAL: "保持专业、严肃的语气",
            PodcastTone.CASUAL: "轻松、随意的对话风格",
            PodcastTone.EDUCATIONAL: "教育性、讲解性的内容",
            PodcastTone.ENTERTAINING: "娱乐性强、吸引听众",
            PodcastTone.INVESTIGATIVE: "深度调查、批判性思维",
            PodcastTone.STORYTELLING: "故事叙述风格、引人入胜",
            PodcastTone.HUMOROUS: "幽默、轻松、能逗笑",
            PodcastTone.DEBATE: "辩论性、观点碰撞",
        }
        
        style_desc = {
            DialogueStyle.MONOLOGUE: "单人独白、讲者主导",
            DialogueStyle.INTERVIEW: "采访形式、问答互动",
            DialogueStyle.DEBATE: "辩论形式、观点对立",
            DialogueStyle.CONVERSATION: "随意对话、自然流畅",
            DialogueStyle.NARRATION: "旁白解说、信息传达",
            DialogueStyle.PANEL: "专家论坛、多人讨论",
        }
        
        lang_indicator = {
            "en-US": "英文",
            "en-GB": "英文",
            "zh-CN": "中文（简体）",
            "zh-TW": "中文（繁体）",
            "ko-KR": "韩文",
            "ja-JP": "日文",
        }.get(language, "英文")
        
        return f"""你是一位资深的播客脚本编剧和内容策划专家，专门创作深入、详细、高质量的长篇播客内容。

你的任务是生成高质量的、内容丰富的播客脚本，满足以下要求：

1. **语言**: {lang_indicator}
2. **语调**: {tone_desc.get(tone, "自然流畅")}
3. **风格**: {style_desc.get(dialogue_style, "自然对话")}

**核心原则 - 内容长度和深度**:
- 你必须生成完整、详细的内容，不要因为担心篇幅而缩短讨论
- 每个主题都要深入展开，包含具体例子、数据、故事和案例
- 讲话人之间要有充分的互动和来回对话
- 宁可内容丰富而略长，也不要内容单薄而过短
- 用户指定的目标时长是最低要求，你应该努力达到或超过这个时长

生成脚本时，请遵循以下指南：
- 内容真实、可信，避免虚构事实
- 每个段落应该有明确的讲话人
- 对话应该自然、有节奏，适合口头表达，但也要有足够的信息密度
- 包含自然的停顿、语气变化等指示
- 返回格式必须是有效的 JSON

关键要求：
- 必须返回有效的 JSON 格式
- 每个讲话人的文本应该自然、有个性、信息丰富
- 内容应该深入、详细、有价值，充分满足用户指定的时长要求
"""
    
    def _build_user_prompt(
        self,
        topic: str,
        num_speakers: int,
        duration_minutes: int,
        language: str,
        speaker_names: Optional[List[str]],
        additional_context: Optional[str] = None,
        custom_instructions: Optional[str] = None
    ) -> str:
        """构建用户提示词"""
        
        duration_seconds = duration_minutes * 60
        
        # 根据语言确定内容量单位和估算值
        if language.startswith("zh") or language.startswith("cmn"):
            # 中文：使用字符数，约3.5字符/秒
            content_estimate = duration_seconds * 3.5
            content_unit = "字符"
            content_unit_short = "字"
        elif language.startswith("ja"):
            # 日文：使用字符数，约4.0字符/秒
            content_estimate = duration_seconds * 4.0
            content_unit = "文字"
            content_unit_short = "文字"
        elif language.startswith("ko"):
            # 韩文：使用字符数，约4.5字符/秒
            content_estimate = duration_seconds * 4.5
            content_unit = "글자"
            content_unit_short = "글자"
        else:
            # 英文及其他：使用词数，约2.5词/秒
            content_estimate = duration_seconds * 2.5
            content_unit = "words"
            content_unit_short = "词"
        
        words_estimate = content_estimate  # 保持变量名兼容
        
        # Only include an explicit speaker list if concrete names were
        # provided by the caller. Otherwise omit it so the LLM can invent
        # realistic human names based on role/gender.
        speaker_list = ""
        if speaker_names:
            speaker_list = "\n".join(
                f"  {i+1}. {name}"
                for i, name in enumerate(speaker_names)
            )
        
        # 对于多人对话，添加强制要求
        multi_speaker_instruction = ""
        if num_speakers > 1:
            multi_speaker_instruction = f"""
【重要：多人对话要求】
- 必须让所有 {num_speakers} 位讲话人都充分参与对话
- 讲话人应该轮流发言，形成自然的对话流
- 每位讲话人应该有 {max(2, num_speakers * 2)} 次以上的发言机会
- 不同讲话人的观点应该有差异和互动
- 避免长篇独白，促进讨论和互动
"""
        
        # Example speakers used inside the prompt when helpful. If
        # speaker_names is provided, use those; otherwise provide generic
        # placeholders in the examples but do not force the model to use
        # them as the canonical list.
        example_speakers = []
        for i in range(min(num_speakers, 4)):
            if speaker_names and i < len(speaker_names):
                example_speakers.append(speaker_names[i])
            else:
                example_speakers.append(f"Speaker {i+1}")
        
        # Build the speaker list block only when available
        speaker_list_block = f"\n{speaker_list}\n" if speaker_list else ""

        # 计算更激进的最小字数要求和段落数
        min_words = int(words_estimate)  # 直接使用目标字数，不打折扣
        min_segments = max(15, int(duration_minutes * 3))  # 至少每20秒一个段落
        words_per_segment = int(min_words / min_segments)
        
        # 检测是否提供了源内容（真实新闻、数据等）
        has_source_content = additional_context and "【重要：基于以下真实内容生成播客】" in additional_context
        
        # 根据是否有源内容，调整prompt要求
        if has_source_content:
            content_requirement = f"""
【🔴 严格要求 - 必须遵守】:
1. **仅使用提供的源内容**: 你必须严格基于"额外背景"中提供的真实内容生成播客
2. **禁止编造事实**: 不要添加任何源内容中没有提到的数据、事件、人名或引用
3. **准确引用**: 如果提到具体数字、日期、公司名、人名，必须与源内容完全一致
4. **可以做的**:
   - 用自己的话重新表述源内容中的信息
   - 解释和分析源内容中的数据和事件
   - 讨论源内容中提到的事件的影响和意义
   - 在源内容的事实基础上进行合理推理
5. **不可以做的**:
   - 编造源内容中不存在的统计数据
   - 提及源内容中未出现的公司、项目或人物
   - 添加源内容中没有的"专家观点"或"最新消息"
   - 夸大或扭曲源内容中的信息

如果源内容信息不足以填满 {duration_minutes} 分钟，你应该:
- 深入分析已有信息的含义和影响
- 讨论事件的背景和context
- 探讨可能的后续影响和趋势
- 但仍然不要编造新的事实
"""
        else:
            content_requirement = f"""
【关键要求 - 必须严格遵守】:

1. **长度要求（最重要）**:
   - 整个脚本必须包含至少 {min_segments} 个对话段落
   - 总内容量必须达到或超过 {min_words} {content_unit}
   - 每个段落应该包含 {words_per_segment}-{words_per_segment + 50} {content_unit_short}
   - 如果你发现内容不够长，必须增加更多讨论、举例、细节和互动

2. **内容深度要求**:
   - 对主题的每个方面都要深入讨论
   - 包含具体的例子、数据、故事或案例
   - 让讲话人之间有充分的互动和来回对话
   - 不要匆忙结束话题，要充分展开

3. **结构要求**:
   - Opening (开场): 2-3个段落，介绍主题和讲话人
   - Main (主体): 至少 {min_segments - 6} 个段落，深入讨论多个子话题
   - Closing (结尾): 2-3个段落，总结要点
"""
        
        prompt = f"""请为以下播客生成一个完整的、高质量的脚本。

【播客信息】
- 主题: {topic}
- 讲话人数: {num_speakers}
{speaker_list_block}- 目标时长: {duration_minutes} 分钟（{duration_seconds} 秒）
- 必须达到的总内容量: 至少 {min_words} {content_unit}
- 必须包含的段落数: 至少 {min_segments} 个段落
- 语言: {language}

【额外背景】
{additional_context or "无"}

【自定义要求】
{custom_instructions or "遵循默认风格"}
{multi_speaker_instruction}

{content_requirement}

【输出格式要求】
请返回以下 JSON 结构，不要包含任何代码块标记或其他文本，直接返回 JSON：

{{
    "title": "播客标题",
    "description": "播客简述（一句话概括）",
    "segments": [
        {{
            "speaker_id": "speaker_1",
            "speaker_name": "{example_speakers[0]}",
            "segment_type": "opening",
            "text": "开场白，应该热情欢迎听众并介绍今天的主题...(约{words_per_segment}{content_unit_short})",
            "notes": "语气热情、节奏适中"
        }},
        {{
            "speaker_id": "speaker_2",
            "speaker_name": "{example_speakers[1] if len(example_speakers) > 1 else example_speakers[0]}",
            "segment_type": "opening",
            "text": "第二位讲话人的自我介绍和对主题的初步看法...(约{words_per_segment}{content_unit_short})"
        }},
        // ... 继续添加更多段落，确保达到{min_segments}个段落
        {{
            "speaker_id": "speaker_1",
            "speaker_name": "{example_speakers[0]}",
            "segment_type": "main",
            "text": "深入讨论第一个子话题，包含具体例子和细节...(约{words_per_segment}{content_unit_short})"
        }},
        // ... 主体部分要有大量的back-and-forth对话
        {{
            "speaker_id": "speaker_1",
            "speaker_name": "{example_speakers[0]}",
            "segment_type": "closing",
            "text": "总结今天讨论的要点，感谢听众收听...(约{words_per_segment}{content_unit_short})"
        }}
    ]
}}

【质量检查清单】:
- ✓ 是否有至少 {min_segments} 个段落？
- ✓ 总内容量是否达到 {min_words} {content_unit}？
- ✓ 每个讲话人是否都充分参与？
- ✓ 内容是否深入、有价值、信息丰富？
- ✓ 是否包含具体的例子和细节？

请确保生成的内容完整、有深度，不要为了速度而牺牲质量和长度。直接返回JSON，不要加任何其他文本。
"""
        
        return prompt
    
    def _generate_speaker_names(
        self,
        num_speakers: int,
        language: str
    ) -> List[str]:
        """生成讲话人名字"""
        
        if language.startswith("zh"):
            # 中文名字
            names_pools = {
                1: ["主持人"],
                2: ["主持人 A", "主持人 B"],
                3: ["主持人", "嘉宾 A", "嘉宾 B"],
                4: ["主持人", "嘉宾 A", "嘉宾 B", "嘉宾 C"],
            }
        elif language.startswith("ko"):
            # 韩文名字
            names_pools = {
                1: ["호스트"],
                2: ["호스트 A", "호스트 B"],
                3: ["호스트", "게스트 A", "게스트 B"],
            }
        else:
            # 英文名字
            names_pools = {
                1: ["Host"],
                2: ["Host A", "Host B"],
                3: ["Host", "Guest A", "Guest B"],
                4: ["Host", "Guest A", "Guest B", "Guest C"],
            }
        
        return names_pools.get(num_speakers, names_pools[2])
    
    def _parse_script_response(
        self,
        script_data: Dict,
        topic: str,
        tone: PodcastTone,
        dialogue_style: DialogueStyle,
        language: str,
        num_speakers: int,
        template_speaker_ids: Optional[List[str]] = None
    ) -> PodcastScript:
        """解析 LLM 返回的脚本数据"""
        
        segments = []
        total_duration = 0.0
        
        # 建立 speaker_N 到 template_speaker_id 的映射
        # 例如: speaker_1 -> host_male, speaker_2 -> host_female, ...
        speaker_id_map = {}
        if template_speaker_ids:
            for i in range(min(num_speakers, len(template_speaker_ids))):
                speaker_id_map[f"speaker_{i+1}"] = template_speaker_ids[i]
        
        for seg_data in script_data.get("segments", []):
            # 估算时长 - 根据语言使用不同的计算方式
            text = seg_data["text"]
            duration = self._estimate_duration(text, language)
            total_duration += duration
            
            # 转换为 SSML 格式
            ssml_text = self._text_to_ssml(
                seg_data["text"],
                language
            )
            
            # 使用 template speaker ID 映射
            original_speaker_id = seg_data.get("speaker_id", f"speaker_{len(segments)}")
            final_speaker_id = speaker_id_map.get(original_speaker_id, original_speaker_id)
            
            segment = ScriptSegment(
                speaker_id=final_speaker_id,  # ✅ 使用映射后的 template speaker ID
                speaker_name=seg_data.get("speaker_name", "Unknown"),
                text=seg_data["text"],
                ssml_text=ssml_text,
                duration_seconds=duration,
                segment_type=seg_data.get("segment_type", "main"),
                notes=seg_data.get("notes")
            )
            
            segments.append(segment)
        
        script = PodcastScript(
            topic=topic,
            title=script_data.get("title", f"Podcast: {topic[:50]}"),
            description=script_data.get("description", topic),
            language=language,
            tone=tone,
            dialogue_style=dialogue_style,
            num_speakers=num_speakers,
            estimated_duration_seconds=total_duration,
            segments=segments,
            metadata={
                "model": self.model,
                "generated_at": __import__('datetime').datetime.now().isoformat()
            }
        )
        
        return script
    
    def _expand_script(
        self,
        current_script: PodcastScript,
        target_duration: float,
        language: str,
        tone: PodcastTone,
        dialogue_style: DialogueStyle,
        template_speaker_ids: Optional[List[str]] = None
    ) -> PodcastScript:
        """
        扩展现有脚本以达到目标时长
        
        Args:
            current_script: 当前的脚本对象
            target_duration: 目标时长（秒）
            language: 语言代码
            tone: 语调
            dialogue_style: 对话风格
            template_speaker_ids: 模板speaker ID列表（用于映射）
            
        Returns:
            扩展后的脚本对象
        """
        
        # 计算需要增加的时长
        current_duration = current_script.estimated_duration_seconds
        needed_duration = target_duration - current_duration
        
        # 根据语言确定内容量单位和估算值
        if language.startswith("zh") or language.startswith("cmn"):
            needed_content = int(needed_duration * 3.5)
            content_unit = "字符"
        elif language.startswith("ja"):
            needed_content = int(needed_duration * 4.0)
            content_unit = "文字"
        elif language.startswith("ko"):
            needed_content = int(needed_duration * 4.5)
            content_unit = "글자"
        else:
            needed_content = int(needed_duration * 2.5)
            content_unit = "words"
        
        needed_words = needed_content  # 保持变量名兼容
        
        # 构建speaker ID到名字的映射（从现有段落中提取）
        speaker_map = {}
        for seg in current_script.segments:
            if seg.speaker_id not in speaker_map:
                speaker_map[seg.speaker_id] = seg.speaker_name
        
        # 构建扩展提示词中的讲话人列表
        speaker_list_for_prompt = "\n".join([
            f"  - speaker_id: \"{sid}\", speaker_name: \"{sname}\""
            for sid, sname in speaker_map.items()
        ])
        
        # 构建扩展提示词
        current_segments_summary = []
        for i, seg in enumerate(current_script.segments[-5:]):  # 只取最后5个段落作为上下文
            current_segments_summary.append(f"{seg.speaker_name} ({seg.speaker_id}): {seg.text[:100]}...")
        
        context_summary = "\n".join(current_segments_summary)
        
        expansion_prompt = f"""当前播客脚本长度不足，需要继续扩展内容。

【当前状态】
- 主题: {current_script.topic}
- 当前时长: {current_duration:.1f} 秒
- 目标时长: {target_duration:.1f} 秒
- 需要增加: 约 {needed_words} {content_unit}

【讲话人信息（必须严格使用这些ID和名字）】
{speaker_list_for_prompt}

【最近的对话内容】
{context_summary}

【扩展要求】
请继续这个播客的讨论，生成更多段落来达到目标时长：

1. **继续当前话题**: 在现有讨论的基础上继续深入
2. **新的子话题**: 可以引入相关的新角度或子话题
3. **必须使用上述精确的speaker_id**: 例如使用 "{list(speaker_map.keys())[0]}" 而不是 "speaker_1" 或其他变体
4. **必须使用上述精确的speaker_name**: 保持名字完全一致
5. **自然衔接**: 内容要与前面的对话自然衔接
6. **生成至少 {needed_words} {content_unit}**: 确保达到所需的长度

请返回扩展的段落数组，格式与之前相同的JSON结构：

{{
    "segments": [
        {{
            "speaker_id": "{list(speaker_map.keys())[0]}",
            "speaker_name": "{list(speaker_map.values())[0]}",
            "segment_type": "main",
            "text": "继续讨论的内容...",
            "notes": "可选的导演笔记"
        }},
        ...更多段落以达到{needed_words}词...
    ]
}}

直接返回JSON，不要添加任何其他文本。
"""
        
        try:
            response = self._chat_completions_create_by_model(
                messages=[
                    {"role": "system", "content": f"你是播客脚本编剧，擅长扩展和丰富内容。语言：{language}"},
                    {"role": "user", "content": expansion_prompt},
                ],
                max_output_tokens=8000,
                temperature=0.7,
                top_p=0.95,
            )
            
            expansion_json = response.choices[0].message.content
            expansion_data = json.loads(expansion_json)
            
            # 提取token使用统计
            usage_dict = None
            if hasattr(response, 'usage') and response.usage:
                usage_obj = response.usage
                usage_dict = {
                    'prompt_tokens': usage_obj.prompt_tokens,
                    'completion_tokens': usage_obj.completion_tokens,
                    'total_tokens': usage_obj.total_tokens
                }
            
            # 解析新段落并添加到现有脚本
            for seg_data in expansion_data.get("segments", []):
                text = seg_data["text"]
                duration = self._estimate_duration(text, language)
                
                ssml_text = self._text_to_ssml(text, language)
                
                segment = ScriptSegment(
                    speaker_id=seg_data.get("speaker_id", "speaker_1"),
                    speaker_name=seg_data.get("speaker_name", "Unknown"),
                    text=seg_data["text"],
                    ssml_text=ssml_text,
                    duration_seconds=duration,
                    segment_type=seg_data.get("segment_type", "main"),
                    notes=seg_data.get("notes")
                )
                
                current_script.segments.append(segment)
                current_script.estimated_duration_seconds += duration
            
            # 更新usage信息
            if usage_dict:
                current_script.metadata['usage'] = usage_dict
            
            return current_script
            
        except Exception as e:
            logger.error(f"❌ 脚本扩展失败: {e}")
            # 扩展失败时返回原脚本
            return current_script
    
    def _estimate_duration(self, text: str, language: str) -> float:
        """
        根据语言智能估算文本的播报时长
        
        中文/日文/韩文: 按字符数计算（因为每个字符基本都是一个完整的语义单位）
        英文等: 按单词数计算
        
        Args:
            text: 要估算的文本
            language: 语言代码
            
        Returns:
            估算的秒数
        """
        # 中文系语言（包括简体中文、繁体中文）
        if language.startswith("zh") or language.startswith("cmn"):
            # 中文平均语速约 4-5 字/秒
            # 播客通常较慢，使用 3.5 字/秒
            char_count = len([c for c in text if '\u4e00' <= c <= '\u9fff'])  # 只计算汉字
            return char_count / 3.5
        
        # 日文
        elif language.startswith("ja"):
            # 日文语速类似中文，约 4 字/秒
            # 包含平假名、片假名、汉字
            char_count = len([c for c in text if (
                ('\u3040' <= c <= '\u309f') or  # 平假名
                ('\u30a0' <= c <= '\u30ff') or  # 片假名
                ('\u4e00' <= c <= '\u9fff')     # 汉字
            )])
            return char_count / 4.0
        
        # 韩文
        elif language.startswith("ko"):
            # 韩文语速约 4-5 字/秒
            char_count = len([c for c in text if '\uac00' <= c <= '\ud7af'])  # 韩文字符
            return char_count / 4.5
        
        # 英文等西方语言（默认）
        else:
            # 英文平均语速约 150-160 词/分钟
            # 即 2.5-2.7 词/秒，播客通常较慢，使用 2.5 词/秒
            word_count = len(text.split())
            return word_count / 2.5
    
    def _text_to_ssml(self, text: str, language: str) -> str:
        """将文本转换为 SSML 格式"""
        
        # 基础 SSML 包装
        ssml = f'<speak>{text}</speak>'
        
        # 添加语言和语音属性
        if language.startswith("zh"):
            # 中文：添加断句和自然停顿
            ssml = ssml.replace("。", '<break time="500ms"/>')
            ssml = ssml.replace("，", '<break time="200ms"/>')
        elif language.startswith("en"):
            # 英文：添加重音和停顿
            ssml = ssml.replace("!", '<emphasis level="strong">!</emphasis><break time="300ms"/>')
            ssml = ssml.replace("?", '<break time="300ms"/>')
        
        return ssml
    
    def save_script(
        self,
        script: PodcastScript,
        output_path: str
    ) -> None:
        """保存脚本为 JSON 文件"""
        
        output_dir = Path(output_path).parent
        output_dir.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(script.to_dict(), f, indent=2, ensure_ascii=False)
        
        logger.info(f"✅ 脚本已保存: {output_path}")


# ============================================================================
# 演示用法
# ============================================================================

def main():
    """演示脚本生成"""
    
    print("\n" + "="*80)
    print("🎙️  LLM 播客脚本生成器演示".center(80))
    print("="*80 + "\n")
    
    # 初始化生成器
    generator = LLMScriptGenerator(model="gpt-4-mini")
    
    # 示例 1: 加州旅游
    print("【示例 1】加州旅游播客")
    print("-" * 80)
    
    script1 = generator.generate_script(
        topic="加州旅游必去的景点和体验，包括旧金山、洛杉矶、圣地亚哥等地的推荐",
        num_speakers=2,
        duration_minutes=5,
        language="zh-CN",
        tone=PodcastTone.ENTERTAINING,
        dialogue_style=DialogueStyle.CONVERSATION,
        speaker_names=["Amy", "Tom"],
        additional_context="目标听众是计划去加州旅游的年轻人"
    )
    
    print(f"✅ 生成完成!")
    print(f"   标题: {script1.title}")
    print(f"   描述: {script1.description}")
    print(f"   段落: {len(script1.segments)}")
    print(f"   时长: {script1.estimated_duration_seconds:.1f} 秒")
    print()
    
    # 保存脚本
    script1.save_path = "outputs/script_california_tour.json"
    generator.save_script(script1, script1.save_path)
    
    # 示例 2: GPU 选购指南
    print("\n【示例 2】GPU 选购指南播客")
    print("-" * 80)
    
    script2 = generator.generate_script(
        topic="2025年GPU显卡选购指南，对比NVIDIA RTX和AMD的性能和价格，适合游戏和AI应用",
        num_speakers=2,
        duration_minutes=5,
        language="zh-CN",
        tone=PodcastTone.EDUCATIONAL,
        dialogue_style=DialogueStyle.INTERVIEW,
        speaker_names=["主持人小李", "硬件专家王博士"],
        additional_context="目标听众是想要升级GPU的开发者和游戏玩家"
    )
    
    print(f"✅ 生成完成!")
    print(f"   标题: {script2.title}")
    print(f"   段落: {len(script2.segments)}")
    print(f"   时长: {script2.estimated_duration_seconds:.1f} 秒")
    
    generator.save_script(script2, "outputs/script_gpu_guide.json")
    
    print("\n✅ 所有脚本生成完成!")


if __name__ == "__main__":
    main()
