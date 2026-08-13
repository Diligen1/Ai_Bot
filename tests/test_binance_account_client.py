"""Tests for the READ-ONLY Binance account connector
(binance/account_client.py, binance/account_connector.py).

All tests use a mock client or a monkeypatched `urllib.request.urlopen` —
no real network calls to Binance are ever made.
"""
from __future__ import annotations

import json
import time
import urllib.error
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from binance.account_client import (
    BinanceAccountAuthError,
    BinanceAccountClient,
    BinanceAccountError,
    BinanceAccountTimeout,
)
from binance.account_connector import BinanceAccountConnector

# -- architectural guard: this client must never be able to trade ---------

FORBIDDEN_METHOD_NAMES = (
    'create_order', 'place_order', 'cancel_order', 'new_order',
    'transfer', 'withdraw', 'change_leverage', 'set_leverage',
    'close_position', 'modify_position', 'change_margin_type',
)


def test_no_trading_or_transfer_methods_exist() -> None:
    for name in FORBIDDEN_METHOD_NAMES:
        assert not hasattr(BinanceAccountClient, name), f'BinanceAccountClient must not implement {name}()'


def test_client_only_exposes_read_methods() -> None:
    public_methods = {
        name for name in dir(BinanceAccountClient)
        if not name.startswith('_') and callable(getattr(BinanceAccountClient, name))
    }
    assert public_methods == {'get_account_info', 'get_balance', 'get_position_risk'}


# -- FakeClient-based connector tests ---------------------------------------

class FakeAccountClient:
    def __init__(
        self,
        account_info: dict[str, Any] | None = None,
        positions: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.account_info = account_info
        self.positions = positions
        self.error = error
        self.calls = 0

    def get_account_info(self) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.account_info is not None
        return self.account_info

    def get_position_risk(self) -> list[dict[str, Any]]:
        if self.error is not None:
            raise self.error
        assert self.positions is not None
        return self.positions


def test_missing_api_key_disables_connector(monkeypatch) -> None:
    # config/env.py::get_env() falls back to a module-level cache of the
    # real local .env file when a key isn't in os.environ. If that file
    # happens to contain real Binance credentials (a dev machine's actual
    # .env, never committed — see .gitignore), monkeypatch.delenv() alone
    # can't hide them: it only clears os.environ, not that cache. Clearing
    # the cache too makes this test depend solely on the values set here,
    # never on whatever's in a real local .env.
    monkeypatch.delenv('BINANCE_API_KEY', raising=False)
    monkeypatch.delenv('BINANCE_API_SECRET', raising=False)
    monkeypatch.setattr('config.env._env_file_cache', {})

    connector = BinanceAccountConnector()

    assert connector.is_enabled is False
    snapshot = connector.get_snapshot()
    assert snapshot.status == 'DISABLED'


def test_missing_secret_only_still_disables_connector(monkeypatch) -> None:
    monkeypatch.setenv('BINANCE_API_KEY', 'some-key')
    monkeypatch.delenv('BINANCE_API_SECRET', raising=False)
    monkeypatch.setattr('config.env._env_file_cache', {})  # see comment above

    connector = BinanceAccountConnector()

    assert connector.is_enabled is False


def test_valid_account_response_parses_correctly() -> None:
    account_info = {
        'totalWalletBalance': '1000.50',
        'availableBalance': '800.25',
        'totalUnrealizedProfit': '15.75',
        'canTrade': True,
    }
    positions = [
        {'symbol': 'BTCUSDT', 'positionAmt': '0.500', 'entryPrice': '50000.00', 'unRealizedProfit': '25.00', 'leverage': '5', 'positionSide': 'BOTH'},
        {'symbol': 'ETHUSDT', 'positionAmt': '0.000', 'entryPrice': '0.00', 'unRealizedProfit': '0.00', 'leverage': '10', 'positionSide': 'BOTH'},
    ]
    client = FakeAccountClient(account_info=account_info, positions=positions)
    connector = BinanceAccountConnector(client=client)

    snapshot = connector.get_snapshot()

    assert snapshot.status == 'CONNECTED'
    assert snapshot.wallet_balance == 1000.50
    assert snapshot.available_balance == 800.25
    assert snapshot.unrealized_pnl == 15.75
    assert snapshot.can_trade is True
    # ETHUSDT has positionAmt == 0 -> filtered out (flat, not an open position)
    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].symbol == 'BTCUSDT'
    assert snapshot.positions[0].position_amt == 0.5
    assert snapshot.positions[0].entry_price == 50000.0
    assert snapshot.positions[0].unrealized_pnl == 25.0
    assert snapshot.positions[0].leverage == 5


