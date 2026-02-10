"""
Podcast 时长控制模块
提供精确的字数计算、TTS 语速调整和音频截断功能
"""

import re
import logging
from typing import Optional, Dict, Any, Tuple
from pydub import AudioSegment

logger = logging.getLogger(__name__)

# TTS 语速配置（字/分钟）- 基于实际测试数据
SPEECH_RATE = {
    "cmn-CN": 220,  # 中文普通话
    "cmn-TW": 220,  # 中文台湾
    "ja-JP": 200,   # 日文
    "ko-KR": 210,   # 韩文
    "en-US": 140,   # 英文美国
    "en-GB": 140,   # 英文英国
}

#
# Google Cloud TTS `speaking_rate` 语义（重要）：
# - speaking_rate 是倍率参数：1.0 = 正常语速；> 1.0 更快；< 1.0 更慢
# - 有效范围通常在 0.25 到 4.0（不同 SDK/版本可能略有差异）
#
# 之前的实现把 speaking_rate 当成了 0.0=正常、0.6=更快 的“偏移量”，
# 会导致语速整体偏慢甚至接近最慢值，从而出现“语速很慢”的体验问题。


def count_words(text: str, language: str) -> int:
    """
    根据语言计算字数
    
    Args:
        text: 要计算的文本
        language: 语言代码 (如 "cmn-CN", "en-US")
    
    Returns:
        字数（中日韩计算字符，英文计算单词）
    """
    if not text:
        return 0
    
    # 中日韩语言：计算字符数（去除标点和空格）
    if language.startswith(("cmn", "ja", "ko")):
        # 去除标点、空格、数字、英文字母、常见符号
        import unicodedata
        cleaned = []
        for char in text:
            # 跳过空格、数字、ASCII字母
            if char.isspace() or char.isdigit() or char.isascii():
                continue
            # 检查是否是标点符号
            category = unicodedata.category(char)
            if category.startswith('P'):  # Punctuation
                continue
            cleaned.append(char)
        return len(cleaned)
    else:
        # 英文及其他：计算单词数
        words = text.split()
        return len(words)


def calculate_max_words(
    duration_seconds: float, 
    language: str, 
    safety_margin: float = 0.85
) -> int:
    """
    根据目标时长计算最大字数
    
    Args:
        duration_seconds: 目标时长（秒）
        language: 语言代码
        safety_margin: 安全余量（默认 85%，防止语速过快不自然）
    
    Returns:
        最大字数限制
    """
    rate = SPEECH_RATE.get(language, 140)
    return int((duration_seconds / 60) * rate * safety_margin)


def calculate_optimal_tts_params(
    text: str, 
    target_duration: float, 
    language: str
) -> Dict[str, Any]:
    """
    计算最优 TTS 参数
    
    Args:
        text: 脚本文本
        target_duration: 目标时长（秒）
        language: 语言代码
    
    Returns:
        {
            "speaking_rate": float,  # Google Cloud TTS 参数 (multiplier, 1.0 = normal)
            "speed_ratio": float,    # 期望语速倍数（仅用于估算/日志）
            "estimated_duration": float,  # 预估时长（秒）
            "word_count": int,       # 字数
            "max_words": int,        # 建议最大字数
        }
    """
    word_count = count_words(text, language)
    rate = SPEECH_RATE.get(language, 140)
    max_words = calculate_max_words(target_duration, language)
    
    # 预估基础时长（正常语速）
    base_duration = (word_count / rate) * 60
    
    # 计算“如果要在目标时长内说完”理论上需要的速度倍率（仅用于日志/估算）。
    # 注意：产品策略可能选择“固定 speaking_rate=1.0（原语速）”，此时需要通过压缩脚本而不是调速来满足时长。
    if base_duration > target_duration and target_duration > 0:
        speed_ratio = base_duration / target_duration
    else:
        speed_ratio = 1.0

    # Product requirement: keep Google TTS at its natural/default speed.
    speaking_rate = 1.0
    
    # 重新计算预估时长
    estimated_duration = base_duration / speaking_rate if speaking_rate > 0 else base_duration
    
    return {
        "speaking_rate": round(speaking_rate, 2),
        "speed_ratio": round(speed_ratio, 2),
        "estimated_duration": round(estimated_duration, 1),
        "word_count": word_count,
        "max_words": max_words,
        "is_over_limit": word_count > max_words,
    }


def truncate_audio(
    input_path: str,
    output_path: str,
    target_duration: float,
    fade_out: float = 1.5
) -> str:
    """
    截断音频到目标时长，添加淡出效果
    
    Args:
        input_path: 输入音频路径
        output_path: 输出音频路径
        target_duration: 目标时长（秒）
        fade_out: 淡出时长（秒）
    
    Returns:
        输出音频路径
    """
    try:
        audio = AudioSegment.from_file(input_path)
        target_ms = int(target_duration * 1000)
        fade_ms = int(fade_out * 1000)
        
        if len(audio) <= target_ms:
            logger.info(f"音频时长 {len(audio)/1000:.1f}s 已在目标范围内")
            return input_path
        
        logger.warning(f"音频时长 {len(audio)/1000:.1f}s 超过目标 {target_duration}s，进行截断")
        
        # 截断并淡出
        truncated = audio[:target_ms]
        if fade_ms > 0 and len(truncated) > fade_ms:
            truncated = truncated.fade_out(fade_ms)
        
        truncated.export(output_path, format="mp3")
        logger.info(f"截断后音频已保存: {output_path} ({target_duration}s)")
        return output_path
    
    except Exception as e:
        logger.error(f"音频截断失败: {e}")
        return input_path


