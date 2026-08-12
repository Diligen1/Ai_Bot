"""Binance USD-M Futures public market client."""
from __future__ import annotations

import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from typing import Any


class BinanceApiError(Exception):
    pass


class BinancePublicClient:
    BASE_URL = "https://fapi.binance.com"
    DEFAULT_TIMEOUT = 7.0
    MAX_RETRIES = 3
    BACKOFF_BASE = 0.4

    def __init__(self, timeout: float | None = None, max_retries: int | None = None) -> None:
        self.timeout = timeout or self.DEFAULT_TIMEOUT
        self.max_retries = max_retries or self.MAX_RETRIES

    def _build_url(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = f"{self.BASE_URL}{path}"
        if params:
            query = urlencode(params)
            url = f"{url}?{query}"
        return url

    def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self._build_url(path, params)
        headers = {
            "Accept": "application/json",
            "User-Agent": "AI-Futures-Bot/1.0",
        }
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                request = Request(url, headers=headers)
                with urlopen(request, timeout=self.timeout) as response:
                    payload = response.read().decode("utf-8")
                    data = json.loads(payload)
                if isinstance(data, dict) and data.get("code") is not None:
                    raise BinanceApiError(f"Binance error {data.get('code')}: {data.get('msg')}")
                return data
            except HTTPError as error:
                body = error.read().decode("utf-8", errors="ignore")
                last_error = BinanceApiError(f"HTTP {error.code} {error.reason}: {body}")
                if 500 <= error.code < 600 and attempt < self.max_retries:
                    time.sleep(self.BACKOFF_BASE * attempt)
                    continue
                raise last_error
            except URLError as error:
                last_error = BinanceApiError(f"Network error: {error}")
                if attempt < self.max_retries:
                    time.sleep(self.BACKOFF_BASE * attempt)
                    continue
                raise last_error
            except json.JSONDecodeError as error:
                last_error = BinanceApiError(f"Response decode error: {error}")
                raise last_error
        raise last_error or BinanceApiError("Unknown Binance request failure")

    def get_exchange_info(self) -> dict[str, Any]:
        return self._request("/fapi/v1/exchangeInfo")

    def get_all_prices(self) -> list[dict[str, Any]]:
        return self._request("/fapi/v1/ticker/price")

    def get_price(self, symbol: str) -> dict[str, Any]:
        return self._request("/fapi/v1/ticker/price", {"symbol": symbol})

    def get_all_ticker_24hr(self) -> list[dict[str, Any]]:
        return self._request("/fapi/v1/ticker/24hr")

    def get_ticker_24hr(self, symbol: str) -> dict[str, Any]:
        return self._request("/fapi/v1/ticker/24hr", {"symbol": symbol})

    def get_all_premium_index(self) -> list[dict[str, Any]]:
        return self._request("/fapi/v1/premiumIndex")

    def get_mark_price(self, symbol: str) -> dict[str, Any]:
        return self._request("/fapi/v1/premiumIndex", {"symbol": symbol})

    def get_open_interest(self, symbol: str) -> dict[str, Any]:
        return self._request("/fapi/v1/openInterest", {"symbol": symbol})

    def get_order_book(self, symbol: str, limit: int = 5) -> dict[str, Any]:
        return self._request("/fapi/v1/depth", {"symbol": symbol, "limit": limit})

    def get_klines(self, symbol: str, interval: str, limit: int = 250) -> list[list[Any]]:
        return self._request("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
