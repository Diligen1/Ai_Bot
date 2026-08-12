"""Technical analysis engine for Binance USD-M Futures market data."""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from threading import Lock, Thread
from time import sleep
from typing import Any

from market.market_data_service import MarketDataService
from config import analysis_settings as settings


class IndicatorEngine:
    @staticmethod
    def _calculate_ema(values: list[float], period: int) -> list[float]:
        if len(values) < period:
            return []
        alpha = 2.0 / (period + 1)
        sma = sum(values[:period]) / period
        ema_values = [sma]
        for value in values[period:]:
            ema_values.append((value - ema_values[-1]) * alpha + ema_values[-1])
        return ema_values

    @staticmethod
    def _calculate_rsi(closes: list[float], period: int) -> float | None:
        if len(closes) <= period:
            return None
        gains: list[float] = []
        losses: list[float] = []
        for previous, current in zip(closes, closes[1:]):
            change = current - previous
            gains.append(max(change, 0.0))
            losses.append(max(-change, 0.0))
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        for gain, loss in zip(gains[period:], losses[period:]):
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def _calculate_macd(closes: list[float]) -> dict[str, float | None]:
        fast = IndicatorEngine._calculate_ema(closes, settings.MACD_FAST)
        slow = IndicatorEngine._calculate_ema(closes, settings.MACD_SLOW)
        if not fast or not slow:
            return {'macd_line': None, 'signal_line': None, 'histogram': None}
        offset = len(fast) - len(slow)
        if offset < 0:
            return {'macd_line': None, 'signal_line': None, 'histogram': None}
        macd_values = [fast[i + offset] - slow[i] for i in range(len(slow))]
        signal = IndicatorEngine._calculate_ema(macd_values, settings.MACD_SIGNAL)
        if not signal:
            return {'macd_line': None, 'signal_line': None, 'histogram': None}
        return {
            'macd_line': macd_values[-1],
            'signal_line': signal[-1],
            'histogram': macd_values[-1] - signal[-1],
        }

    @staticmethod
    def _calculate_atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> float | None:
        if len(closes) <= period:
            return None
        true_ranges: list[float] = []
        for prev_close, high, low in zip(closes, highs[1:], lows[1:]):
            tr1 = high - low
            tr2 = abs(high - prev_close)
            tr3 = abs(low - prev_close)
            true_ranges.append(max(tr1, tr2, tr3))
        if len(true_ranges) < period:
            return None
        atr = sum(true_ranges[:period]) / period
        for tr in true_ranges[period:]:
            atr = (atr * (period - 1) + tr) / period
        return atr

    @staticmethod
    def _calculate_bollinger(closes: list[float], period: int, stddev: float) -> dict[str, float | None]:
        if len(closes) < period:
            return {'middle': None, 'upper': None, 'lower': None, 'width': None}
        window = closes[-period:]
        middle = sum(window) / period
        variance = sum((price - middle) ** 2 for price in window) / period
        deviation = sqrt(variance)
        upper = middle + stddev * deviation
        lower = middle - stddev * deviation
        width = (upper - lower) / middle if middle else 0.0
        return {'middle': middle, 'upper': upper, 'lower': lower, 'width': width}

    @staticmethod
    def _calculate_volume_metrics(volumes: list[float], period: int) -> dict[str, float | None]:
        if len(volumes) < period:
            return {'current': None, 'average': None, 'ratio': None}
        current = volumes[-1]
        average = sum(volumes[-period:]) / period
        ratio = current / average if average and average > 0 else 0.0
        return {'current': current, 'average': average, 'ratio': ratio}

    def analyze(self, candles: list[dict[str, Any]]) -> dict[str, Any]:
        closes = [float(item['close']) for item in candles]
        highs = [float(item['high']) for item in candles]
        lows = [float(item['low']) for item in candles]
        volumes = [float(item['volume']) for item in candles]
        return {
            'ema20': self._calculate_ema(closes, settings.EMA_SHORT)[-1] if len(closes) >= settings.EMA_SHORT else None,
            'ema50': self._calculate_ema(closes, settings.EMA_MEDIUM)[-1] if len(closes) >= settings.EMA_MEDIUM else None,
            'ema200': self._calculate_ema(closes, settings.EMA_LONG)[-1] if len(closes) >= settings.EMA_LONG else None,
            'rsi14': self._calculate_rsi(closes, settings.RSI_PERIOD),
            **self._calculate_macd(closes),
            'atr14': self._calculate_atr(highs, lows, closes, settings.ATR_PERIOD),
            **self._calculate_bollinger(closes, settings.BB_PERIOD, settings.BB_STDDEV),
            **self._calculate_volume_metrics(volumes, settings.VOLUME_AVERAGE_PERIOD),
        }


