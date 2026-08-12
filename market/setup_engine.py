from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from market.analysis_engine import MarketStructureAnalyzer
from config import analysis_settings as settings


@dataclass(frozen=True)
class TradeSetup:
    symbol: str
    direction: str
    setup_type: str
    entry_zone_low: float
    entry_zone_high: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    risk_reward_tp1: float
    risk_reward_tp2: float
    invalidation_level: float
    confidence: str
    technical_score: int
    setup_score: int
    status: str
    created_at: str
    expires_at: str
    reasons: list[str]
    rejection_reasons: list[str]
    analysis_snapshot: dict[str, Any]


class SupportResistanceAnalyzer:
    def __init__(self, tolerance_atr: float = settings.SUPPORT_RESISTANCE_TOLERANCE_ATR) -> None:
        self.tolerance_atr = tolerance_atr

    def analyze(self, candles: list[dict[str, Any]], atr: float | None) -> dict[str, list[float]]:
        swings = MarketStructureAnalyzer().identify_swings(candles)
        supports = [swing['price'] for swing in swings if swing['type'] == 'low']
        resistances = [swing['price'] for swing in swings if swing['type'] == 'high']
        if not supports and candles:
            supports = [min(candle['low'] for candle in candles)]
        if not resistances and candles:
            resistances = [max(candle['high'] for candle in candles)]
        tolerance = (atr or 0.0) * self.tolerance_atr
        if tolerance <= 0.0:
            tolerance = max(1.0, (candles[-1]['close'] if candles else 0.0) * 0.002)
        return {
            'supports': self._cluster_levels(sorted(set(supports)), tolerance),
            'resistances': self._cluster_levels(sorted(set(resistances)), tolerance),
        }

    @staticmethod
    def _cluster_levels(levels: list[float], tolerance: float) -> list[float]:
        if not levels:
            return []
        clustered: list[float] = []
        current_group = [levels[0]]
        for level in levels[1:]:
            if level - current_group[-1] <= tolerance:
                current_group.append(level)
            else:
                clustered.append(sum(current_group) / len(current_group))
                current_group = [level]
        clustered.append(sum(current_group) / len(current_group))
        return clustered

    @staticmethod
    def nearest_below(levels: list[float], price: float) -> float | None:
        candidates = [level for level in levels if level < price]
        return max(candidates) if candidates else None

    @staticmethod
    def nearest_above(levels: list[float], price: float) -> float | None:
        candidates = [level for level in levels if level > price]
        return min(candidates) if candidates else None


class SetupQualityFilter:
    def validate(
        self,
        setup: dict[str, Any],
        analysis: dict[str, Any],
        current_price: float,
        atr: float,
    ) -> dict[str, Any]:
        reasons: list[str] = []
        rejected = False

        entry_low = setup['entry_zone_low']
        entry_high = setup['entry_zone_high']
        stop_loss = setup['stop_loss']
        tp1 = setup['take_profit_1']
        tp2 = setup['take_profit_2']
        direction = setup['direction']

        if entry_high <= entry_low:
            rejected = True
            reasons.append('INVALID_ENTRY_ZONE')

        if entry_high - entry_low > atr * settings.MAX_ENTRY_ZONE_WIDTH_ATR:
            rejected = True
            reasons.append('ENTRY_ZONE_TOO_WIDE')

        if entry_high - entry_low < atr * settings.MIN_ENTRY_ZONE_WIDTH_ATR:
            reasons.append('ENTRY_ZONE_NARROW')

        if direction == 'LONG':
            if not (stop_loss < entry_low):
                rejected = True
                reasons.append('STOP_TOO_HIGH')
            if not (tp1 > entry_high and tp2 > tp1):
                rejected = True
                reasons.append('TP_WRONG_SIDE')
        elif direction == 'SHORT':
            if not (stop_loss > entry_high):
                rejected = True
                reasons.append('STOP_TOO_LOW')
            if not (tp1 < entry_low and tp2 < tp1):
                rejected = True
                reasons.append('TP_WRONG_SIDE')

        if setup['risk_reward_tp1'] < settings.MINIMUM_RR_TP1:
            rejected = True
            reasons.append('INSUFFICIENT_RR_TP1')
        if setup['risk_reward_tp2'] < settings.MINIMUM_RR_TP2:
            rejected = True
            reasons.append('INSUFFICIENT_RR_TP2')

        if analysis.get('market_regime') == 'HIGH_VOLATILITY' and analysis.get('last_price') is not None:
            rejected = True
            reasons.append('EXTREME_VOLATILITY')

        if direction == 'LONG' and current_price > entry_high + atr * settings.MAX_PRICE_DISTANCE_ATR:
            rejected = True
            reasons.append('MISSED_ENTRY')
        if direction == 'SHORT' and current_price < entry_low - atr * settings.MAX_PRICE_DISTANCE_ATR:
            rejected = True
            reasons.append('MISSED_ENTRY')

        if analysis.get('status') != 'LIVE':
            rejected = True
            reasons.append('STALE_DATA')

        if analysis.get('trade_candidate') is not True:
            rejected = True
            reasons.append('NO_TRADE_CANDIDATE')

        return {'valid': not rejected, 'reasons': reasons}