def compress_script_prompt(script: str, max_words: int, language: str) -> str:
    """
    生成脚本压缩的 prompt
    
    Args:
        script: 原始脚本
        max_words: 最大字数限制
        language: 语言代码
    
    Returns:
        用于 LLM 压缩的 prompt
    """
    current_words = count_words(script, language)
    
    if language.startswith("cmn"):
        return f"""请将以下播客脚本压缩到 {max_words} 字以内（当前 {current_words} 字）。

要求：
1. 删除所有寒暄、过渡句（如"让我们"、"接下来"、"首先"）
2. 合并重复内容
3. 只保留最核心的：结论 + 1个最重要原因 + 风险提示
4. 保持对话格式（主持人A/B）
5. 直接输出压缩后的脚本，不要解释

原文：
{script}"""
    elif language.startswith("ja"):
        return f"""以下のポッドキャスト台本を{max_words}文字以内に圧縮してください（現在{current_words}文字）。

要件：
1. 挨拶や接続詞を削除
2. 結論+最重要ポイント1つ+リスクのみを保持
3. 対話形式を維持
4. 圧縮後の台本のみを出力

原文：
{script}"""
    elif language.startswith("ko"):
        return f"""다음 팟캐스트 스크립트를 {max_words}단어 이내로 압축하세요 (현재 {current_words}단어).

요구사항:
1. 인사말과 접속사 삭제
2. 결론+가장 중요한 포인트 1개+리스크만 유지
3. 대화 형식 유지
4. 압축된 스크립트만 출력

원문:
{script}"""
    else:
        return f"""Please compress the following podcast script to under {max_words} words (currently {current_words} words).

Requirements:
1. Remove all greetings and transition phrases
2. Keep only: conclusion + top 1 point + risk warning
3. Maintain dialogue format
4. Output only the compressed script

Original:
{script}"""


def add_duration_constraints_to_prompt(
    base_prompt: str,
    max_words: int,
    target_duration: float,
    language: str
) -> str:
    """
    向 prompt 添加时长约束
    
    Args:
        base_prompt: 原始 prompt
        max_words: 最大字数
        target_duration: 目标时长（秒）
        language: 语言代码
    
    Returns:
        添加约束后的 prompt
    """
    if language.startswith("cmn"):
        constraint = f"""

【硬性约束 - 必须遵守】
1. 总字数必须控制在 {max_words} 字以内
2. 预估时长约 {target_duration} 秒
3. 禁止：寒暄、过渡句（"让我们"、"接下来"、"首先"）
4. 必须：开头直接给出结论（BUY/SELL/HOLD + 置信度）
5. 结构：结论(10字) → 核心原因(30字) → 风险提示(10字)
"""
    elif language.startswith("ja"):
        constraint = f"""

【必須制約】
1. 総文字数は{max_words}文字以内
2. 推定時間は約{target_duration}秒
3. 挨拶や接続詞を禁止
4. 冒頭で結論を提示（BUY/SELL/HOLD + 信頼度）
"""
    elif language.startswith("ko"):
        constraint = f"""

【필수 제약】
1. 총 단어 수 {max_words}단어 이내
2. 예상 시간 약 {target_duration}초
3. 인사말과 접속사 금지
4. 시작에서 결론 제시 (BUY/SELL/HOLD + 신뢰도)
"""
    else:
        constraint = f"""

[CRITICAL CONSTRAINTS]
1. Total length MUST be under {max_words} words
2. Estimated duration: ~{target_duration} seconds
3. NO greetings, NO transition phrases
4. MUST start with conclusion (BUY/SELL/HOLD + confidence)
"""
    
    return base_prompt + constraint


# ==================== 主控函数 ====================

async def enforce_duration_limit(
    script_text: str,
    target_duration: float,
    language: str,
    llm_compress_func: Optional[callable] = None
) -> Tuple[str, Dict[str, Any]]:
    """
    强制执行时长限制
    
    Args:
        script_text: 原始脚本
        target_duration: 目标时长（秒）
        language: 语言代码
        llm_compress_func: 可选的 LLM 压缩函数
    
    Returns:
        (处理后的脚本, 元数据字典)
    """
    word_count = count_words(script_text, language)
    max_words = calculate_max_words(target_duration, language)
    
    metadata = {
        "original_word_count": word_count,
        "max_words": max_words,
        "target_duration": target_duration,
        "was_compressed": False,
    }
    
    # 如果字数在限制内，直接返回
    if word_count <= max_words:
        logger.info(f"脚本字数 {word_count} 在限制 {max_words} 内")
        return script_text, metadata
    
    logger.warning(f"脚本字数 {word_count} 超过限制 {max_words}，需要压缩")
    
    # 如果有 LLM 压缩函数，使用它
    if llm_compress_func:
        try:
            compressed_prompt = compress_script_prompt(script_text, max_words, language)
            compressed_script = await llm_compress_func(compressed_prompt)
            
            new_word_count = count_words(compressed_script, language)
            metadata["was_compressed"] = True
            metadata["compressed_word_count"] = new_word_count
            
            logger.info(f"脚本已压缩: {word_count} -> {new_word_count} 字")
            return compressed_script, metadata
        except Exception as e:
            logger.error(f"LLM 压缩失败: {e}")
    
    # 如果没有压缩函数或压缩失败，返回原脚本（依赖 TTS 语速调整）
    metadata["compression_failed"] = True
    return script_text, metadata
