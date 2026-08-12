"""AI Analyzer V1 — advisory-only AI layer on top of an already-built Trade Setup.

Architecture: `AIAnalyzer` -> `AIProvider` (ai/provider.py) -> `GeminiProvider`
(ai/gemini_provider.py) today; adding OpenAI/Claude later is another
AIProvider subclass, nothing here or in any caller changes.

Hard boundaries (see project instructions):
- The AI never invents a new entry/stop/TP/leverage/position size — it only
  scores the setup it is given (see build_snapshot: read-only fields).
- The AI's output is NEVER read by RiskManager or PaperTradingEngine — this
  module is only ever consumed by app/dashboard_data.py for the "Мозг ИИ"
  display. A RiskManager rejection can never be overridden by an AI
  CONFIRM, because RiskManager never sees the AI result at all.
- Disabled unless AI_ENABLED=true AND a GEMINI_API_KEY is configured; any
  provider failure (timeout/HTTP error/rate limit/bad JSON) degrades to
  AI_STATUS=ERROR instead of raising or opening a trade.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ai.gemini_provider import GeminiProvider
from ai.provider import AIProvider, AIProviderError
from config.env import get_env

AI_STATUS_DISABLED = 'DISABLED'
AI_STATUS_CONNECTED = 'CONNECTED'
AI_STATUS_ERROR = 'ERROR'

VALID_DIRECTIONS = {'LONG', 'SHORT', 'NEUTRAL'}
VALID_DECISIONS = {'CONFIRM', 'REJECT', 'WAIT'}


@dataclass(frozen=True)
class AIAnalysisResult:
    status: str
    provider: str
    direction: str = 'NEUTRAL'
    ai_score: int = 0
    decision: str = 'WAIT'
    reasons: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)


class AIAnalyzer:
    def __init__(
        self,
        provider: AIProvider | None = None,
        enabled: bool | None = None,
        provider_name: str = 'gemini',
    ) -> None:
        """`provider`/`enabled` overrides are for tests (inject a mock provider,
        force enabled True/False) — production code should call `AIAnalyzer()`
        with no arguments and let it read GEMINI_API_KEY/AI_ENABLED from .env."""
        self.provider_name = provider_name
        self._enabled_override = enabled
        self._provider = provider if provider is not None else self._build_default_provider()

    @staticmethod
    def _build_default_provider() -> AIProvider | None:
        api_key = get_env('GEMINI_API_KEY')
        if not api_key:
            return None
        return GeminiProvider(api_key=api_key)

    @property
    def is_enabled(self) -> bool:
        if self._provider is None:
            return False
        if self._enabled_override is not None:
            return self._enabled_override
        return (get_env('AI_ENABLED', 'false') or '').strip().lower() == 'true'

    @staticmethod
    def build_snapshot(symbol: str, analysis: dict[str, Any], setup: dict[str, Any]) -> dict[str, Any]:
        """Compact, JSON-serializable snapshot only — never raw candle arrays."""
        timeframes = analysis.get('analysis') or {}
        indicators_15m = (timeframes.get('15m') or {}).get('indicators') or {}
        return {
            'symbol': symbol,
            'current_price': analysis.get('last_price'),
            'trend_4h': (timeframes.get('4h') or {}).get('trend'),
            'trend_1h': (timeframes.get('1h') or {}).get('trend'),
            'trend_15m': (timeframes.get('15m') or {}).get('trend'),
            'market_regime': analysis.get('market_regime'),
            'technical_score': analysis.get('technical_score'),
            'setup_type': setup.get('setup_type'),
            'setup_score': setup.get('setup_score'),
            'entry_zone_low': setup.get('entry_zone_low'),
            'entry_zone_high': setup.get('entry_zone_high'),
            'stop_loss': setup.get('stop_loss'),
            'take_profit_1': setup.get('take_profit_1'),
            'take_profit_2': setup.get('take_profit_2'),
            'risk_reward': setup.get('risk_reward_tp1'),
            'volume_ratio': indicators_15m.get('ratio'),
            'rsi': indicators_15m.get('rsi14'),
            'macd': {
                'macd_line': indicators_15m.get('macd_line'),
                'signal_line': indicators_15m.get('signal_line'),
                'histogram': indicators_15m.get('histogram'),
            },
            'atr': indicators_15m.get('atr14'),
        }

    def analyze(self, symbol: str, analysis: dict[str, Any], setup: dict[str, Any]) -> AIAnalysisResult:
        if not self.is_enabled:
            return AIAnalysisResult(status=AI_STATUS_DISABLED, provider=self.provider_name)

        snapshot = self.build_snapshot(symbol, analysis, setup)
        try:
            raw = self._provider.analyze(snapshot)
        except AIProviderError:
            return AIAnalysisResult(status=AI_STATUS_ERROR, provider=self.provider_name, risk_flags=['AI_PROVIDER_ERROR'])
        except Exception:
            # Any unexpected provider bug must degrade, never crash the app or
            # be mistaken for permission to trade.
            return AIAnalysisResult(status=AI_STATUS_ERROR, provider=self.provider_name, risk_flags=['AI_PROVIDER_ERROR'])

        return self._to_result(raw)

    def _to_result(self, raw: dict[str, Any]) -> AIAnalysisResult:
        direction = raw.get('direction')
        if direction not in VALID_DIRECTIONS:
            direction = 'NEUTRAL'

        decision = raw.get('decision')
        if decision not in VALID_DECISIONS:
            decision = 'WAIT'

        try:
            ai_score = int(raw.get('ai_score', 0))
        except (TypeError, ValueError):
            ai_score = 0
        ai_score = min(max(ai_score, 0), 100)

        reasons = raw.get('reasons') or []
        if not isinstance(reasons, list):
            reasons = [reasons]
        risk_flags = raw.get('risk_flags') or []
        if not isinstance(risk_flags, list):
            risk_flags = [risk_flags]

        return AIAnalysisResult(
            status=AI_STATUS_CONNECTED,
            provider=self.provider_name,
            direction=direction,
            ai_score=ai_score,
            decision=decision,
            reasons=[str(r) for r in reasons],
            risk_flags=[str(r) for r in risk_flags],
        )
