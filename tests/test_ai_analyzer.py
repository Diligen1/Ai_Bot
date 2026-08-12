"""Tests for AI Analyzer V1 (ai/analyzer.py, ai/provider.py, ai/gemini_provider.py).

All tests here use a mock AIProvider — no real network calls to Gemini are
ever made. GeminiProvider's own HTTP layer is exercised separately by
monkeypatching `urllib.request.urlopen`, still without any real request.
"""
from __future__ import annotations

import json
import urllib.error
from types import SimpleNamespace
from typing import Any

import pytest

from ai.analyzer import AIAnalyzer, AIAnalysisResult
from ai.gemini_provider import GeminiProvider
from ai.provider import (
    AIProvider,
    AIProviderError,
    AIProviderInvalidResponse,
    AIProviderRateLimited,
    AIProviderTimeout,
)

SAMPLE_ANALYSIS: dict[str, Any] = {
    'symbol': 'BTCUSDT', 'status': 'LIVE', 'last_price': 50000.0, 'direction': 'LONG',
    'technical_score': 78, 'market_regime': 'TREND_UP', 'timeframe_alignment': 'STRONG_LONG',
    'analysis': {
        '4h': {'trend': 'BULLISH', 'indicators': {'rsi14': 60.0}},
        '1h': {'trend': 'BULLISH', 'indicators': {}},
        '15m': {
            'trend': 'BULLISH',
            'indicators': {
                'ratio': 1.2, 'rsi14': 55.0,
                'macd_line': 1.1, 'signal_line': 0.9, 'histogram': 0.2,
                'atr14': 120.0,
            },
        },
    },
}

SAMPLE_SETUP: dict[str, Any] = {
    'setup_type': 'TREND_PULLBACK', 'setup_score': 82,
    'entry_zone_low': 49500.0, 'entry_zone_high': 50000.0,
    'stop_loss': 48800.0, 'take_profit_1': 51000.0, 'take_profit_2': 52000.0,
    'risk_reward_tp1': 2.5,
}


