"""Tests for BinanceSymbolRules (binance/symbol_rules.py).

Uses a fake public client — no real network calls. This wraps the PUBLIC
exchangeInfo endpoint only (no API key, no signature).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from market.binance_client import BinanceApiError
from binance.symbol_rules import BinanceSymbolRules, SymbolRules


class FakePublicClient:
    def __init__(self, exchange_info: dict[str, Any]) -> None:
        self._exchange_info = exchange_info
        self.calls = 0

    def get_exchange_info(self) -> dict[str, Any]:
        self.calls += 1
        return self._exchange_info


def _exchange_info(*entries: dict[str, Any]) -> dict[str, Any]:
    return {'symbols': list(entries)}


BTCUSDT_ENTRY = {
    'symbol': 'BTCUSDT',
    'status': 'TRADING',
    'filters': [
        {'filterType': 'PRICE_FILTER', 'tickSize': '0.10', 'minPrice': '0', 'maxPrice': '1000000'},
        {'filterType': 'LOT_SIZE', 'stepSize': '0.001', 'minQty': '0.001', 'maxQty': '1000'},
        {'filterType': 'MIN_NOTIONAL', 'notional': '5'},
    ],
}


def test_btcusdt_rules_parsed_correctly() -> None:
    provider = BinanceSymbolRules(client=FakePublicClient(_exchange_info(BTCUSDT_ENTRY)))

    rules = provider.get_rules('BTCUSDT')

    assert rules is not None
    assert rules.symbol == 'BTCUSDT'
    assert rules.status == 'TRADING'
    assert rules.tick_size == Decimal('0.10')
    assert rules.step_size == Decimal('0.001')
    assert rules.min_qty == Decimal('0.001')
    assert rules.min_notional == Decimal('5')


def test_rules_are_cached_not_refetched_every_call() -> None:
    client = FakePublicClient(_exchange_info(BTCUSDT_ENTRY))
    provider = BinanceSymbolRules(client=client)

    provider.get_rules('BTCUSDT')
    provider.get_rules('BTCUSDT')

    assert client.calls == 1


def test_refresh_forces_a_refetch() -> None:
    client = FakePublicClient(_exchange_info(BTCUSDT_ENTRY))
    provider = BinanceSymbolRules(client=client)

    provider.get_rules('BTCUSDT')
    provider.refresh()
    provider.get_rules('BTCUSDT')

    assert client.calls == 2


def test_quantity_rounds_down_to_step_size_never_naive_round() -> None:
    rules = SymbolRules(symbol='BTCUSDT', status='TRADING', tick_size=Decimal('0.1'), step_size=Decimal('0.001'), min_qty=Decimal('0.001'), min_notional=Decimal('5'))

    # 0.15678 at step 0.001 must floor to 0.156, not round() to 0.157.
    assert rules.round_quantity(0.15678) == Decimal('0.156')


def test_price_rounds_down_to_tick_size_never_naive_round() -> None:
    rules = SymbolRules(symbol='BTCUSDT', status='TRADING', tick_size=Decimal('0.10'), step_size=Decimal('0.001'), min_qty=Decimal('0.001'), min_notional=Decimal('5'))

    # 50123.47 at tick 0.10 must floor to 50123.40.
    assert rules.round_price(50123.47) == Decimal('50123.40')


def test_rounding_never_rounds_up_past_the_step() -> None:
    rules = SymbolRules(symbol='ETHUSDT', status='TRADING', tick_size=Decimal('0.01'), step_size=Decimal('0.01'), min_qty=Decimal('0.01'), min_notional=Decimal('5'))

    # 1.999 at step 0.01 must floor to 1.99, never round up to 2.00 (Python's
    # round(1.999, 2) would give 2.0 here — exactly the bug this must avoid).
    assert rules.round_quantity(1.999) == Decimal('1.99')
    assert round(1.999, 2) == 2.0  # documents why naive round() is unsafe here


def test_unknown_symbol_returns_none() -> None:
    provider = BinanceSymbolRules(client=FakePublicClient(_exchange_info(BTCUSDT_ENTRY)))

    assert provider.get_rules('DOGEUSDT') is None


def test_exchange_info_error_returns_none_without_raising() -> None:
    class FailingClient:
        def get_exchange_info(self) -> dict[str, Any]:
            raise BinanceApiError('network down')

    provider = BinanceSymbolRules(client=FailingClient())

    assert provider.get_rules('BTCUSDT') is None
