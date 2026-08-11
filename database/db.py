"""Database manager for trading data."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DatabaseManager:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or str(Path('data/trading.db'))
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        if self._conn:
            self._conn.close()
        self._conn = None

    def _execute(self, query: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        cursor = self._conn.cursor()
        cursor.execute(query, params)
        self._conn.commit()
        return cursor

    def create_tables(self) -> None:
        db_path_obj = Path(self.db_path)
        first_run = not db_path_obj.exists()
        self.connect()
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL,
                entry_price REAL,
                exit_price REAL,
                quantity REAL,
                leverage REAL,
                margin_used REAL,
                stop_loss REAL,
                take_profit REAL,
                realized_pnl REAL,
                net_pnl REAL,
                fees REAL,
                funding REAL,
                risk_reward REAL,
                signal_score REAL,
                ai_score REAL,
                historical_score REAL,
                market_regime TEXT,
                entry_reason TEXT,
                exit_reason TEXT,
                opened_at TEXT,
                closed_at TEXT
            )
            """
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                technical_score REAL,
                historical_score REAL,
                ai_score REAL,
                final_score REAL,
                decision TEXT,
                reason TEXT,
                created_at TEXT
            )
            """
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS whitelist_symbols (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL UNIQUE,
                created_at TEXT
            )
            """
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS bot_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                starting_balance REAL,
                ending_balance REAL,
                daily_pnl REAL,
                daily_pnl_percent REAL,
                trades_count INTEGER,
                wins INTEGER,
                losses INTEGER,
                max_drawdown REAL
            )
            """
        )
        if first_run:
            self._initialize_default_whitelist()
        else:
            initialized = self.get_setting('default_whitelist_initialized')
            if initialized != 'true':
                if self.get_whitelist_symbols():
                    self.set_setting('default_whitelist_initialized', 'true')
                else:
                    # Existing database with empty whitelist should remain empty.
                    self.set_setting('default_whitelist_initialized', 'true')

    def add_trade(
        self,
        symbol: str,
        side: str,
        status: str,
        entry_price: float | None = None,
        exit_price: float | None = None,
        quantity: float | None = None,
        leverage: float | None = None,
        margin_used: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        realized_pnl: float | None = None,
        net_pnl: float | None = None,
        fees: float | None = None,
        funding: float | None = None,
        risk_reward: float | None = None,
        signal_score: float | None = None,
        ai_score: float | None = None,
        historical_score: float | None = None,
        market_regime: str | None = None,
        entry_reason: str | None = None,
        exit_reason: str | None = None,
        opened_at: str | None = None,
        closed_at: str | None = None,
    ) -> int:
        cursor = self._execute(
            """
            INSERT INTO trades (
                symbol, side, status, entry_price, exit_price, quantity, leverage,
                margin_used, stop_loss, take_profit, realized_pnl, net_pnl,
                fees, funding, risk_reward, signal_score, ai_score, historical_score,
                market_regime, entry_reason, exit_reason, opened_at, closed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol,
                side,
                status,
                entry_price,
                exit_price,
                quantity,
                leverage,
                margin_used,
                stop_loss,
                take_profit,
                realized_pnl,
                net_pnl,
                fees,
                funding,
                risk_reward,
                signal_score,
                ai_score,
                historical_score,
                market_regime,
                entry_reason,
                exit_reason,
                opened_at,
                closed_at,
            ),
        )
        return cursor.lastrowid

    def close_trade(
        self,
        trade_id: int,
        exit_price: float | None = None,
        realized_pnl: float | None = None,
        net_pnl: float | None = None,
        fees: float | None = None,
        funding: float | None = None,
        exit_reason: str | None = None,
        closed_at: str | None = None,
        status: str = 'closed',
    ) -> None:
        self._execute(
            """
            UPDATE trades
            SET exit_price = ?, realized_pnl = ?, net_pnl = ?, fees = ?, funding = ?,
                exit_reason = ?, closed_at = ?, status = ?
            WHERE id = ?
            """,
            (
                exit_price,
                realized_pnl,
                net_pnl,
                fees,
                funding,
                exit_reason,
                closed_at,
                status,
                trade_id,
            ),
        )

    def get_open_trades(self) -> list[sqlite3.Row]:
        cursor = self._execute(
            "SELECT * FROM trades WHERE status != 'closed' ORDER BY opened_at DESC"
        )
        return cursor.fetchall()

    def get_trade_history(self) -> list[sqlite3.Row]:
        cursor = self._execute(
            "SELECT * FROM trades WHERE status = 'closed' ORDER BY closed_at DESC"
        )
        return cursor.fetchall()

    def add_signal(
        self,
        symbol: str,
        direction: str,
        technical_score: float | None = None,
        historical_score: float | None = None,
        ai_score: float | None = None,
        final_score: float | None = None,
        decision: str | None = None,
        reason: str | None = None,
        created_at: str | None = None,
    ) -> int:
        created_at = created_at or datetime.utcnow().isoformat()
        cursor = self._execute(
            """
            INSERT INTO signals (
                symbol, direction, technical_score, historical_score, ai_score,
                final_score, decision, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol,
                direction,
                technical_score,
                historical_score,
                ai_score,
                final_score,
                decision,
                reason,
                created_at,
            ),
        )
        return cursor.lastrowid

    def get_recent_signals(self, limit: int = 5) -> list[sqlite3.Row]:
        cursor = self._execute(
            "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return cursor.fetchall()

    def get_setting(self, key: str) -> str | None:
        cursor = self._execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,),
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self._execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def add_whitelist_symbol(self, symbol: str) -> int:
        normalized = symbol.strip().upper()
        cursor = self._execute(
            "INSERT OR IGNORE INTO whitelist_symbols (symbol, created_at) VALUES (?, ?)",
            (normalized, datetime.now(timezone.utc).isoformat()),
        )
        return cursor.lastrowid

    def remove_whitelist_symbol(self, symbol: str) -> None:
        self._execute(
            "DELETE FROM whitelist_symbols WHERE symbol = ?",
            (symbol.strip().upper(),),
        )

    def get_whitelist_symbols(self) -> list[sqlite3.Row]:
        cursor = self._execute(
            "SELECT * FROM whitelist_symbols ORDER BY symbol ASC"
        )
        return cursor.fetchall()

    def _initialize_default_whitelist(self) -> None:
        initialized = self.get_setting('default_whitelist_initialized') == 'true'
        if initialized:
            return
        default_symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'ZECUSDT']
        existing = [row['symbol'] for row in self.get_whitelist_symbols()]
        if not existing:
            for symbol in default_symbols:
                self.add_whitelist_symbol(symbol)
        self.set_setting('default_whitelist_initialized', 'true')

    def get_dashboard_stats(self) -> dict[str, Any]:
        open_trades = self.get_open_trades()
        closed_trades = self.get_trade_history()
        total_equity = 500.0 + sum(trade['net_pnl'] or 0.0 for trade in closed_trades)
        today = datetime.utcnow().date().isoformat()
        todays_trades = [trade for trade in closed_trades if trade['closed_at'] and trade['closed_at'].startswith(today)]
        trades_count = len(todays_trades)
        pnl = sum(trade['net_pnl'] or 0.0 for trade in todays_trades)
        wins = sum(1 for trade in todays_trades if trade['net_pnl'] is not None and trade['net_pnl'] > 0)
        losses = sum(1 for trade in todays_trades if trade['net_pnl'] is not None and trade['net_pnl'] <= 0)
        pnl_percent = (pnl / (total_equity - pnl) * 100) if total_equity != 0 and trades_count > 0 else 0.0
        return {
            'futures_balance': f'{total_equity:.2f} USDT',
            'spot_vault': '0 USDT',
            'total_equity': f'{total_equity:.2f} USDT',
            'pnl': f'{pnl:.2f} USDT',
            'pnl_percent': f'{pnl_percent:.2f}%',
            'trades': str(trades_count),
            'wins': str(wins),
            'losses': str(losses),
            'open_positions': [dict(trade) for trade in open_trades],
        }

    def get_analytics_stats(self) -> dict[str, Any]:
        trades = self.get_trade_history()
        total_trades = len(trades)
        wins = sum(1 for trade in trades if trade['net_pnl'] is not None and trade['net_pnl'] > 0)
        losses = sum(1 for trade in trades if trade['net_pnl'] is not None and trade['net_pnl'] <= 0)
        net_profit = sum(trade['net_pnl'] or 0.0 for trade in trades)
        total_profit = sum(trade['realized_pnl'] or 0.0 for trade in trades if trade['realized_pnl'] is not None and trade['realized_pnl'] > 0)
        total_loss = abs(sum(trade['realized_pnl'] or 0.0 for trade in trades if trade['realized_pnl'] is not None and trade['realized_pnl'] < 0))
        profit_factor = round(total_profit / total_loss, 2) if total_loss > 0 else 0.0
        average_r = round((net_profit / total_trades), 2) if total_trades > 0 else 0.0
        equity = 500.0
        running = equity
        peak = equity
        max_drawdown = 0.0
        for trade in sorted(trades, key=lambda t: t['closed_at'] or ''):
            running += trade['net_pnl'] or 0.0
            peak = max(peak, running)
            drawdown = peak - running
            max_drawdown = max(max_drawdown, drawdown)
        max_drawdown_percent = (max_drawdown / peak * 100) if peak > 0 else 0.0
        return {
            'total_trades': str(total_trades),
            'wins': str(wins),
            'losses': str(losses),
            'win_rate': f'{(wins/total_trades*100):.0f}%' if total_trades > 0 else '0%',
            'profit_factor': f'{profit_factor:.2f}',
            'average_r': f'{average_r:.2f}',
            'max_drawdown': f'{max_drawdown_percent:.2f}%',
            'net_profit': f'{net_profit:.2f} USDT',
        }
