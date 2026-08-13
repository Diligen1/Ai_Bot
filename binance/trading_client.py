"""Binance USD-M Futures TRADING client — Live Execution Engine V2.

Deliberately a SEPARATE class/file from binance/account_client.py
(READ-ONLY). That client's own guarantee ("only 3 GET methods, method='GET'
hardcoded") must stay provably true, so trading capability lives here
instead of being bolted onto it. This is the ONLY class in the project that
can ever send a mutating request to Binance, and it exposes a narrow,
explicit allow-list of exactly six methods — nothing generic, nothing that
accepts an arbitrary path:

    get_position_mode()   GET  /fapi/v1/positionSide/dual  (ONE-WAY/HEDGE)
    open_position()       POST /fapi/v1/order               (entry)
    place_stop_loss()     POST /fapi/v1/order (STOP_MARKET, protective)
    place_take_profit()   POST /fapi/v1/order (TAKE_PROFIT_MARKET, protective)
    get_order_status()    GET  /fapi/v1/order               (verify a fill)
    cancel_order()        DELETE /fapi/v1/order             (bot's own orders only)

Deliberately absent, forever, in this class: withdraw, any transfer
endpoint, margin/loan endpoints, Spot trading (/api/v3/*), change-leverage,
close-all-positions, or anything not in the list above. Actual position
reads reuse the already-audited READ-ONLY binance/account_client.py, not a
duplicate here.

Whether any of these methods is EVER actually called is entirely decided by
execution/live_trading_engine.py's gates (LIVE_TRADING_ENABLED, Live Kill
Switch, RiskGuard, etc.) — this class itself performs no gating; it is pure
transport plus Binance's request-signing mechanics.

Same clock-sync approach as binance/account_client.py (fixes error -1021)
kept as an independent implementation on purpose — a trading-capable client
must never share mutable state with the read-only one.

The API secret is only ever used in-memory to compute an HMAC-SHA256
signature; it is never logged, never included in an exception message, and
never persisted anywhere.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

AUTH_ERROR_CODES = {-1022, -2014, -2015}


class LiveTradingError(Exception):
    """`code`, when known, is the raw Binance error code (e.g. -2013 = order
    does not exist) — used by execution/live_trading_engine.py's
    reconciliation logic to distinguish "confirmed not found" from "could
    not check" without parsing exception message text."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class LiveTradingAuthError(LiveTradingError):
    """Invalid API key/secret, bad signature, or insufficient permissions."""


class LiveTradingTimeout(LiveTradingError):
    pass


class LiveTradingAPIError(LiveTradingError):
    pass


class LiveTradingRateLimited(LiveTradingError):
    pass


