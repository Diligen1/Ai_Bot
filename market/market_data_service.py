"""In-memory market data service for Binance USD-M Futures."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Lock, Thread
from time import sleep
from typing import Any, Iterable

from market.binance_client import BinanceApiError, BinancePublicClient


DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ZECUSDT"]
FAST_INTERVAL = 10.0
SLOW_INTERVAL = 60.0
STALE_SECONDS = 90


class MarketDataService:
    def __init__(self, symbols: Iterable[str] | None = None) -> None:
        self.client = BinancePublicClient()
        self.symbols = list(symbols) if symbols is not None else list(DEFAULT_SYMBOLS)
        self._lock = Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._initialized = False
        self._stop = False
        self._threads: list[Thread] = []
        self._load_initial_data()
        self._start_pollers()

    def _load_initial_data(self) -> None:
        self._refresh_symbol_list()
        self._refresh_slow_data()
        self._refresh_fast_data()
        self._initialized = True

    def _start_pollers(self) -> None:
        fast_thread = Thread(target=self._fast_loop, daemon=True)
        slow_thread = Thread(target=self._slow_loop, daemon=True)
        self._threads.extend([fast_thread, slow_thread])
        fast_thread.start()
        slow_thread.start()

    def _fast_loop(self) -> None:
        while not self._stop:
            self._refresh_fast_data()
            sleep(FAST_INTERVAL)

    def _slow_loop(self) -> None:
        while not self._stop:
            self._refresh_slow_data()
            sleep(SLOW_INTERVAL)

    def stop(self) -> None:
        self._stop = True
        for thread in self._threads:
            thread.join(timeout=1.0)

    def _refresh_symbol_list(self) -> None:
        try:
            exchange_info = self.client.get_exchange_info()
            symbols_data = {item["symbol"]: item for item in exchange_info.get("symbols", [])}
            with self._lock:
                for symbol in self.symbols:
                    self._data.setdefault(symbol, {})
                    self._data[symbol]["exchange_info"] = symbols_data.get(symbol)
        except BinanceApiError as exc:
            self._set_error_all(f"exchange_info_error: {exc}")

    def _refresh_fast_data(self) -> None:
        for symbol in self.symbols:
            self._fetch_price_snapshot(symbol)
            self._fetch_order_book(symbol)

    def _refresh_slow_data(self) -> None:
        for symbol in self.symbols:
            self._fetch_mark_price(symbol)
            self._fetch_ticker_24hr(symbol)
            self._fetch_open_interest(symbol)
            self._fetch_candles(symbol, "15m")
            self._fetch_candles(symbol, "1h")
            self._fetch_candles(symbol, "4h")

    def _fetch_price_snapshot(self, symbol: str) -> None:
        try:
            price_data = self.client.get_price(symbol)
            with self._lock:
                self._update_symbol(symbol, {
                    "last_price": float(price_data.get("price", 0.0)),
                    "last_price_ts": datetime.now(timezone.utc),
                    "status": "LIVE",
                })
        except BinanceApiError as exc:
            self._set_symbol_error(symbol, f"price_error: {exc}")

    def _fetch_mark_price(self, symbol: str) -> None:
        try:
            mark_data = self.client.get_mark_price(symbol)
            if isinstance(mark_data, list):
                mark_obj = mark_data[0]
            else:
                mark_obj = mark_data
            with self._lock:
                self._update_symbol(symbol, {
                    "mark_price": float(mark_obj.get("markPrice", 0.0)),
                    "funding_rate": float(mark_obj.get("lastFundingRate", 0.0)),
                    "mark_price_ts": datetime.now(timezone.utc),
                })
        except BinanceApiError as exc:
            self._set_symbol_error(symbol, f"mark_price_error: {exc}")

    def _fetch_ticker_24hr(self, symbol: str) -> None:
        try:
            ticker = self.client.get_ticker_24hr(symbol)
            with self._lock:
                self._update_symbol(symbol, {
                    "price_change_percent": float(ticker.get("priceChangePercent", 0.0)),
                    "volume": float(ticker.get("volume", 0.0)),
                    "ticker_ts": datetime.now(timezone.utc),
                })
        except BinanceApiError as exc:
            self._set_symbol_error(symbol, f"ticker_error: {exc}")

    def _fetch_open_interest(self, symbol: str) -> None:
        try:
            interest = self.client.get_open_interest(symbol)
            with self._lock:
                self._update_symbol(symbol, {
                    "open_interest": float(interest.get("openInterest", 0.0)),
                    "open_interest_ts": datetime.now(timezone.utc),
                })
        except BinanceApiError as exc:
            self._set_symbol_error(symbol, f"open_interest_error: {exc}")

    def _fetch_order_book(self, symbol: str) -> None:
        try:
            book = self.client.get_order_book(symbol)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            best_bid = float(bids[0][0]) if bids else 0.0
            best_ask = float(asks[0][0]) if asks else 0.0
            top_bid_volume = float(bids[0][1]) if bids else 0.0
            top_ask_volume = float(asks[0][1]) if asks else 0.0
            with self._lock:
                self._update_symbol(symbol, {
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "spread": best_ask - best_bid if best_ask and best_bid else 0.0,
                    "top_bid_volume": top_bid_volume,
                    "top_ask_volume": top_ask_volume,
                    "order_book_ts": datetime.now(timezone.utc),
                })
        except BinanceApiError as exc:
            self._set_symbol_error(symbol, f"order_book_error: {exc}")

    def _fetch_candles(self, symbol: str, interval: str) -> None:
        try:
            klines = self.client.get_klines(symbol, interval, limit=250)
            candles = [
                {
                    "open_time": int(item[0]),
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[5]),
                }
                for item in klines
            ]
            with self._lock:
                self._update_symbol(symbol, {f"candles_{interval}": candles, f"candles_{interval}_ts": datetime.now(timezone.utc)})
        except BinanceApiError as exc:
            self._set_symbol_error(symbol, f"candles_{interval}_error: {exc}")

    def _update_symbol(self, symbol: str, updates: dict[str, Any]) -> None:
        self._data.setdefault(symbol, {})
        self._data[symbol].update(updates)
        self._data[symbol]["last_update"] = datetime.now(timezone.utc)

    def _set_symbol_error(self, symbol: str, message: str) -> None:
        self._data.setdefault(symbol, {})
        self._data[symbol]["status"] = "ERROR"
        self._data[symbol]["error"] = message
        self._data[symbol]["last_update"] = datetime.now(timezone.utc)

    def _set_error_all(self, message: str) -> None:
        for symbol in self.symbols:
            self._set_symbol_error(symbol, message)

    def get_symbol_data(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            return self._data.get(symbol)

    def get_all_data(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {symbol: data.copy() for symbol, data in self._data.items()}

    def is_data_fresh(self, symbol: str) -> bool:
        with self._lock:
            data = self._data.get(symbol)
            if not data:
                return False
            last_update = data.get("last_update")
            if not isinstance(last_update, datetime):
                return False
            return datetime.now(timezone.utc) - last_update < timedelta(seconds=STALE_SECONDS)

    def validate_symbol(self, symbol: str) -> bool:
        try:
            exchange_info = self.client.get_exchange_info()
            for symbol_info in exchange_info.get("symbols", []):
                if symbol_info.get("symbol") == symbol and symbol_info.get("contractType") == "PERPETUAL" and symbol_info.get("status") == "TRADING":
                    return True
            return False
        except BinanceApiError:
            return False

    def set_symbols(self, symbols: Iterable[str] | None = None) -> None:
        with self._lock:
            self.symbols = list(symbols or DEFAULT_SYMBOLS)
            current = set(self.symbols)
            for symbol in self.symbols:
                self._data.setdefault(symbol, {})
            for symbol in list(self._data):
                if symbol not in current:
                    del self._data[symbol]
        self._refresh_symbol_list()
        self._refresh_fast_data()
        self._refresh_slow_data()
