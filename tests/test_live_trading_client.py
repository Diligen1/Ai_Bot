"""Tests for LiveTradingClient (binance/trading_client.py) — HTTP layer only.

All tests monkeypatch `urllib.request.urlopen` — no real network calls to
Binance are ever made, and this file never calls a real trading endpoint.
"""
from __future__ import annotations

import json
import urllib.error
from typing import Any

import pytest

from binance.trading_client import (
    LiveTradingAuthError,
    LiveTradingClient,
    LiveTradingError,
    LiveTradingRateLimited,
    LiveTradingTimeout,
)

DEFAULT_SERVER_TIME_MS = 1_700_000_000_000


class _FakeHTTPResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def _is_time_endpoint(request: Any) -> bool:
    return '/fapi/v1/time' in request.full_url


def _time_ok_response(server_time_ms: int = DEFAULT_SERVER_TIME_MS) -> _FakeHTTPResponse:
    return _FakeHTTPResponse(json.dumps({'serverTime': server_time_ms}).encode('utf-8'))


# -- architectural guard: only the six allowed operations exist -----------

def test_client_only_exposes_the_six_allowed_operations() -> None:
    public_methods = {
        name for name in dir(LiveTradingClient)
        if not name.startswith('_') and callable(getattr(LiveTradingClient, name))
    }
    assert public_methods == {
        'get_position_mode', 'open_position', 'place_stop_loss',
        'place_take_profit', 'get_order_status', 'cancel_order',
    }


FORBIDDEN_METHOD_NAMES = ('withdraw', 'transfer', 'margin_loan', 'borrow', 'repay', 'spot_order', 'change_leverage', 'set_leverage')


def test_no_withdraw_transfer_margin_or_spot_methods_exist() -> None:
    for name in FORBIDDEN_METHOD_NAMES:
        assert not hasattr(LiveTradingClient, name), f'LiveTradingClient must not implement {name}()'


def test_no_leverage_endpoint_referenced_anywhere_in_source() -> None:
    import inspect
    source = inspect.getsource(LiveTradingClient)
    assert '/fapi/v1/leverage' not in source
    assert '/sapi' not in source
    assert '/api/v3' not in source  # Spot API


# -- signing / headers ------------------------------------------------------

