"""AI provider abstraction (AI Analyzer V1).

`AIProvider` is the seam between `AIAnalyzer` (ai/analyzer.py) and a specific
AI backend. `GeminiProvider` (ai/gemini_provider.py) is the first
implementation; adding OpenAI/Claude later means writing another subclass,
not touching AIAnalyzer or anything that consumes it.

A provider's `analyze()` either returns a raw parsed dict with the five
required keys (direction/ai_score/decision/reasons/risk_flags) or raises one
of the `AIProviderError` subclasses below — it never returns partial/invalid
data silently. AIAnalyzer treats ANY of these exceptions the same way:
AI_STATUS=ERROR, no crash, no trade opened on the strength of the error.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class AIProviderError(Exception):
    """Base class for all provider failures (network, timeout, bad response)."""


class AIProviderTimeout(AIProviderError):
    pass


class AIProviderAPIError(AIProviderError):
    pass


class AIProviderRateLimited(AIProviderError):
    pass


class AIProviderInvalidResponse(AIProviderError):
    pass


class AIProvider(ABC):
    @abstractmethod
    def analyze(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Returns a dict with direction/ai_score/decision/reasons/risk_flags.

        Must raise an AIProviderError subclass on any failure (timeout, HTTP
        error, rate limit, unparsable response) instead of returning
        partial/guessed data."""
        raise NotImplementedError
