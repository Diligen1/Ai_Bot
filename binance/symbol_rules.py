"""Binance USD-M Futures per-symbol trading rules — READ-ONLY, PUBLIC data.

Fetches PUBLIC exchangeInfo (market/binance_client.py::BinancePublicClient,
the same public client already used by MarketDataService — no API key, no
signature) and exposes tick size / step size / min quantity / min notional /
symbol status, so Live Execution Engine V1 (execution/live_execution_engine.py)
can round quantities/prices EXACTLY the way Binance requires instead of a
naive round(..., N), and reject anything that wouldn't pass Binance's own
filters. This module makes no signed/private calls and implements no
trading method.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from market.binance_client import BinanceApiError, BinancePublicClient


def _decimal(value: Any, default: str = '0') -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Floors `value` to the nearest multiple of `step` using exact Decimal
    arithmetic — never float round(..., N), which can silently produce a
    value Binance's filters would reject."""
    if step <= 0:
        return value
    return (value // step) * step


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    status: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal

    def round_price(self, price: float) -> Decimal:
        return _round_down_to_step(_decimal(price), self.tick_size)

    def round_quantity(self, quantity: float) -> Decimal:
        return _round_down_to_step(_decimal(quantity), self.step_size)


class BinanceSymbolRules:
    """Caches parsed exchangeInfo filters per symbol for the process lifetime
    (exchange filters change rarely; call `refresh()` to force a re-fetch)."""

    def __init__(self, client: BinancePublicClient | None = None) -> None:
        self._client = client or BinancePublicClient()
        self._rules_cache: dict[str, SymbolRules] | None = None

    def refresh(self) -> None:
        self._rules_cache = None

    def _load(self) -> dict[str, SymbolRules]:
        if self._rules_cache is not None:
            return self._rules_cache
        info = self._client.get_exchange_info()
        rules: dict[str, SymbolRules] = {}
        for entry in info.get('symbols', []) or []:
            symbol = entry.get('symbol')
            if not symbol:
                continue
            filters = {f.get('filterType'): f for f in entry.get('filters', []) or []}
            price_filter = filters.get('PRICE_FILTER', {})
            lot_filter = filters.get('LOT_SIZE', {})
            notional_filter = filters.get('MIN_NOTIONAL') or filters.get('NOTIONAL') or {}
            rules[symbol] = SymbolRules(
                symbol=symbol,
                status=entry.get('status', 'UNKNOWN'),
                tick_size=_decimal(price_filter.get('tickSize', '0')),
                step_size=_decimal(lot_filter.get('stepSize', '0')),
                min_qty=_decimal(lot_filter.get('minQty', '0')),
                min_notional=_decimal(notional_filter.get('notional', notional_filter.get('minNotional', '0'))),
            )
        self._rules_cache = rules
        return rules

    def get_rules(self, symbol: str) -> SymbolRules | None:
        try:
            rules = self._load()
        except BinanceApiError:
            return None
        return rules.get(symbol)
