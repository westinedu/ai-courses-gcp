"""
Cost Calculator Module

Computes billing estimates for LLM tokens and Google Text-to-Speech (TTS) usage.
Supports configurable pricing rates and provides detailed cost breakdowns.
"""

from dataclasses import dataclass
from typing import Optional, Dict, Any


@dataclass
class TokenPricing:
    """LLM token pricing (per 1,000 tokens in USD)."""
    prompt_tokens_per_1k: float = 0.0005  # Example: Claude 3 Haiku
    completion_tokens_per_1k: float = 0.0015  # Higher cost for completions


@dataclass
class TTSPricing:
    """Google Text-to-Speech pricing (per 1,000,000 characters in USD)."""
    standard_per_1m_chars: float = 4.0  # Standard WaveNet voices
    neural_per_1m_chars: float = 16.0  # Neural voices (higher quality)
    wavenet_per_1m_chars: float = 16.0  # WaveNet voices


@dataclass
class UsageMetrics:
    """Captured usage metrics from a generation run."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    tts_characters: int = 0
    tts_duration_seconds: Optional[float] = None
    audio_file_size_bytes: Optional[int] = None


@dataclass
class CostBreakdown:
    """Detailed cost breakdown for billing."""
    prompt_cost_usd: float = 0.0
    completion_cost_usd: float = 0.0
    llm_total_cost_usd: float = 0.0
    tts_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    
    # Additional metrics for display
    metrics: Optional[UsageMetrics] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "prompt_cost_usd": round(self.prompt_cost_usd, 6),
            "completion_cost_usd": round(self.completion_cost_usd, 6),
            "llm_total_cost_usd": round(self.llm_total_cost_usd, 6),
            "tts_cost_usd": round(self.tts_cost_usd, 6),
            "total_cost_usd": round(self.total_cost_usd, 6),
        }


class CostCalculator:
    """Calculate costs based on usage metrics and pricing."""
    
    def __init__(
        self,
        token_pricing: Optional[TokenPricing] = None,
        tts_pricing: Optional[TTSPricing] = None,
    ):
        self.token_pricing = token_pricing or TokenPricing()
        self.tts_pricing = tts_pricing or TTSPricing()
    
    def calculate_llm_cost(
        self,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float:
        """Calculate LLM token cost in USD."""
        prompt_cost = (prompt_tokens / 1000.0) * self.token_pricing.prompt_tokens_per_1k
        completion_cost = (completion_tokens / 1000.0) * self.token_pricing.completion_tokens_per_1k
        return prompt_cost + completion_cost
    
    def calculate_tts_cost(
        self,
        character_count: int,
        voice_type: str = "standard",
    ) -> float:
        """
        Calculate TTS cost in USD.
        
        Args:
            character_count: Number of characters passed to TTS
            voice_type: Type of voice ('standard', 'neural', 'wavenet')
        """
        rate_per_1m = getattr(self.tts_pricing, f"{voice_type}_per_1m_chars", self.tts_pricing.standard_per_1m_chars)
        return (character_count / 1_000_000.0) * rate_per_1m
    
    def calculate_total_cost(
        self,
        metrics: UsageMetrics,
        voice_type: str = "standard",
    ) -> CostBreakdown:
        """
        Calculate complete cost breakdown.
        
        Args:
            metrics: Captured usage metrics
            voice_type: TTS voice type ('standard', 'neural', 'wavenet')
        
        Returns:
            CostBreakdown with all costs
        """
        llm_cost = self.calculate_llm_cost(metrics.prompt_tokens, metrics.completion_tokens)
        prompt_cost = (metrics.prompt_tokens / 1000.0) * self.token_pricing.prompt_tokens_per_1k
        completion_cost = (metrics.completion_tokens / 1000.0) * self.token_pricing.completion_tokens_per_1k
        
        tts_cost = self.calculate_tts_cost(metrics.tts_characters, voice_type) if metrics.tts_characters > 0 else 0.0
        
        return CostBreakdown(
            prompt_cost_usd=prompt_cost,
            completion_cost_usd=completion_cost,
            llm_total_cost_usd=llm_cost,
            tts_cost_usd=tts_cost,
            total_cost_usd=llm_cost + tts_cost,
            metrics=metrics,
        )
    
    def estimate_tts_characters_from_duration(
        self,
        duration_seconds: float,
        avg_chars_per_second: float = 15.0,
    ) -> int:
        """Estimate character count from audio duration (fallback when exact count unavailable)."""
        return int(duration_seconds * avg_chars_per_second)