class TradeSetupEngine:
    STATUS_WAITING = 'WAITING'
    STATUS_READY = 'READY'
    STATUS_INVALIDATED = 'INVALIDATED'
    STATUS_EXPIRED = 'EXPIRED'
    STATUS_REJECTED = 'REJECTED'

    SETUP_TREND_PULLBACK = 'TREND_PULLBACK'
    SETUP_BREAKOUT_RETEST = 'BREAKOUT_RETEST'
    SETUP_MOMENTUM_CONTINUATION = 'MOMENTUM_CONTINUATION'

    REGIME_TO_SETUP = {
        'TREND_UP': {SETUP_TREND_PULLBACK, SETUP_MOMENTUM_CONTINUATION},
        'TREND_DOWN': {SETUP_TREND_PULLBACK, SETUP_MOMENTUM_CONTINUATION},
        'BREAKOUT_UP': {SETUP_BREAKOUT_RETEST, SETUP_MOMENTUM_CONTINUATION},
        'BREAKOUT_DOWN': {SETUP_BREAKOUT_RETEST, SETUP_MOMENTUM_CONTINUATION},
    }

    def __init__(self) -> None:
        self.support_resistance = SupportResistanceAnalyzer()
        self.quality_filter = SetupQualityFilter()

    def build_setup(self, analysis: dict[str, Any], symbol_data: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        created_at = now.isoformat()
        expires_at = (now + timedelta(seconds=settings.SETUP_TTL_SECONDS)).isoformat()

        setup_result: dict[str, Any] = {
            'symbol': analysis.get('symbol', 'UNKNOWN'),
            'direction': analysis.get('direction', 'NEUTRAL'),
            'setup_type': 'NO_SETUP',
            'entry_zone_low': 0.0,
            'entry_zone_high': 0.0,
            'stop_loss': 0.0,
            'take_profit_1': 0.0,
            'take_profit_2': 0.0,
            'risk_reward_tp1': 0.0,
            'risk_reward_tp2': 0.0,
            'invalidation_level': 0.0,
            'confidence': analysis.get('confidence', 'LOW'),
            'technical_score': int(analysis.get('technical_score', 0)),
            'setup_score': 0,
            'status': self.STATUS_REJECTED,
            'created_at': created_at,
            'expires_at': expires_at,
            'reasons': [],
            'rejection_reasons': [],
            'analysis_snapshot': self._build_snapshot(analysis, symbol_data),
        }

        if not analysis.get('trade_candidate'):
            setup_result['rejection_reasons'].append('TRADE_CANDIDATE_FALSE')
            setup_result['reasons'].append('Кандидат на сделку отсутствует')
            return setup_result

        market_regime = analysis.get('market_regime', 'UNKNOWN')
        if market_regime not in self.REGIME_TO_SETUP:
            setup_result['rejection_reasons'].append('REGIME_NOT_ALLOWED')
            setup_result['reasons'].append('Режим рынка не позволяет создавать сетап')
            return setup_result

        if analysis.get('status') != 'LIVE':
            setup_result['rejection_reasons'].append('STALE_DATA')
            setup_result['reasons'].append('Данные устарели или неактуальны')
            return setup_result

        if analysis.get('analysis', {}).get('15m', {}).get('volatility') == 'EXTREME' and market_regime not in ('BREAKOUT_UP', 'BREAKOUT_DOWN'):
            setup_result['rejection_reasons'].append('EXTREME_VOLATILITY')
            setup_result['reasons'].append('Экстремальная волатильность')
            return setup_result

        if not symbol_data:
            setup_result['rejection_reasons'].append('MISSING_MARKET_DATA')
            setup_result['reasons'].append('Недостаточно данных по рынку')
            return setup_result

        last_price = analysis.get('last_price')
        if last_price is None or last_price <= 0:
            setup_result['rejection_reasons'].append('INVALID_PRICE')
            setup_result['reasons'].append('Некорректная цена')
            return setup_result

        candles_15m = symbol_data.get('candles_15m', [])
        if not candles_15m:
            setup_result['rejection_reasons'].append('NO_CANDLES_15M')
            setup_result['reasons'].append('Нет 15-минутных свечей')
            return setup_result

        atr = analysis.get('atr')
        if atr is None or atr <= 0:
            setup_result['rejection_reasons'].append('NO_ATR')
            setup_result['reasons'].append('ATR не рассчитан')
            return setup_result

        timeframe_alignment = analysis.get('timeframe_alignment', 'NEUTRAL')
        trend_4h = analysis.get('analysis', {}).get('4h', {}).get('trend', 'NEUTRAL')
        trend_1h = analysis.get('analysis', {}).get('1h', {}).get('trend', 'NEUTRAL')
        if self._has_timeframe_conflict(trend_4h, trend_1h):
            setup_result['rejection_reasons'].append('TIMEFRAME_CONFLICT')
            setup_result['reasons'].append('Конфликт таймфреймов')
            return setup_result

        direction = analysis.get('direction', 'NEUTRAL')
        if direction not in ('LONG', 'SHORT'):
            setup_result['rejection_reasons'].append('NO_CLEAR_DIRECTION')
            setup_result['reasons'].append('Нет четкого направления')
            return setup_result

        chosen_type = self._choose_setup_type(market_regime, analysis, symbol_data)
        if chosen_type == 'NO_SETUP':
            setup_result['rejection_reasons'].append('NO_SETUP_TYPE')
            setup_result['reasons'].append('Не удалось подобрать тип сетапа')
            return setup_result

        builder = {
            self.SETUP_TREND_PULLBACK: self._build_trend_pullback,
            self.SETUP_BREAKOUT_RETEST: self._build_breakout_retest,
            self.SETUP_MOMENTUM_CONTINUATION: self._build_momentum_continuation,
        }.get(chosen_type)

        if builder is None:
            setup_result['rejection_reasons'].append('UNKNOWN_SETUP_TYPE')
            setup_result['reasons'].append('Неизвестный тип сетапа')
            return setup_result

        candidate = builder(analysis, symbol_data, last_price, atr)
        if candidate is None:
            setup_result['rejection_reasons'].append('INVALID_ZONE')
            setup_result['reasons'].append('Не удалось построить зону входа')
            return setup_result

        candidate['setup_type'] = chosen_type
        candidate['direction'] = direction
        candidate['confidence'] = analysis.get('confidence', 'LOW')
        candidate['technical_score'] = int(analysis.get('technical_score', 0))
        candidate['created_at'] = created_at
        candidate['expires_at'] = expires_at
        candidate['analysis_snapshot'] = setup_result['analysis_snapshot']

        quality_result = self.quality_filter.validate(candidate, analysis, last_price, atr)
        if not quality_result['valid']:
            candidate['status'] = self.STATUS_REJECTED
            candidate['rejection_reasons'] = quality_result['reasons']
            candidate['reasons'] = ['Сетап отклонён'] + quality_result['reasons']
            candidate['setup_score'] = self._calculate_setup_score(candidate, analysis, atr)
            return candidate

        candidate['setup_score'] = self._calculate_setup_score(candidate, analysis, atr)
        candidate['rejection_reasons'] = []
        candidate['reasons'] = self._build_setup_reasons(candidate, analysis)
        candidate['status'] = self._determine_status(candidate, last_price, atr)

        return candidate

    def _build_snapshot(self, analysis: dict[str, Any], symbol_data: dict[str, Any]) -> dict[str, Any]:
        return {
            'symbol': analysis.get('symbol', 'UNKNOWN'),
            'market_regime': analysis.get('market_regime'),
            'direction': analysis.get('direction'),
            'technical_score': analysis.get('technical_score'),
            'timeframe_alignment': analysis.get('timeframe_alignment'),
            'confidence': analysis.get('confidence'),
            'last_price': analysis.get('last_price'),
            'analysis_4h': analysis.get('analysis', {}).get('4h', {}),
            'analysis_1h': analysis.get('analysis', {}).get('1h', {}),
            'analysis_15m': analysis.get('analysis', {}).get('15m', {}),
            'candles_15m_count': len(symbol_data.get('candles_15m', [])),
        }

    def _has_timeframe_conflict(self, trend_4h: str, trend_1h: str) -> bool:
        return (trend_4h == 'BULLISH' and trend_1h == 'BEARISH') or (
            trend_4h == 'BEARISH' and trend_1h == 'BULLISH'
        )

    def _choose_setup_type(self, market_regime: str, analysis: dict[str, Any], symbol_data: dict[str, Any]) -> str:
        if market_regime in ('TREND_UP', 'TREND_DOWN'):
            return self.SETUP_TREND_PULLBACK
        if market_regime in ('BREAKOUT_UP', 'BREAKOUT_DOWN'):
            candles_15m = symbol_data.get('candles_15m', [])
            last_price = analysis.get('last_price') or 0.0
            upper = analysis.get('analysis', {}).get('15m', {}).get('indicators', {}).get('upper')
            lower = analysis.get('analysis', {}).get('15m', {}).get('indicators', {}).get('lower')
            if upper is not None and lower is not None and (
                (market_regime == 'BREAKOUT_UP' and last_price > upper) or
                (market_regime == 'BREAKOUT_DOWN' and last_price < lower)
            ):
                return self.SETUP_BREAKOUT_RETEST
            return self.SETUP_MOMENTUM_CONTINUATION
        return 'NO_SETUP'

    def _build_trend_pullback(
        self,
        analysis: dict[str, Any],
        symbol_data: dict[str, Any],
        current_price: float,
        atr: float,
    ) -> dict[str, Any] | None:
        candles = symbol_data.get('candles_15m', [])
        levels = self.support_resistance.analyze(candles, atr)
        supports = levels['supports']
        resistances = levels['resistances']
        ema20 = analysis.get('analysis', {}).get('15m', {}).get('indicators', {}).get('ema20')
        ema50 = analysis.get('analysis', {}).get('15m', {}).get('indicators', {}).get('ema50')
        direction = analysis.get('direction', 'NEUTRAL')

        if direction == 'LONG':
            support = self.support_resistance.nearest_below(supports, current_price)
            if support is None:
                return None
            entry_low = max(support, ema20 or support, ema50 or support)
            entry_high = min(current_price, entry_low + atr * 1.5, (self.support_resistance.nearest_above(resistances, current_price) or current_price) - atr * 0.25)
            entry_high = max(entry_low, entry_high)
            invalidation = support - atr * settings.ATR_BUFFER
            stop_loss = invalidation
            tp1 = self.support_resistance.nearest_above(resistances, current_price) or (entry_high + atr * 2.0)
            tp2 = tp1 + atr * 1.5
        else:
            resistance = self.support_resistance.nearest_above(resistances, current_price)
            if resistance is None:
                return None
            entry_high = min(resistance, ema20 or resistance, ema50 or resistance)
            entry_low = max(current_price, entry_high - atr * 1.5, (self.support_resistance.nearest_below(supports, current_price) or current_price) + atr * 0.25)
            entry_low = min(entry_high, entry_low)
            invalidation = resistance + atr * settings.ATR_BUFFER
            stop_loss = invalidation
            tp1 = self.support_resistance.nearest_below(supports, current_price) or (entry_low - atr * 2.0)
            tp2 = tp1 - atr * 1.5

        if entry_high <= entry_low:
            return None

        return self._compose_setup(entry_low, entry_high, stop_loss, tp1, tp2, current_price, direction, invalidation)

    def _build_breakout_retest(
        self,
        analysis: dict[str, Any],
        symbol_data: dict[str, Any],
        current_price: float,
        atr: float,
    ) -> dict[str, Any] | None:
        candles = symbol_data.get('candles_15m', [])
        levels = self.support_resistance.analyze(candles, atr)
        supports = levels['supports']
        resistances = levels['resistances']
        direction = analysis.get('direction', 'NEUTRAL')

        if direction == 'LONG':
            breakout_support = self.support_resistance.nearest_below(supports, current_price)
            if breakout_support is None:
                return None
            entry_low = max(breakout_support, current_price - atr * 1.0)
            entry_high = min(current_price, breakout_support + atr * 1.25)
            invalidation = breakout_support - atr * settings.ATR_BUFFER
            stop_loss = invalidation
            tp1 = self.support_resistance.nearest_above(resistances, current_price) or (current_price + atr * 2.0)
            tp2 = tp1 + atr * 1.5
        else:
            breakout_resistance = self.support_resistance.nearest_above(resistances, current_price)
            if breakout_resistance is None:
                return None
            entry_high = min(breakout_resistance, current_price + atr * 1.0)
            entry_low = max(current_price, breakout_resistance - atr * 1.25)
            invalidation = breakout_resistance + atr * settings.ATR_BUFFER
            stop_loss = invalidation
            tp1 = self.support_resistance.nearest_below(supports, current_price) or (current_price - atr * 2.0)
            tp2 = tp1 - atr * 1.5

        if entry_high <= entry_low:
            return None

        return self._compose_setup(entry_low, entry_high, stop_loss, tp1, tp2, current_price, direction, invalidation)

    def _build_momentum_continuation(
        self,
        analysis: dict[str, Any],
        symbol_data: dict[str, Any],
        current_price: float,
        atr: float,
    ) -> dict[str, Any] | None:
        candles = symbol_data.get('candles_15m', [])
        levels = self.support_resistance.analyze(candles, atr)
        supports = levels['supports']
        resistances = levels['resistances']
        direction = analysis.get('direction', 'NEUTRAL')

        if direction == 'LONG':
            support = self.support_resistance.nearest_below(supports, current_price)
            if support is None:
                return None
            entry_low = max(support, current_price - atr * 0.8)
            entry_high = min(current_price, entry_low + atr * 1.2)
            invalidation = support - atr * settings.ATR_BUFFER
            stop_loss = invalidation
            tp1 = self.support_resistance.nearest_above(resistances, current_price) or (entry_high + atr * 2.0)
            tp2 = tp1 + atr * 1.5
        else:
            resistance = self.support_resistance.nearest_above(resistances, current_price)
            if resistance is None:
                return None
            entry_high = min(resistance, current_price + atr * 0.8)
            entry_low = max(current_price, entry_high - atr * 1.2)
            invalidation = resistance + atr * settings.ATR_BUFFER
            stop_loss = invalidation
            tp1 = self.support_resistance.nearest_below(supports, current_price) or (entry_low - atr * 2.0)
            tp2 = tp1 - atr * 1.5

        if entry_high <= entry_low:
            return None

        return self._compose_setup(entry_low, entry_high, stop_loss, tp1, tp2, current_price, direction, invalidation)

    def _compose_setup(
        self,
        entry_low: float,
        entry_high: float,
        stop_loss: float,
        tp1: float,
        tp2: float,
        current_price: float,
        direction: str,
        invalidation: float,
    ) -> dict[str, Any] | None:
        if tp1 == tp2 or tp1 == entry_high:
            return None

        conservative_entry = entry_high if direction == 'LONG' else entry_low
        risk = abs(conservative_entry - stop_loss)
        if risk <= 0:
            return None
        reward1 = abs(tp1 - conservative_entry)
        reward2 = abs(tp2 - conservative_entry)
        rr_tp1 = reward1 / risk
        rr_tp2 = reward2 / risk

        return {
            'entry_zone_low': round(entry_low, 6),
            'entry_zone_high': round(entry_high, 6),
            'stop_loss': round(stop_loss, 6),
            'take_profit_1': round(tp1, 6),
            'take_profit_2': round(tp2, 6),
            'risk_reward_tp1': round(rr_tp1, 2),
            'risk_reward_tp2': round(rr_tp2, 2),
            'invalidation_level': round(invalidation, 6),
            'status': self.STATUS_WAITING,
        }

    def _calculate_setup_score(self, setup: dict[str, Any], analysis: dict[str, Any], atr: float) -> int:
        score = 0.0
        score += min(setup['technical_score'], 100) * settings.SETUP_SCORE_WEIGHTS['technical'] / 100.0

        entry_width = setup['entry_zone_high'] - setup['entry_zone_low']
        if entry_width <= atr * settings.MIN_ENTRY_ZONE_WIDTH_ATR:
            score += settings.SETUP_SCORE_WEIGHTS['entry'] * 0.9
        elif entry_width <= atr * settings.MAX_ENTRY_ZONE_WIDTH_ATR:
            score += settings.SETUP_SCORE_WEIGHTS['entry'] * 0.7
        else:
            score += settings.SETUP_SCORE_WEIGHTS['entry'] * 0.4

        rr1 = setup['risk_reward_tp1']
        rr2 = setup['risk_reward_tp2']
        score += min(rr1 / settings.MINIMUM_RR_TP1, 2.0) * settings.SETUP_SCORE_WEIGHTS['rr'] * 0.6
        score += min(rr2 / settings.MINIMUM_RR_TP2, 2.0) * settings.SETUP_SCORE_WEIGHTS['rr'] * 0.4

        if analysis.get('timeframe_alignment') in ('STRONG_LONG', 'STRONG_SHORT'):
            score += settings.SETUP_SCORE_WEIGHTS['alignment']
        elif analysis.get('timeframe_alignment') in ('LONG_BIAS', 'SHORT_BIAS'):
            score += settings.SETUP_SCORE_WEIGHTS['alignment'] * 0.6

        if analysis.get('market_regime') in ('TREND_UP', 'TREND_DOWN', 'BREAKOUT_UP', 'BREAKOUT_DOWN'):
            score += settings.SETUP_SCORE_WEIGHTS['structure']

        distance = abs(setup['entry_zone_high'] - setup['invalidation_level']) if setup['direction'] == 'LONG' else abs(setup['entry_zone_low'] - setup['invalidation_level'])
        if distance >= atr * 2.0:
            score += settings.SETUP_SCORE_WEIGHTS['distance']
        elif distance >= atr * 1.0:
            score += settings.SETUP_SCORE_WEIGHTS['distance'] * 0.6

        score = min(max(score, 0.0), 100.0)
        return int(round(score))

    def _determine_status(self, setup: dict[str, Any], current_price: float, atr: float) -> str:
        if datetime.now(timezone.utc) > datetime.fromisoformat(setup['expires_at']):
            return self.STATUS_EXPIRED
        if setup['direction'] == 'LONG':
            if setup['entry_zone_low'] <= current_price <= setup['entry_zone_high']:
                return self.STATUS_READY
            if current_price > setup['entry_zone_high']:
                return self.STATUS_WAITING
        else:
            if setup['entry_zone_low'] <= current_price <= setup['entry_zone_high']:
                return self.STATUS_READY
            if current_price < setup['entry_zone_low']:
                return self.STATUS_WAITING
        return self.STATUS_REJECTED

    def _build_setup_reasons(self, setup: dict[str, Any], analysis: dict[str, Any]) -> list[str]:
        reasons: list[str] = []
        reasons.append(f'Тип сетапа: {setup["setup_type"].replace("_", " ")}')
        reasons.append(f'Режим рынка: {analysis.get("market_regime", "UNKNOWN")}')
        reasons.append(f'Технический балл: {setup["technical_score"]}')
        if setup['risk_reward_tp1'] >= settings.MINIMUM_RR_TP1:
            reasons.append(f'RR1 {setup["risk_reward_tp1"]}:1')
        if setup['risk_reward_tp2'] >= settings.MINIMUM_RR_TP2:
            reasons.append(f'RR2 {setup["risk_reward_tp2"]}:1')
        return reasons