def test_open_positions_parsing_filters_zero_amt() -> None:
    account_info = {'totalWalletBalance': '0', 'availableBalance': '0', 'totalUnrealizedProfit': '0'}
    positions = [
        {'symbol': 'BTCUSDT', 'positionAmt': '0', 'entryPrice': '0', 'unRealizedProfit': '0', 'leverage': '5', 'positionSide': 'BOTH'},
        {'symbol': 'SOLUSDT', 'positionAmt': '-2.5', 'entryPrice': '100.0', 'unRealizedProfit': '-4.0', 'leverage': '3', 'positionSide': 'BOTH'},
    ]
    connector = BinanceAccountConnector(client=FakeAccountClient(account_info=account_info, positions=positions))

    snapshot = connector.get_snapshot()

    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].symbol == 'SOLUSDT'
    assert snapshot.positions[0].position_amt == -2.5  # SHORT


def test_invalid_key_returns_error_status_without_raising() -> None:
    client = FakeAccountClient(error=BinanceAccountAuthError('Binance error -2015: Invalid API-key'))
    connector = BinanceAccountConnector(client=client)

    snapshot = connector.get_snapshot()

    assert snapshot.status == 'ERROR'
    assert snapshot.error_message is not None


def test_timeout_returns_error_status_without_raising() -> None:
    client = FakeAccountClient(error=BinanceAccountTimeout('Binance account request timed out'))
    connector = BinanceAccountConnector(client=client)

    snapshot = connector.get_snapshot()

    assert snapshot.status == 'ERROR'


def test_unexpected_exception_also_degrades_to_error() -> None:
    client = FakeAccountClient(error=RuntimeError('client bug'))
    connector = BinanceAccountConnector(client=client)

    snapshot = connector.get_snapshot()

    assert snapshot.status == 'ERROR'


# -- BinanceAccountClient HTTP layer (monkeypatched urlopen, no network) ---

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