class MockProvider(AIProvider):
    def __init__(self, response: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls = 0

    def analyze(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


# -- decision paths (mock provider) --------------------------------------

def test_confirm_decision_from_provider() -> None:
    provider = MockProvider(response={
        'direction': 'LONG', 'ai_score': 82, 'decision': 'CONFIRM',
        'reasons': ['strong trend alignment'], 'risk_flags': [],
    })
    analyzer = AIAnalyzer(provider=provider, enabled=True)
    result = analyzer.analyze('BTCUSDT', SAMPLE_ANALYSIS, SAMPLE_SETUP)

    assert result.status == 'CONNECTED'
    assert result.decision == 'CONFIRM'
    assert result.direction == 'LONG'
    assert result.ai_score == 82
    assert result.reasons == ['strong trend alignment']
    assert provider.calls == 1


def test_reject_decision_from_provider() -> None:
    provider = MockProvider(response={
        'direction': 'NEUTRAL', 'ai_score': 20, 'decision': 'REJECT',
        'reasons': ['setup contradicts higher timeframe'], 'risk_flags': ['LOW_CONFIDENCE'],
    })
    analyzer = AIAnalyzer(provider=provider, enabled=True)
    result = analyzer.analyze('BTCUSDT', SAMPLE_ANALYSIS, SAMPLE_SETUP)

    assert result.status == 'CONNECTED'
    assert result.decision == 'REJECT'
    assert result.ai_score == 20
    assert result.risk_flags == ['LOW_CONFIDENCE']


def test_wait_decision_from_provider() -> None:
    provider = MockProvider(response={
        'direction': 'LONG', 'ai_score': 55, 'decision': 'WAIT',
        'reasons': ['needs confirmation on lower timeframe'], 'risk_flags': [],
    })
    analyzer = AIAnalyzer(provider=provider, enabled=True)
    result = analyzer.analyze('BTCUSDT', SAMPLE_ANALYSIS, SAMPLE_SETUP)

    assert result.status == 'CONNECTED'
    assert result.decision == 'WAIT'
    assert result.ai_score == 55


# -- provider failures never crash / never fabricate a trade --------------

def test_timeout_returns_error_status_without_raising() -> None:
    provider = MockProvider(error=AIProviderTimeout('Gemini request timed out'))
    analyzer = AIAnalyzer(provider=provider, enabled=True)
    result = analyzer.analyze('BTCUSDT', SAMPLE_ANALYSIS, SAMPLE_SETUP)

    assert result.status == 'ERROR'
    assert result.decision == 'WAIT'
    assert result.ai_score == 0
    assert 'AI_PROVIDER_ERROR' in result.risk_flags


def test_invalid_json_returns_error_status_without_raising() -> None:
    provider = MockProvider(error=AIProviderInvalidResponse('Gemini did not return valid JSON'))
    analyzer = AIAnalyzer(provider=provider, enabled=True)
    result = analyzer.analyze('BTCUSDT', SAMPLE_ANALYSIS, SAMPLE_SETUP)

    assert result.status == 'ERROR'
    assert result.decision == 'WAIT'


def test_unexpected_provider_exception_also_degrades_to_error() -> None:
    provider = MockProvider(error=RuntimeError('something the provider never should have raised'))
    analyzer = AIAnalyzer(provider=provider, enabled=True)
    result = analyzer.analyze('BTCUSDT', SAMPLE_ANALYSIS, SAMPLE_SETUP)

    assert result.status == 'ERROR'


# -- enable/disable and missing key ---------------------------------------

def test_missing_api_key_forces_disabled_even_if_ai_enabled_true(monkeypatch) -> None:
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    monkeypatch.setenv('AI_ENABLED', 'true')
    analyzer = AIAnalyzer()  # no provider override -> builds from env -> None (no key)

    assert analyzer.is_enabled is False
    result = analyzer.analyze('BTCUSDT', SAMPLE_ANALYSIS, SAMPLE_SETUP)
    assert result.status == 'DISABLED'


def test_ai_disabled_by_default_even_with_provider() -> None:
    provider = MockProvider(response={'direction': 'LONG', 'ai_score': 90, 'decision': 'CONFIRM', 'reasons': [], 'risk_flags': []})
    analyzer = AIAnalyzer(provider=provider, enabled=False)

    result = analyzer.analyze('BTCUSDT', SAMPLE_ANALYSIS, SAMPLE_SETUP)

    assert result.status == 'DISABLED'
    assert provider.calls == 0  # disabled must never call the provider


def test_ai_enabled_env_flag_defaults_to_false(monkeypatch) -> None:
    monkeypatch.delenv('AI_ENABLED', raising=False)
    provider = MockProvider(response={'direction': 'LONG', 'ai_score': 90, 'decision': 'CONFIRM', 'reasons': [], 'risk_flags': []})
    analyzer = AIAnalyzer(provider=provider)  # no explicit enabled override -> reads env

    assert analyzer.is_enabled is False


# -- robustness of raw provider output handling ----------------------------

def test_unknown_direction_falls_back_to_neutral() -> None:
    provider = MockProvider(response={'direction': 'SIDEWAYS', 'ai_score': 50, 'decision': 'WAIT', 'reasons': [], 'risk_flags': []})
    analyzer = AIAnalyzer(provider=provider, enabled=True)
    result = analyzer.analyze('BTCUSDT', SAMPLE_ANALYSIS, SAMPLE_SETUP)
    assert result.direction == 'NEUTRAL'


def test_ai_score_clamped_to_0_100() -> None:
    provider = MockProvider(response={'direction': 'LONG', 'ai_score': 500, 'decision': 'CONFIRM', 'reasons': [], 'risk_flags': []})
    analyzer = AIAnalyzer(provider=provider, enabled=True)
    result = analyzer.analyze('BTCUSDT', SAMPLE_ANALYSIS, SAMPLE_SETUP)
    assert result.ai_score == 100


# -- compact snapshot (requirement: never send raw candle arrays) ---------

def test_snapshot_is_compact_and_never_includes_candle_arrays() -> None:
    snapshot = AIAnalyzer.build_snapshot('BTCUSDT', SAMPLE_ANALYSIS, SAMPLE_SETUP)

    assert 'candles' not in json.dumps(snapshot).lower()
    assert snapshot['symbol'] == 'BTCUSDT'
    assert snapshot['current_price'] == 50000.0
    assert snapshot['trend_4h'] == 'BULLISH'
    assert snapshot['trend_1h'] == 'BULLISH'
    assert snapshot['trend_15m'] == 'BULLISH'
    assert snapshot['setup_type'] == 'TREND_PULLBACK'
    assert snapshot['entry_zone_low'] == 49500.0
    assert snapshot['stop_loss'] == 48800.0
    assert snapshot['rsi'] == 55.0
    # Every value must be a plain scalar or a tiny fixed-shape dict (macd) —
    # never a list, which is where a candle array could sneak in.
    for key, value in snapshot.items():
        assert not isinstance(value, list), f'{key} must not be a list/array'


# -- GeminiProvider: timeout / retry / errors never leak the key ----------

class _FakeHTTPResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def _gemini_success_body(payload: dict[str, Any]) -> bytes:
    return json.dumps({
        'candidates': [{'content': {'parts': [{'text': json.dumps(payload)}]}}],
    }).encode('utf-8')


def test_gemini_provider_retries_once_then_succeeds(monkeypatch) -> None:
    calls = {'count': 0}

    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        calls['count'] += 1
        if calls['count'] == 1:
            raise urllib.error.URLError('temporary DNS failure')
        return _FakeHTTPResponse(_gemini_success_body(
            {'direction': 'LONG', 'ai_score': 70, 'decision': 'CONFIRM', 'reasons': [], 'risk_flags': []}
        ))

    monkeypatch.setattr('ai.gemini_provider.urllib.request.urlopen', fake_urlopen)
    provider = GeminiProvider(api_key='fake-key', max_retries=1)

    result = provider.analyze({'symbol': 'BTCUSDT'})

    assert calls['count'] == 2
    assert result['decision'] == 'CONFIRM'


def test_gemini_provider_gives_up_after_max_retries_no_infinite_loop(monkeypatch) -> None:
    calls = {'count': 0}

    def always_fail(request: Any, timeout: float) -> None:
        calls['count'] += 1
        raise urllib.error.URLError('down')

    monkeypatch.setattr('ai.gemini_provider.urllib.request.urlopen', always_fail)
    provider = GeminiProvider(api_key='fake-key', max_retries=1)

    with pytest.raises(AIProviderError):
        provider.analyze({'symbol': 'BTCUSDT'})

    assert calls['count'] == 2  # first attempt + exactly 1 retry, never unbounded


def test_gemini_provider_invalid_json_does_not_retry(monkeypatch) -> None:
    calls = {'count': 0}

    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        calls['count'] += 1
        return _FakeHTTPResponse(json.dumps({
            'candidates': [{'content': {'parts': [{'text': 'not valid json at all'}]}}],
        }).encode('utf-8'))

    monkeypatch.setattr('ai.gemini_provider.urllib.request.urlopen', fake_urlopen)
    provider = GeminiProvider(api_key='fake-key', max_retries=2)

    with pytest.raises(AIProviderInvalidResponse):
        provider.analyze({'symbol': 'BTCUSDT'})

    assert calls['count'] == 1  # malformed JSON is not retried


def test_gemini_provider_timeout_is_mapped(monkeypatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> None:
        raise TimeoutError('timed out')

    monkeypatch.setattr('ai.gemini_provider.urllib.request.urlopen', fake_urlopen)
    provider = GeminiProvider(api_key='fake-key', max_retries=0)

    with pytest.raises(AIProviderTimeout):
        provider.analyze({'symbol': 'BTCUSDT'})


def test_gemini_provider_rate_limit_is_mapped(monkeypatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> None:
        raise urllib.error.HTTPError(url='x', code=429, msg='Too Many Requests', hdrs=None, fp=None)

    monkeypatch.setattr('ai.gemini_provider.urllib.request.urlopen', fake_urlopen)
    provider = GeminiProvider(api_key='fake-key', max_retries=0)

    with pytest.raises(AIProviderRateLimited):
        provider.analyze({'symbol': 'BTCUSDT'})


def test_gemini_provider_error_never_leaks_api_key(monkeypatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> None:
        raise urllib.error.HTTPError(url='x', code=500, msg='Internal Server Error', hdrs=None, fp=None)

    monkeypatch.setattr('ai.gemini_provider.urllib.request.urlopen', fake_urlopen)
    provider = GeminiProvider(api_key='SUPER-SECRET-KEY-DO-NOT-LEAK', max_retries=0)

    with pytest.raises(AIProviderError) as excinfo:
        provider.analyze({'symbol': 'BTCUSDT'})

    assert 'SUPER-SECRET-KEY-DO-NOT-LEAK' not in str(excinfo.value)


# -- critical: AI can never override RiskManager ---------------------------

def test_ai_confirm_never_overrides_rejected_risk_decision(monkeypatch) -> None:
    from app import dashboard_data

    fake_analysis = {
        'symbol': 'BTCUSDT', 'status': 'LIVE', 'last_price': 100.0, 'direction': 'LONG',
        'technical_score': 80, 'confidence': 'HIGH', 'market_regime': 'TREND_UP',
        'timeframe_alignment': 'STRONG_LONG', 'trade_candidate': True,
        'reasons': [], 'block_reasons': [],
        'analysis': {
            '4h': {'trend': 'BULLISH', 'indicators': {}},
            '1h': {'trend': 'BULLISH', 'indicators': {}},
            '15m': {'trend': 'BULLISH', 'indicators': {}},
        },
        'volume_current': 100, 'volume_average': 90, 'volume_ratio': 1.1,
        'atr': 1.0, 'atr_percent': 1.0,
    }
    fake_setup = {
        'symbol': 'BTCUSDT', 'direction': 'LONG', 'setup_type': 'TREND_PULLBACK',
        'entry_zone_low': 99.0, 'entry_zone_high': 100.0, 'stop_loss': 95.0,
        'take_profit_1': 110.0, 'take_profit_2': 120.0, 'risk_reward_tp1': 2.0, 'risk_reward_tp2': 3.0,
        'invalidation_level': 95.0, 'confidence': 'HIGH', 'technical_score': 80, 'setup_score': 85,
        'status': 'READY', 'created_at': '', 'expires_at': '', 'reasons': [], 'rejection_reasons': [],
        'analysis_snapshot': {},
    }
    rejected_decision = {'approved': False, 'block_reasons': ['DAILY_LOSS_LIMIT'], 'reasons': ['blocked']}
    ai_confirm = AIAnalysisResult(
        status='CONNECTED', provider='gemini', direction='LONG', ai_score=95,
        decision='CONFIRM', reasons=['strong setup'], risk_flags=[],
    )

    monkeypatch.setattr(dashboard_data, '_market_analysis_service', SimpleNamespace(get_analysis=lambda s: fake_analysis))
    monkeypatch.setattr(dashboard_data, '_market_service', SimpleNamespace(get_symbol_data=lambda s: {'candles_15m': [], 'last_price': 100.0}))
    monkeypatch.setattr(dashboard_data, '_setup_engine', SimpleNamespace(build_setup=lambda a, m: fake_setup))
    monkeypatch.setattr(dashboard_data, '_risk_manager', SimpleNamespace(evaluate=lambda *a, **k: rejected_decision))
    monkeypatch.setattr(dashboard_data, '_ai_analyzer', SimpleNamespace(analyze=lambda *a, **k: ai_confirm))

    view = dashboard_data.get_brain_view('BTCUSDT')

    assert view['ai_analysis']['decision'] == 'CONFIRM'
    assert view['ai_analysis']['ai_score'] == 95
    # Even a confident AI CONFIRM must never flip an explicit RiskManager rejection.
    assert view['risk_decision']['approved'] is False