class LiveTradingClient:
    BASE_URL = 'https://fapi.binance.com'
    DEFAULT_TIMEOUT = 7.0
    RECV_WINDOW_MS = 5000
    TIME_OFFSET_REFRESH_SECONDS = 1800.0
    TIMESTAMP_ERROR_CODE = -1021

    def __init__(self, api_key: str, api_secret: str, timeout: float | None = None) -> None:
        self.api_key = api_key
        self._api_secret = api_secret
        self.timeout = timeout or self.DEFAULT_TIMEOUT
        self._time_offset_ms = 0
        self._time_offset_synced_at: float | None = None

    # -- server time sync (fixes Binance error -1021) -----------------------

    def _get_server_time_offset_ms(self, force: bool = False) -> int:
        now = time.monotonic()
        if not force and self._time_offset_synced_at is not None and (now - self._time_offset_synced_at) < self.TIME_OFFSET_REFRESH_SECONDS:
            return self._time_offset_ms
        local_time_ms = int(time.time() * 1000)
        server_time_ms = self._fetch_server_time_ms()
        self._time_offset_ms = server_time_ms - local_time_ms
        self._time_offset_synced_at = now
        return self._time_offset_ms

    def _fetch_server_time_ms(self) -> int:
        data = self._request(f'{self.BASE_URL}/fapi/v1/time', method='GET', signed=False)
        try:
            return int(data['serverTime'])
        except (KeyError, TypeError, ValueError) as exc:
            raise LiveTradingAPIError('Unexpected Binance server time response shape') from exc

    def _current_timestamp_ms(self) -> int:
        return int(time.time() * 1000) + self._get_server_time_offset_ms()

    # -- HTTP + signing -------------------------------------------------

    def _signed_query(self, params: dict[str, Any]) -> str:
        query_params = dict(params)
        query_params['timestamp'] = self._current_timestamp_ms()
        query_params.setdefault('recvWindow', self.RECV_WINDOW_MS)
        query = urlencode(query_params)
        signature = hmac.new(self._api_secret.encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()
        return f'{query}&signature={signature}'

    def _request(self, url: str, method: str, signed: bool) -> Any:
        headers = {'Accept': 'application/json', 'User-Agent': 'AI-Futures-Bot/1.0'}
        if signed:
            headers['X-MBX-APIKEY'] = self.api_key
        request = Request(url, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw_body = response.read().decode('utf-8')
        except HTTPError as exc:
            body = exc.read().decode('utf-8', errors='ignore')
            if exc.code in (401, 403):
                raise LiveTradingAuthError(f'Binance auth error (HTTP {exc.code})') from exc
            raise LiveTradingAPIError(f'Binance HTTP error {exc.code}: {body[:200]}') from exc
        except TimeoutError as exc:
            raise LiveTradingTimeout('Binance trading request timed out') from exc
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise LiveTradingTimeout('Binance trading request timed out') from exc
            raise LiveTradingAPIError(f'Binance network error: {exc.reason}') from exc

        try:
            return json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise LiveTradingAPIError('Binance returned a non-JSON response') from exc

    def _signed_request(self, path: str, method: str, params: dict[str, Any] | None = None, _retry_on_timestamp_error: bool = True) -> Any:
        url = f'{self.BASE_URL}{path}?{self._signed_query(params or {})}'
        data = self._request(url, method=method, signed=True)

        if isinstance(data, dict) and isinstance(data.get('code'), int) and data.get('code') < 0:
            code = data['code']
            message = data.get('msg', 'unknown error')
            if code == self.TIMESTAMP_ERROR_CODE and _retry_on_timestamp_error:
                self._get_server_time_offset_ms(force=True)
                return self._signed_request(path, method, params, _retry_on_timestamp_error=False)
            if code == -2019 or code == -2010:  # margin/insufficient balance/would-trigger-immediately etc.
                raise LiveTradingAPIError(f'Binance error {code}: {message}', code=code)
            if code in AUTH_ERROR_CODES:
                raise LiveTradingAuthError(f'Binance error {code}: {message}', code=code)
            if code == -1003:
                raise LiveTradingRateLimited(f'Binance error {code}: {message}', code=code)
            raise LiveTradingAPIError(f'Binance error {code}: {message}', code=code)
        return data

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._signed_request(path, 'GET', params)

    def _post(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._signed_request(path, 'POST', params)

    def _delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._signed_request(path, 'DELETE', params)

    # -- order building (private — all public entry points are narrow) ------

    def _place_order(
        self,
        symbol: str,
        side: str,
        position_side: str,
        order_type: str,
        quantity: float,
        price: float | None = None,
        stop_price: float | None = None,
        reduce_only: bool | None = None,
        time_in_force: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            'symbol': symbol,
            'side': side,
            'positionSide': position_side,
            'type': order_type,
            'quantity': quantity,
        }
        if price is not None:
            params['price'] = price
        if stop_price is not None:
            params['stopPrice'] = stop_price
        # Hedge Mode rejects `reduceOnly` outright — only ONE-WAY mode may send
        # it (see execution/live_trading_engine.py::_reduce_only_flag).
        if reduce_only is not None:
            params['reduceOnly'] = 'true' if reduce_only else 'false'
        if time_in_force is not None:
            params['timeInForce'] = time_in_force
        if client_order_id is not None:
            params['newClientOrderId'] = client_order_id
        return self._post('/fapi/v1/order', params)

    # -- the six allowed trading operations -------------------------------

    def get_position_mode(self) -> dict[str, Any]:
        """GET /fapi/v1/positionSide/dual -> {'dualSidePosition': bool}."""
        return self._get('/fapi/v1/positionSide/dual')

    def open_position(
        self,
        symbol: str,
        side: str,
        position_side: str,
        quantity: float,
        order_type: str = 'MARKET',
        price: float | None = None,
        time_in_force: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        tif = time_in_force or ('GTC' if order_type == 'LIMIT' else None)
        return self._place_order(symbol, side, position_side, order_type, quantity, price=price, time_in_force=tif, client_order_id=client_order_id)

    def place_stop_loss(
        self,
        symbol: str,
        side: str,
        position_side: str,
        quantity: float,
        stop_price: float,
        reduce_only: bool | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        return self._place_order(symbol, side, position_side, 'STOP_MARKET', quantity, stop_price=stop_price, reduce_only=reduce_only, client_order_id=client_order_id)

    def place_take_profit(
        self,
        symbol: str,
        side: str,
        position_side: str,
        quantity: float,
        stop_price: float,
        reduce_only: bool | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        return self._place_order(symbol, side, position_side, 'TAKE_PROFIT_MARKET', quantity, stop_price=stop_price, reduce_only=reduce_only, client_order_id=client_order_id)

    def get_order_status(self, symbol: str, order_id: str | None = None, client_order_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {'symbol': symbol}
        if order_id is not None:
            params['orderId'] = order_id
        if client_order_id is not None:
            params['origClientOrderId'] = client_order_id
        return self._get('/fapi/v1/order', params)

    def cancel_order(self, symbol: str, order_id: str | None = None, client_order_id: str | None = None) -> dict[str, Any]:
        """Only ever called by the engine with an order id it logged itself
        (see execution/live_trading_engine.py::cancel_protective_orders) —
        never with an arbitrary/unknown order id."""
        params: dict[str, Any] = {'symbol': symbol}
        if order_id is not None:
            params['orderId'] = order_id
        if client_order_id is not None:
            params['origClientOrderId'] = client_order_id
        return self._delete('/fapi/v1/order', params)