def test_open_position_sends_signed_post_with_api_key(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        if _is_time_endpoint(request):
            return _time_ok_response()
        captured['method'] = request.get_method()
        captured['headers'] = dict(request.headers)
        captured['url'] = request.full_url
        return _FakeHTTPResponse(json.dumps({'orderId': 111, 'status': 'NEW', 'executedQty': '0'}).encode('utf-8'))

    monkeypatch.setattr('binance.trading_client.urlopen', fake_urlopen)
    client = LiveTradingClient(api_key='my-key', api_secret='my-secret')

    client.open_position(symbol='BTCUSDT', side='BUY', position_side='LONG', quantity=0.01, client_order_id='bot-1-entry')

    assert captured['method'] == 'POST'
    assert captured['headers'].get('X-mbx-apikey') == 'my-key'
    assert 'signature=' in captured['url']
    assert 'my-secret' not in captured['url']


def test_cancel_order_sends_signed_delete(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        if _is_time_endpoint(request):
            return _time_ok_response()
        captured['method'] = request.get_method()
        return _FakeHTTPResponse(json.dumps({'orderId': 222, 'status': 'CANCELED'}).encode('utf-8'))

    monkeypatch.setattr('binance.trading_client.urlopen', fake_urlopen)
    client = LiveTradingClient(api_key='key', api_secret='secret')

    client.cancel_order('BTCUSDT', order_id='222')

    assert captured['method'] == 'DELETE'


def test_get_order_status_sends_signed_get(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        if _is_time_endpoint(request):
            return _time_ok_response()
        captured['method'] = request.get_method()
        return _FakeHTTPResponse(json.dumps({'orderId': 333, 'status': 'FILLED', 'executedQty': '0.01'}).encode('utf-8'))

    monkeypatch.setattr('binance.trading_client.urlopen', fake_urlopen)
    client = LiveTradingClient(api_key='key', api_secret='secret')

    client.get_order_status('BTCUSDT', order_id='333')

    assert captured['method'] == 'GET'


def test_reduce_only_and_client_order_id_are_forwarded(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        if _is_time_endpoint(request):
            return _time_ok_response()
        captured['url'] = request.full_url
        return _FakeHTTPResponse(json.dumps({'orderId': 444, 'status': 'NEW'}).encode('utf-8'))

    monkeypatch.setattr('binance.trading_client.urlopen', fake_urlopen)
    client = LiveTradingClient(api_key='key', api_secret='secret')

    client.place_stop_loss(symbol='BTCUSDT', side='SELL', position_side='BOTH', quantity=0.01, stop_price=49000.0, reduce_only=True, client_order_id='bot-1-sl')

    assert 'reduceOnly=true' in captured['url']
    assert 'newClientOrderId=bot-1-sl' in captured['url']
    assert 'stopPrice=49000' in captured['url']  # covers both 49000 and 49000.0 encodings


def test_hedge_mode_omits_reduce_only_param_entirely(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        if _is_time_endpoint(request):
            return _time_ok_response()
        captured['url'] = request.full_url
        return _FakeHTTPResponse(json.dumps({'orderId': 555, 'status': 'NEW'}).encode('utf-8'))

    monkeypatch.setattr('binance.trading_client.urlopen', fake_urlopen)
    client = LiveTradingClient(api_key='key', api_secret='secret')

    client.place_stop_loss(symbol='BTCUSDT', side='SELL', position_side='LONG', quantity=0.01, stop_price=49000.0, reduce_only=None, client_order_id='bot-1-sl')

    assert 'reduceOnly' not in captured['url']


# -- error mapping -----------------------------------------------------------

def test_401_raises_auth_error(monkeypatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        if _is_time_endpoint(request):
            return _time_ok_response()
        raise urllib.error.HTTPError(url='x', code=401, msg='Unauthorized', hdrs=None, fp=None)

    monkeypatch.setattr('binance.trading_client.urlopen', fake_urlopen)
    client = LiveTradingClient(api_key='key', api_secret='secret')

    with pytest.raises(LiveTradingAuthError):
        client.open_position(symbol='BTCUSDT', side='BUY', position_side='LONG', quantity=0.01)


def test_timeout_is_mapped(monkeypatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> None:
        if _is_time_endpoint(request):
            return _time_ok_response()
        raise TimeoutError('timed out')

    monkeypatch.setattr('binance.trading_client.urlopen', fake_urlopen)
    client = LiveTradingClient(api_key='key', api_secret='secret')

    with pytest.raises(LiveTradingTimeout):
        client.open_position(symbol='BTCUSDT', side='BUY', position_side='LONG', quantity=0.01)


def test_rate_limit_code_is_mapped(monkeypatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        if _is_time_endpoint(request):
            return _time_ok_response()
        return _FakeHTTPResponse(json.dumps({'code': -1003, 'msg': 'Too many requests'}).encode('utf-8'))

    monkeypatch.setattr('binance.trading_client.urlopen', fake_urlopen)
    client = LiveTradingClient(api_key='key', api_secret='secret')

    with pytest.raises(LiveTradingRateLimited):
        client.open_position(symbol='BTCUSDT', side='BUY', position_side='LONG', quantity=0.01)


def test_1021_triggers_resync_and_single_retry(monkeypatch) -> None:
    calls = {'time': 0, 'order': 0}

    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        if _is_time_endpoint(request):
            calls['time'] += 1
            return _time_ok_response()
        calls['order'] += 1
        if calls['order'] == 1:
            return _FakeHTTPResponse(json.dumps({'code': -1021, 'msg': 'Timestamp for this request is outside of the recvWindow.'}).encode('utf-8'))
        return _FakeHTTPResponse(json.dumps({'orderId': 1, 'status': 'NEW'}).encode('utf-8'))

    monkeypatch.setattr('binance.trading_client.urlopen', fake_urlopen)
    client = LiveTradingClient(api_key='key', api_secret='secret')

    result = client.open_position(symbol='BTCUSDT', side='BUY', position_side='LONG', quantity=0.01)

    assert result['orderId'] == 1
    assert calls['order'] == 2  # one retry, never unbounded


def test_error_never_leaks_api_secret(monkeypatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        if _is_time_endpoint(request):
            return _time_ok_response()
        raise urllib.error.HTTPError(url='x', code=500, msg='Internal Server Error', hdrs=None, fp=None)

    monkeypatch.setattr('binance.trading_client.urlopen', fake_urlopen)
    client = LiveTradingClient(api_key='key', api_secret='SUPER-SECRET-DO-NOT-LEAK')

    with pytest.raises(LiveTradingError) as excinfo:
        client.open_position(symbol='BTCUSDT', side='BUY', position_side='LONG', quantity=0.01)

    assert 'SUPER-SECRET-DO-NOT-LEAK' not in str(excinfo.value)