def test_client_sends_api_key_header_and_signature(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        if _is_time_endpoint(request):
            return _time_ok_response()
        captured['headers'] = dict(request.headers)
        captured['url'] = request.full_url
        return _FakeHTTPResponse(json.dumps({'totalWalletBalance': '100'}).encode('utf-8'))

    monkeypatch.setattr('binance.account_client.urlopen', fake_urlopen)
    client = BinanceAccountClient(api_key='my-api-key', api_secret='my-secret')

    client.get_account_info()

    assert captured['headers'].get('X-mbx-apikey') == 'my-api-key'
    assert 'signature=' in captured['url']
    assert 'my-secret' not in captured['url']  # the secret itself is never sent, only its HMAC


def test_time_endpoint_request_is_unsigned(monkeypatch) -> None:
    """The public /fapi/v1/time call must carry neither the API key header
    nor a signature — it needs no authentication at all."""
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        if _is_time_endpoint(request):
            captured['headers'] = dict(request.headers)
            captured['url'] = request.full_url
            return _time_ok_response()
        return _FakeHTTPResponse(json.dumps({'totalWalletBalance': '0'}).encode('utf-8'))

    monkeypatch.setattr('binance.account_client.urlopen', fake_urlopen)
    client = BinanceAccountClient(api_key='my-api-key', api_secret='my-secret')

    client.get_account_info()

    assert 'X-mbx-apikey' not in captured['headers']
    assert 'signature=' not in captured['url']


def test_client_raises_auth_error_on_401(monkeypatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> None:
        raise urllib.error.HTTPError(url='x', code=401, msg='Unauthorized', hdrs=None, fp=None)

    monkeypatch.setattr('binance.account_client.urlopen', fake_urlopen)
    client = BinanceAccountClient(api_key='bad-key', api_secret='bad-secret')

    with pytest.raises(BinanceAccountAuthError):
        client.get_account_info()


def test_client_raises_auth_error_on_binance_invalid_key_code(monkeypatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        if _is_time_endpoint(request):
            return _time_ok_response()
        return _FakeHTTPResponse(json.dumps({'code': -2015, 'msg': 'Invalid API-key, IP, or permissions for action.'}).encode('utf-8'))

    monkeypatch.setattr('binance.account_client.urlopen', fake_urlopen)
    client = BinanceAccountClient(api_key='bad-key', api_secret='bad-secret')

    with pytest.raises(BinanceAccountAuthError):
        client.get_account_info()


def test_client_raises_timeout(monkeypatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> None:
        raise TimeoutError('timed out')

    monkeypatch.setattr('binance.account_client.urlopen', fake_urlopen)
    client = BinanceAccountClient(api_key='key', api_secret='secret')

    with pytest.raises(BinanceAccountTimeout):
        client.get_account_info()


def test_client_error_never_leaks_api_secret(monkeypatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> None:
        raise urllib.error.HTTPError(url='x', code=500, msg='Internal Server Error', hdrs=None, fp=None)

    monkeypatch.setattr('binance.account_client.urlopen', fake_urlopen)
    client = BinanceAccountClient(api_key='key', api_secret='SUPER-SECRET-DO-NOT-LEAK')

    with pytest.raises(BinanceAccountError) as excinfo:
        client.get_account_info()

    assert 'SUPER-SECRET-DO-NOT-LEAK' not in str(excinfo.value)


def test_client_parses_position_risk_response(monkeypatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        if _is_time_endpoint(request):
            return _time_ok_response()
        return _FakeHTTPResponse(json.dumps([
            {'symbol': 'BTCUSDT', 'positionAmt': '1.0', 'entryPrice': '60000', 'unRealizedProfit': '100', 'leverage': '10', 'positionSide': 'BOTH'},
        ]).encode('utf-8'))

    monkeypatch.setattr('binance.account_client.urlopen', fake_urlopen)
    client = BinanceAccountClient(api_key='key', api_secret='secret')

    result = client.get_position_risk()

    assert result[0]['symbol'] == 'BTCUSDT'


# -- server time sync / fix for Binance error -1021 ------------------------

def test_server_time_offset_is_fetched_and_cached(monkeypatch) -> None:
    calls = {'time': 0}

    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        assert _is_time_endpoint(request)
        calls['time'] += 1
        return _time_ok_response()

    monkeypatch.setattr('binance.account_client.urlopen', fake_urlopen)
    client = BinanceAccountClient(api_key='key', api_secret='secret')

    offset_first = client._get_server_time_offset_ms()
    offset_second = client._get_server_time_offset_ms()

    assert offset_first == offset_second
    assert calls['time'] == 1  # cached on the second call, not re-fetched


def test_server_time_offset_force_refresh_refetches(monkeypatch) -> None:
    calls = {'time': 0}

    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        calls['time'] += 1
        return _time_ok_response()

    monkeypatch.setattr('binance.account_client.urlopen', fake_urlopen)
    client = BinanceAccountClient(api_key='key', api_secret='secret')

    client._get_server_time_offset_ms()
    client._get_server_time_offset_ms(force=True)

    assert calls['time'] == 2


def test_signed_request_timestamp_uses_server_offset(monkeypatch) -> None:
    """timestamp sent on a signed request must track the (offset-corrected)
    server clock, not the raw local clock — this is what fixes -1021."""
    local_snapshot_ms = int(time.time() * 1000)
    server_time_ms = local_snapshot_ms + 10_000_000  # server clock ~2.8h ahead of local
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        if _is_time_endpoint(request):
            return _time_ok_response(server_time_ms)
        captured['url'] = request.full_url
        return _FakeHTTPResponse(json.dumps({'totalWalletBalance': '0'}).encode('utf-8'))

    monkeypatch.setattr('binance.account_client.urlopen', fake_urlopen)
    client = BinanceAccountClient(api_key='key', api_secret='secret')

    client.get_account_info()

    query = parse_qs(urlparse(captured['url']).query)
    sent_timestamp = int(query['timestamp'][0])
    assert abs(sent_timestamp - server_time_ms) < 5000  # within a few seconds of test execution


def test_recv_window_is_not_inflated_to_mask_the_problem(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        if _is_time_endpoint(request):
            return _time_ok_response()
        captured['url'] = request.full_url
        return _FakeHTTPResponse(json.dumps({'totalWalletBalance': '0'}).encode('utf-8'))

    monkeypatch.setattr('binance.account_client.urlopen', fake_urlopen)
    client = BinanceAccountClient(api_key='key', api_secret='secret')

    client.get_account_info()

    query = parse_qs(urlparse(captured['url']).query)
    assert int(query['recvWindow'][0]) == BinanceAccountClient.RECV_WINDOW_MS == 5000


def test_error_1021_triggers_resync_and_single_retry(monkeypatch) -> None:
    calls = {'time': 0, 'account': 0}

    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        if _is_time_endpoint(request):
            calls['time'] += 1
            return _time_ok_response()
        calls['account'] += 1
        if calls['account'] == 1:
            return _FakeHTTPResponse(json.dumps({
                'code': -1021, 'msg': 'Timestamp for this request is outside of the recvWindow.',
            }).encode('utf-8'))
        return _FakeHTTPResponse(json.dumps({'totalWalletBalance': '250.00'}).encode('utf-8'))

    monkeypatch.setattr('binance.account_client.urlopen', fake_urlopen)
    client = BinanceAccountClient(api_key='key', api_secret='secret')

    result = client.get_account_info()

    assert result['totalWalletBalance'] == '250.00'
    assert calls['account'] == 2  # first attempt (failed) + exactly one retry (succeeded)
    assert calls['time'] == 2  # initial lazy sync + forced resync triggered by -1021


def test_error_1021_does_not_retry_infinitely(monkeypatch) -> None:
    calls = {'time': 0, 'account': 0}

    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        if _is_time_endpoint(request):
            calls['time'] += 1
            return _time_ok_response()
        calls['account'] += 1
        return _FakeHTTPResponse(json.dumps({
            'code': -1021, 'msg': 'Timestamp for this request is outside of the recvWindow.',
        }).encode('utf-8'))

    monkeypatch.setattr('binance.account_client.urlopen', fake_urlopen)
    client = BinanceAccountClient(api_key='key', api_secret='secret')

    with pytest.raises(BinanceAccountError):
        client.get_account_info()

    assert calls['account'] == 2  # original attempt + exactly one bounded retry, never unbounded


def test_error_1021_connector_degrades_to_error_status_not_crash(monkeypatch) -> None:
    """End-to-end: even a persistent -1021 must surface as ERROR through the
    connector, never crash the Dashboard."""
    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        if _is_time_endpoint(request):
            return _time_ok_response()
        return _FakeHTTPResponse(json.dumps({
            'code': -1021, 'msg': 'Timestamp for this request is outside of the recvWindow.',
        }).encode('utf-8'))

    monkeypatch.setattr('binance.account_client.urlopen', fake_urlopen)
    client = BinanceAccountClient(api_key='key', api_secret='secret')
    connector = BinanceAccountConnector(client=client)

    snapshot = connector.get_snapshot()

    assert snapshot.status == 'ERROR'
    assert snapshot.error_message is not None
    assert '-1021' in snapshot.error_message