class MarketStructureAnalyzer:
    def __init__(self, left: int = settings.SWING_LEFT, right: int = settings.SWING_RIGHT, min_move_pct: float = settings.SWING_MIN_MOVE_PCT) -> None:
        self.left = left
        self.right = right
        self.min_move_pct = min_move_pct

    def identify_swings(self, candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        highs = [float(item['high']) for item in candles]
        lows = [float(item['low']) for item in candles]
        swings: list[dict[str, Any]] = []
        if len(candles) < self.left + self.right + 1:
            return swings
        for index in range(self.left, len(candles) - self.right):
            window_highs = highs[index - self.left:index] + highs[index + 1:index + 1 + self.right]
            window_lows = lows[index - self.left:index] + lows[index + 1:index + 1 + self.right]
            if not window_highs or not window_lows:
                continue
            reference_high = max(window_highs)
            reference_low = min(window_lows)
            denominator_high = reference_high if reference_high != 0.0 else 1.0
            denominator_low = reference_low if reference_low != 0.0 else 1.0
            if highs[index] > reference_high:
                peak_move = (highs[index] - reference_high) / denominator_high * 100
                if peak_move >= self.min_move_pct:
                    swings.append({'type': 'high', 'index': index, 'price': highs[index]})
            if lows[index] < reference_low:
                trough_move = (reference_low - lows[index]) / denominator_low * 100
                if trough_move >= self.min_move_pct:
                    swings.append({'type': 'low', 'index': index, 'price': lows[index]})
        return swings

    def determine_structure(self, swings: list[dict[str, Any]]) -> str:
        if len(swings) < 3:
            return 'UNKNOWN'
        highs: list[float] = []
        lows: list[float] = []
        sequence: list[str] = []
        for swing in swings:
            if swing['type'] == 'high':
                highs.append(swing['price'])
            else:
                lows.append(swing['price'])
            sequence.append(swing['type'])
        bullish = len(highs) >= 2 and len(lows) >= 2 and all(h2 > h1 for h1, h2 in zip(highs, highs[1:])) and all(l2 > l1 for l1, l2 in zip(lows, lows[1:]))
        bearish = len(highs) >= 2 and len(lows) >= 2 and all(h2 < h1 for h1, h2 in zip(highs, highs[1:])) and all(l2 < l1 for l1, l2 in zip(lows, lows[1:]))
        if bullish:
            return 'BULLISH'
        if bearish:
            return 'BEARISH'
        if 'high' in sequence and 'low' in sequence:
            return 'RANGE'
        return 'UNKNOWN'

    def analyze(self, candles: list[dict[str, Any]]) -> dict[str, Any]:
        swings = self.identify_swings(candles)
        return {
            'swings': swings,
            'structure': self.determine_structure(swings),
        }


class TrendAnalyzer:
    def analyze(self, indicators: dict[str, Any], structure: str) -> dict[str, Any]:
        ema20 = indicators.get('ema20')
        ema50 = indicators.get('ema50')
        ema200 = indicators.get('ema200')
        histogram = indicators.get('histogram')
        macd_line = indicators.get('macd_line')
        signal_line = indicators.get('signal_line')
        if ema20 is None or ema50 is None or ema200 is None or histogram is None or macd_line is None or signal_line is None:
            return {'trend': 'NEUTRAL', 'ema_alignment': 'UNKNOWN', 'macd_confirm': False}
        ema_bullish = ema20 > ema50 > ema200
        ema_bearish = ema20 < ema50 < ema200
        macd_bullish = histogram > 0 and macd_line > signal_line
        macd_bearish = histogram < 0 and macd_line < signal_line
        structure_bullish = structure == 'BULLISH'
        structure_bearish = structure == 'BEARISH'
        bullish_count = sum((ema_bullish, macd_bullish, structure_bullish))
        bearish_count = sum((ema_bearish, macd_bearish, structure_bearish))
        if bullish_count >= 2 and not structure_bearish:
            return {'trend': 'BULLISH', 'ema_alignment': 'BULLISH' if ema_bullish else 'PARTIAL', 'macd_confirm': macd_bullish}
        if bearish_count >= 2 and not structure_bullish:
            return {'trend': 'BEARISH', 'ema_alignment': 'BEARISH' if ema_bearish else 'PARTIAL', 'macd_confirm': macd_bearish}
        return {'trend': 'NEUTRAL', 'ema_alignment': 'PARTIAL' if ema_bullish or ema_bearish else 'UNKNOWN', 'macd_confirm': macd_bullish or macd_bearish}


class VolatilityAnalyzer:
    def analyze(self, current_price: float | None, atr: float | None) -> dict[str, Any]:
        if current_price is None or atr is None or current_price <= 0:
            return {'atr_percent': None, 'volatility': 'UNKNOWN'}
        atr_percent = (atr / current_price) * 100
        if atr_percent < settings.VOLATILITY_THRESHOLDS['LOW']:
            state = 'LOW'
        elif atr_percent < settings.VOLATILITY_THRESHOLDS['NORMAL']:
            state = 'NORMAL'
        elif atr_percent < settings.VOLATILITY_THRESHOLDS['HIGH']:
            state = 'HIGH'
        else:
            state = 'EXTREME'
        return {'atr_percent': atr_percent, 'volatility': state}


class MarketRegimeDetector:
    def detect(
        self,
        trend: str,
        structure: str,
        atr_percent: float | None,
        band_width: float | None,
        volume_ratio: float | None,
        current_price: float | None,
        bollinger_upper: float | None,
        bollinger_lower: float | None,
    ) -> str:
        if atr_percent is None or band_width is None or volume_ratio is None or current_price is None:
            if structure == 'RANGE':
                return 'RANGE'
            return 'UNCERTAIN'
        breakout_up = (
            trend == 'BULLISH'
            and structure == 'BULLISH'
            and current_price > (bollinger_upper or 0)
            and volume_ratio >= settings.BREAKOUT_VOLUME_RATIO
            and band_width >= settings.BREAKOUT_BAND_WIDTH
        )
        breakout_down = (
            trend == 'BEARISH'
            and structure == 'BEARISH'
            and current_price < (bollinger_lower or 0)
            and volume_ratio >= settings.BREAKOUT_VOLUME_RATIO
            and band_width >= settings.BREAKOUT_BAND_WIDTH
        )
        if breakout_up:
            return 'BREAKOUT_UP'
        if breakout_down:
            return 'BREAKOUT_DOWN'
        if atr_percent >= settings.VOLATILITY_THRESHOLDS['HIGH']:
            return 'HIGH_VOLATILITY'
        if structure == 'RANGE' or band_width < settings.RANGE_BAND_WIDTH:
            return 'RANGE'
        if trend == 'BULLISH' and structure == 'BULLISH':
            return 'TREND_UP'
        if trend == 'BEARISH' and structure == 'BEARISH':
            return 'TREND_DOWN'
        return 'UNCERTAIN'


class MultiTimeframeAnalyzer:
    def align(self, trends: dict[str, str]) -> str:
        trend_4h = trends.get('4h', 'NEUTRAL')
        trend_1h = trends.get('1h', 'NEUTRAL')
        trend_15m = trends.get('15m', 'NEUTRAL')
        if trend_4h == 'BULLISH' and trend_1h == 'BULLISH' and trend_15m == 'BULLISH':
            return 'STRONG_LONG'
        if trend_4h == 'BEARISH' and trend_1h == 'BEARISH' and trend_15m == 'BEARISH':
            return 'STRONG_SHORT'
        if trend_4h == 'BULLISH' and trend_1h == 'BULLISH' and trend_15m == 'BEARISH':
            return 'LONG_BIAS'
        if trend_4h == 'BEARISH' and trend_1h == 'BEARISH' and trend_15m == 'BULLISH':
            return 'SHORT_BIAS'
        if trend_4h == 'BULLISH' and trend_1h == 'BEARISH':
            return 'CONFLICT'
        if trend_4h == 'BEARISH' and trend_1h == 'BULLISH':
            return 'CONFLICT'
        return 'NEUTRAL'


class TechnicalScoreEngine:
    def calculate(
        self,
        trend: str,
        ema_alignment: str,
        macd_confirm: bool,
        structure: str,
        volume_ratio: float | None,
        volatility: str,
        market_regime: str,
        timeframe_alignment: str,
    ) -> dict[str, Any]:
        score = 0.0
        reasons: list[str] = []

        if trend == 'BULLISH':
            score += settings.TECHNICAL_SCORE_WEIGHTS['trend']
            reasons.append('тренд восходящий')
        elif trend == 'BEARISH':
            score += settings.TECHNICAL_SCORE_WEIGHTS['trend']
            reasons.append('тренд нисходящий')
        else:
            reasons.append('тренд не ясен')

        if ema_alignment == 'BULLISH':
            score += settings.TECHNICAL_SCORE_WEIGHTS['ema_alignment']
            reasons.append('EMA выстроены по тренду')
        elif ema_alignment == 'BEARISH':
            score += settings.TECHNICAL_SCORE_WEIGHTS['ema_alignment']
            reasons.append('EMA выстроены по медвежьему тренду')
        else:
            score += settings.TECHNICAL_SCORE_WEIGHTS['ema_alignment'] * 0.4
            reasons.append('EMA дают смешанный сигнал')

        if macd_confirm:
            score += settings.TECHNICAL_SCORE_WEIGHTS['macd']
            reasons.append('MACD подтверждает направление')
        else:
            score += settings.TECHNICAL_SCORE_WEIGHTS['macd'] * 0.2
            reasons.append('MACD не однозначен')

        if structure == 'BULLISH' and trend == 'BULLISH':
            score += settings.TECHNICAL_SCORE_WEIGHTS['structure']
            reasons.append('структура рынка восходящая')
        elif structure == 'BEARISH' and trend == 'BEARISH':
            score += settings.TECHNICAL_SCORE_WEIGHTS['structure']
            reasons.append('структура рынка нисходящая')
        elif structure == 'RANGE':
            score += settings.TECHNICAL_SCORE_WEIGHTS['structure'] * 0.3
            reasons.append('рынок в диапазоне')
        else:
            score += settings.TECHNICAL_SCORE_WEIGHTS['structure'] * 0.2
            reasons.append('структура рынка не ясна')

        if volume_ratio is not None:
            if volume_ratio >= 1.0:
                score += settings.TECHNICAL_SCORE_WEIGHTS['volume_ratio']
                reasons.append('объём выше среднего')
            elif volume_ratio >= 0.8:
                score += settings.TECHNICAL_SCORE_WEIGHTS['volume_ratio'] * 0.6
                reasons.append('объём близок к среднему')
            else:
                score += settings.TECHNICAL_SCORE_WEIGHTS['volume_ratio'] * 0.2
                reasons.append('объём ниже среднего')
        else:
            reasons.append('данные по объёму недостаточны')

        if volatility == 'NORMAL' or volatility == 'LOW':
            score += settings.TECHNICAL_SCORE_WEIGHTS['volatility']
            reasons.append('волатильность приемлемая')
        elif volatility == 'HIGH':
            score += settings.TECHNICAL_SCORE_WEIGHTS['volatility'] * 0.5
            reasons.append('волатильность высокая')
        else:
            reasons.append('волатильность экстремальная или неизвестна')

        if market_regime in ('TREND_UP', 'TREND_DOWN', 'BREAKOUT_UP', 'BREAKOUT_DOWN'):
            score += settings.TECHNICAL_SCORE_WEIGHTS['market_regime']
            reasons.append('режим рынка подтверждает сигнал')
        elif market_regime == 'RANGE':
            score += settings.TECHNICAL_SCORE_WEIGHTS['market_regime'] * 0.4
            reasons.append('рынок находится в диапазоне')
        else:
            score += settings.TECHNICAL_SCORE_WEIGHTS['market_regime'] * 0.2
            reasons.append('режим рынка не определён')

        if timeframe_alignment in ('STRONG_LONG', 'STRONG_SHORT'):
            score += 5.0
            reasons.append('таймфреймы согласованы')
        elif timeframe_alignment in ('LONG_BIAS', 'SHORT_BIAS'):
            score += 2.5
            reasons.append('таймфреймы скорее согласованы')
        else:
            reasons.append('таймфреймы не согласованы')

        score = min(max(score, 0.0), 100.0)
        confidence = 'LOW'
        if score >= 70:
            confidence = 'HIGH'
        elif score >= 45:
            confidence = 'MEDIUM'

        direction = 'NEUTRAL'
        if trend == 'BULLISH' and score >= settings.TRADE_CANDIDATE_MIN_SCORE:
            direction = 'LONG'
        elif trend == 'BEARISH' and score >= settings.TRADE_CANDIDATE_MIN_SCORE:
            direction = 'SHORT'

        return {
            'direction': direction,
            'technical_score': int(round(score)),
            'confidence': confidence,
            'reasons': reasons,
        }


class TradeCandidateFilter:
    def evaluate(
        self,
        status: str,
        score: int,
        direction: str,
        timeframe_alignment: str,
        volume_ratio: float | None,
        volatility: str,
        structure: str,
        market_regime: str,
        insufficient_data: bool,
    ) -> dict[str, Any]:
        reasons: list[str] = []
        block_reasons: list[str] = []
        trade_candidate = True

        if status != 'LIVE':
            trade_candidate = False
            block_reasons.append('STALE_DATA' if status == 'STALE' else 'ERROR')
            reasons.append('данные устарели или недоступны')

        if insufficient_data:
            trade_candidate = False
            block_reasons.append('INSUFFICIENT_DATA')
            reasons.append('недостаточно данных для анализа')

        if timeframe_alignment == 'CONFLICT':
            trade_candidate = False
            block_reasons.append('TIMEFRAME_CONFLICT')
            reasons.append('конфликт между таймфреймами')

        if volatility == 'EXTREME' and market_regime not in ('BREAKOUT_UP', 'BREAKOUT_DOWN'):
            trade_candidate = False
            block_reasons.append('EXTREME_VOLATILITY')
            reasons.append('экстремальная волатильность')

        if structure == 'UNKNOWN':
            trade_candidate = False
            block_reasons.append('UNKNOWN_STRUCTURE')
            reasons.append('структура рынка не ясна')

        if volume_ratio is not None and volume_ratio < settings.VOLUME_RATIO_MIN:
            trade_candidate = False
            block_reasons.append('LOW_VOLUME')
            reasons.append('низкий объём')

        if direction == 'NEUTRAL':
            trade_candidate = False
            block_reasons.append('NO_CLEAR_DIRECTION')
            reasons.append('нет четкого направления')

        if score < settings.TRADE_CANDIDATE_MIN_SCORE:
            trade_candidate = False
            block_reasons.append('WEAK_TECHNICAL_SCORE')
            reasons.append('техническая оценка недостаточно высокая')

        return {
            'trade_candidate': trade_candidate,
            'reasons': reasons,
            'block_reasons': block_reasons,
        }


class MarketAnalysisService:
    REFRESH_INTERVAL = 30.0

    def __init__(self, data_service: MarketDataService) -> None:
        self.data_service = data_service
        self._lock = Lock()
        self._snapshot: dict[str, dict[str, Any]] = {}
        self._stop = False
        self._thread = Thread(target=self._refresh_loop, daemon=True)
        self._refresh_all()
        self._thread.start()

    def _refresh_loop(self) -> None:
        while not self._stop:
            sleep(self.REFRESH_INTERVAL)
            self._refresh_all()

    def stop(self) -> None:
        self._stop = True
        self._thread.join(timeout=1.0)

    def _extract_closed_candles(self, candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return candles[:-1] if len(candles) > 1 else []

    def _compute_timeframe_analysis(self, candles: list[dict[str, Any]], last_price: float | None) -> dict[str, Any]:
        closed = self._extract_closed_candles(candles)
        indicators = IndicatorEngine().analyze(closed)
        structure = MarketStructureAnalyzer().analyze(closed)
        trend = TrendAnalyzer().analyze(indicators, structure['structure'])
        volatility = VolatilityAnalyzer().analyze(last_price, indicators.get('atr14'))
        regime = MarketRegimeDetector().detect(
            trend['trend'],
            structure['structure'],
            volatility.get('atr_percent'),
            indicators.get('width'),
            indicators.get('ratio'),
            last_price,
            indicators.get('upper'),
            indicators.get('lower'),
        )
        return {
            'indicators': indicators,
            'structure': structure['structure'],
            'trend': trend['trend'],
            'ema_alignment': trend['ema_alignment'],
            'macd_confirm': trend['macd_confirm'],
            'volatility': volatility['volatility'],
            'atr_percent': volatility['atr_percent'],
            'market_regime': regime,
        }

    def _build_snapshot(self, symbol: str, data: dict[str, Any]) -> dict[str, Any]:
        status = data.get('status', 'STALE')
        last_price = float(data.get('last_price') or 0.0) if data.get('last_price') is not None else None
        summary: dict[str, Any] = {}
        insufficient = False
        for timeframe in ('4h', '1h', '15m'):
            timeframe_key = f'candles_{timeframe}'
            analysis = self._compute_timeframe_analysis(data.get(timeframe_key, []), last_price)
            summary[timeframe] = analysis
            if analysis['trend'] == 'NEUTRAL' or analysis['structure'] == 'UNKNOWN':
                insufficient = insufficient or True
        alignment = MultiTimeframeAnalyzer().align({
            '4h': summary['4h']['trend'],
            '1h': summary['1h']['trend'],
            '15m': summary['15m']['trend'],
        })
        score_data = TechnicalScoreEngine().calculate(
            summary['4h']['trend'],
            summary['4h']['ema_alignment'],
            summary['4h']['macd_confirm'],
            summary['4h']['structure'],
            summary['15m']['indicators'].get('ratio'),
            summary['15m']['volatility'],
            summary['4h']['market_regime'],
            alignment,
        )
        trade_filter = TradeCandidateFilter().evaluate(
            status,
            score_data['technical_score'],
            score_data['direction'],
            alignment,
            summary['15m']['indicators'].get('ratio'),
            summary['15m']['volatility'],
            summary['4h']['structure'],
            summary['4h']['market_regime'],
            insufficient,
        )
        return {
            'symbol': symbol,
            'status': status,
            'last_price': last_price,
            'direction': score_data['direction'],
            'technical_score': score_data['technical_score'],
            'confidence': score_data['confidence'],
            'market_regime': summary['4h']['market_regime'],
            'timeframe_alignment': alignment,
            'trade_candidate': trade_filter['trade_candidate'],
            'reasons': score_data['reasons'],
            'block_reasons': trade_filter['block_reasons'],
            'analysis': {
                '4h': summary['4h'],
                '1h': summary['1h'],
                '15m': summary['15m'],
            },
            'volume_current': summary['15m']['indicators'].get('current'),
            'volume_average': summary['15m']['indicators'].get('average'),
            'volume_ratio': summary['15m']['indicators'].get('ratio'),
            'atr': summary['15m']['indicators'].get('atr14'),
            'atr_percent': summary['15m']['atr_percent'],
            'last_update': data.get('last_update'),
        }

    def _refresh_all(self) -> None:
        snapshot: dict[str, dict[str, Any]] = {}
        raw_data = self.data_service.get_all_data()
        for symbol, data in raw_data.items():
            snapshot[symbol] = self._build_snapshot(symbol, data)
        with self._lock:
            self._snapshot = snapshot

    def get_analysis(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            return self._snapshot.get(symbol)

    def get_all_analysis(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {symbol: result.copy() for symbol, result in self._snapshot.items()}
