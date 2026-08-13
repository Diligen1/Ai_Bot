"""Binance USD-M Futures ACCOUNT client — READ-ONLY, V1.

This client talks to the user's REAL Binance account (via API key/secret)
but implements ONLY signed GET (read) endpoints:

    GET /fapi/v2/account        -> get_account_info()
    GET /fapi/v2/balance        -> get_balance()
    GET /fapi/v2/positionRisk   -> get_position_risk()

...plus one PUBLIC, unsigned read endpoint used only to keep signed request
timestamps valid:

    GET /fapi/v1/time           -> _fetch_server_time_ms() (internal)

It MUST NEVER gain a method that places/cancels an order, changes leverage,
transfers funds, or withdraws — see
tests/test_binance_account_client.py::test_no_trading_or_transfer_methods_exist,
which fails the build if any such method appears on this class. No Spot
trading, no Futures trading, ever.

The API secret is only ever used in-memory to compute an HMAC-SHA256
request signature; it is never logged, never included in an exception
message, and never persisted to any file or database.

Clock sync (fixes Binance error -1021 "Timestamp for this request is
outside of the recvWindow"): every signed request's `timestamp` is the
local clock plus a cached offset from Binance's server time
(`server_time - local_time`, see `_get_server_time_offset_ms`). The offset
is fetched lazily on first use and cached for `TIME_OFFSET_REFRESH_SECONDS`
— it is NOT re-fetched before every request. If a signed request still
comes back with code -1021, the offset is force-refreshed and the SAME
request is retried exactly once (never looped).
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

# Binance error codes that mean "the key/secret/signature/permissions are
# wrong" as opposed to a transient/server-side problem.
AUTH_ERROR_CODES = {-1022, -2014, -2015}


class BinanceAccountError(Exception):
    pass


class BinanceAccountAuthError(BinanceAccountError):
    """Invalid API key/secret, bad signature, or insufficient permissions."""


class BinanceAccountTimeout(BinanceAccountError):
    pass


class BinanceAccountClient:
    BASE_URL = 'https://fapi.binance.com'
    DEFAULT_TIMEOUT = 7.0
    RECV_WINDOW_MS = 5000
    TIME_OFFSET_REFRESH_SECONDS = 1800.0  # re-sync at most every 30 min, not on every request
    TIMESTAMP_ERROR_CODE = -1021

    def __init__(self, api_key: str, api_secret: str, timeout: float | None = None) -> None:
        self.api_key = api_key
        self._api_secret = api_secret
        self.timeout = timeout or self.DEFAULT_TIMEOUT
        self._time_offset_ms = 0
        self._time_offset_synced_at: float | None = None  # time.monotonic() of last sync, None = never synced

    # -- server time sync (fixes Binance error -1021) -----------------------

    def _get_server_time_offset_ms(self, force: bool = False) -> int:
        """Cached `server_time_ms - local_time_ms`. Fetched lazily on first
        use and refreshed at most every TIME_OFFSET_REFRESH_SECONDS, or
        immediately when `force=True` (used after a -1021 response)."""
        now = time.monotonic()
        if not force and self._time_offset_synced_at is not None and (now - self._time_offset_synced_at) < self.TIME_OFFSET_REFRESH_SECONDS:
            return self._time_offset_ms
        local_time_ms = int(time.time() * 1000)
        server_time_ms = self._fetch_server_time_ms()
        self._time_offset_ms = server_time_ms - local_time_ms
        self._time_offset_synced_at = now
        return self._time_offset_ms

    def _fetch_server_time_ms(self) -> int:
        """GET /fapi/v1/time — public endpoint, no API key/signature needed."""
        data = self._request(f'{self.BASE_URL}/fapi/v1/time', signed=False)
        return int(data['serverTime'])

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

    def _request(self, url: str, signed: bool) -> Any:
        headers = {'Accept': 'application/json', 'User-Agent': 'AI-Futures-Bot/1.0'}
        if signed:
            headers['X-MBX-APIKEY'] = self.api_key
        request = Request(url, headers=headers, method='GET')
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw_body = response.read().decode('utf-8')
        except HTTPError as exc:
            body = exc.read().decode('utf-8', errors='ignore')
            if exc.code in (401, 403):
                raise BinanceAccountAuthError(f'Binance auth error (HTTP {exc.code})') from exc
            raise BinanceAccountError(f'Binance HTTP error {exc.code}: {body[:200]}') from exc
        except TimeoutError as exc:
            raise BinanceAccountTimeout('Binance account request timed out') from exc
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise BinanceAccountTimeout('Binance account request timed out') from exc
            raise BinanceAccountError(f'Binance network error: {exc.reason}') from exc

        try:
            return json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise BinanceAccountError('Binance returned a non-JSON response') from exc

    def _get(self, path: str, params: dict[str, Any] | None = None, _retry_on_timestamp_error: bool = True) -> Any:
        url = f'{self.BASE_URL}{path}?{self._signed_query(params or {})}'
        data = self._request(url, signed=True)

        if isinstance(data, dict) and isinstance(data.get('code'), int) and data.get('code') < 0:
            code = data['code']
            message = data.get('msg', 'unknown error')
            if code == self.TIMESTAMP_ERROR_CODE and _retry_on_timestamp_error:
                # Resync once and retry the SAME request exactly once — never looped.
                self._get_server_time_offset_ms(force=True)
                return self._get(path, params, _retry_on_timestamp_error=False)
            if code in AUTH_ERROR_CODES:
                raise BinanceAccountAuthError(f'Binance error {code}: {message}')
            raise BinanceAccountError(f'Binance error {code}: {message}')
        return data

    # -- READ-ONLY endpoints (no order/leverage/transfer/withdraw methods) --

    def get_account_info(self) -> dict[str, Any]:
        """GET /fapi/v2/account — wallet balance, unrealized PnL, account status flags."""
        return self._get('/fapi/v2/account')

    def get_balance(self) -> list[dict[str, Any]]:
        """GET /fapi/v2/balance — per-asset futures wallet balance."""
        return self._get('/fapi/v2/balance')

    def get_position_risk(self) -> list[dict[str, Any]]:
        """GET /fapi/v2/positionRisk — open positions, current leverage, unrealized PnL."""
        return self._get('/fapi/v2/positionRisk')
