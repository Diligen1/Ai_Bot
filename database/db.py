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
        self._migrate_trades_source_column()
        self._migrate_trades_paper_columns()
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
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS setups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                setup_type TEXT NOT NULL,
                entry_zone_low REAL,
                entry_zone_high REAL,
                stop_loss REAL,
                take_profit_1 REAL,
                take_profit_2 REAL,
                risk_reward_tp1 REAL,
                risk_reward_tp2 REAL,
                invalidation_level REAL,
                confidence TEXT,
                technical_score INTEGER,
                setup_score INTEGER,
                status TEXT,
                created_at TEXT,
                expires_at TEXT,
                rejection_reasons TEXT,
                analysis_snapshot TEXT
            )
            """
        )
        self._migrate_setups_execution_status_column()
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS paper_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                starting_equity REAL,
                ended_at TEXT
            )
            """
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS paper_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                status TEXT NOT NULL,
                entry_price REAL,
                planned_entry REAL,
                quantity_initial REAL,
                quantity_remaining REAL,
                margin_used REAL,
                leverage REAL,
                stop_loss REAL,
                take_profit_1 REAL,
                take_profit_2 REAL,
                tp1_hit INTEGER DEFAULT 0,
                tp2_hit INTEGER DEFAULT 0,
                realized_pnl REAL DEFAULT 0,
                fees_total REAL DEFAULT 0,
                opened_at TEXT,
                closed_at TEXT,
                setup_id INTEGER,
                strategy_version TEXT,
                setup_snapshot TEXT,
                risk_decision_snapshot TEXT,
                source TEXT DEFAULT 'paper'
            )
            """
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS spot_vault_transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER NOT NULL UNIQUE,
                symbol TEXT,
                profit REAL NOT NULL,
                spot_amount REAL NOT NULL,
                futures_amount REAL NOT NULL,
                created_at TEXT
            )
            """
        )
        self._migrate_spot_vault_transfers_percent_columns()
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

    def _migrate_trades_source_column(self) -> None:
        """Ensure trades.source exists and legacy/pre-migration rows are tagged 'seed'.

        Rows inserted before this migration (or by any caller that doesn't pass
        `source`) have no way of self-identifying as demo/seed data, so any row
        left without a source after the column exists is treated as historical
        seed data and must not be mixed into paper-trading equity calculations.
        """
        cursor = self._execute("PRAGMA table_info(trades)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'source' not in columns:
            self._execute("ALTER TABLE trades ADD COLUMN source TEXT")
        self._execute("UPDATE trades SET source = 'seed' WHERE source IS NULL")

    def _migrate_trades_paper_columns(self) -> None:
        """Link trades rows back to the PaperTradingEngine position/setup that produced them."""
        cursor = self._execute("PRAGMA table_info(trades)")
        columns = [row[1] for row in cursor.fetchall()]
        for column, ddl_type in (
            ('position_id', 'INTEGER'),
            ('setup_id', 'INTEGER'),
            ('strategy_version', 'TEXT'),
            ('setup_score', 'INTEGER'),
        ):
            if column not in columns:
                self._execute(f"ALTER TABLE trades ADD COLUMN {column} {ddl_type}")

    def _migrate_setups_execution_status_column(self) -> None:
        """Idempotency marker: a setup flips to 'EXECUTED' the moment PaperTradingEngine fills it."""
        cursor = self._execute("PRAGMA table_info(setups)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'execution_status' not in columns:
            self._execute("ALTER TABLE setups ADD COLUMN execution_status TEXT")
        self._execute("UPDATE setups SET execution_status = 'PENDING' WHERE execution_status IS NULL")

    def _migrate_spot_vault_transfers_percent_columns(self) -> None:
        """Records which spot/futures split percentages were actually used for each
        transfer, so changing the persistent setting later never reinterprets a
        historical split (see get_profit_split_settings/set_profit_split_settings)."""
        cursor = self._execute("PRAGMA table_info(spot_vault_transfers)")
        columns = [row[1] for row in cursor.fetchall()]
        for column in ('spot_percent_used', 'futures_percent_used'):
            if column not in columns:
                self._execute(f"ALTER TABLE spot_vault_transfers ADD COLUMN {column} REAL")

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
        source: str = 'paper',
        position_id: int | None = None,
        setup_id: int | None = None,
        strategy_version: str | None = None,
        setup_score: int | None = None,
    ) -> int:
        cursor = self._execute(
            """
            INSERT INTO trades (
                symbol, side, status, entry_price, exit_price, quantity, leverage,
                margin_used, stop_loss, take_profit, realized_pnl, net_pnl,
                fees, funding, risk_reward, signal_score, ai_score, historical_score,
                market_regime, entry_reason, exit_reason, opened_at, closed_at, source,
                position_id, setup_id, strategy_version, setup_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                source,
                position_id,
                setup_id,
                strategy_version,
                setup_score,
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

    def get_open_trades(self, source: str | None = None) -> list[sqlite3.Row]:
        if source is None:
            cursor = self._execute(
                "SELECT * FROM trades WHERE status != 'closed' ORDER BY opened_at DESC"
            )
        else:
            cursor = self._execute(
                "SELECT * FROM trades WHERE status != 'closed' AND source = ? ORDER BY opened_at DESC",
                (source,),
            )
        return cursor.fetchall()

    def get_trade_history(self, source: str | None = None) -> list[sqlite3.Row]:
        if source is None:
            cursor = self._execute(
                "SELECT * FROM trades WHERE status = 'closed' ORDER BY closed_at DESC"
            )
        else:
            cursor = self._execute(
                "SELECT * FROM trades WHERE status = 'closed' AND source = ? ORDER BY closed_at DESC",
                (source,),
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
        created_at = created_at or datetime.now(timezone.utc).isoformat()
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

    def add_setup(
        self,
        symbol: str,
        direction: str,
        setup_type: str,
        entry_zone_low: float | None = None,
        entry_zone_high: float | None = None,
        stop_loss: float | None = None,
        take_profit_1: float | None = None,
        take_profit_2: float | None = None,
        risk_reward_tp1: float | None = None,
        risk_reward_tp2: float | None = None,
        invalidation_level: float | None = None,
        confidence: str | None = None,
        technical_score: int | None = None,
        setup_score: int | None = None,
        status: str | None = None,
        created_at: str | None = None,
        expires_at: str | None = None,
        rejection_reasons: str | None = None,
        analysis_snapshot: str | None = None,
        execution_status: str = 'PENDING',
    ) -> int:
        cursor = self._execute(
            """
            INSERT INTO setups (
                symbol, direction, setup_type, entry_zone_low, entry_zone_high,
                stop_loss, take_profit_1, take_profit_2, risk_reward_tp1,
                risk_reward_tp2, invalidation_level, confidence, technical_score,
                setup_score, status, created_at, expires_at, rejection_reasons,
                analysis_snapshot, execution_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol,
                direction,
                setup_type,
                entry_zone_low,
                entry_zone_high,
                stop_loss,
                take_profit_1,
                take_profit_2,
                risk_reward_tp1,
                risk_reward_tp2,
                invalidation_level,
                confidence,
                technical_score,
                setup_score,
                status,
                created_at,
                expires_at,
                rejection_reasons,
                analysis_snapshot,
                execution_status,
            ),
        )
        return cursor.lastrowid

    def get_recent_setups(self, limit: int = 10) -> list[sqlite3.Row]:
        cursor = self._execute(
            "SELECT * FROM setups ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return cursor.fetchall()

    def mark_setup_executed(self, setup_id: int) -> None:
        self._execute(
            "UPDATE setups SET execution_status = 'EXECUTED' WHERE id = ?",
            (setup_id,),
        )

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

    def get_kill_switch(self) -> bool:
        return self.get_setting('risk_kill_switch') == 'true'

    def set_kill_switch(self, value: bool) -> None:
        self.set_setting('risk_kill_switch', 'true' if value else 'false')

    def get_paper_trading_enabled(self) -> bool:
        return self.get_setting('paper_trading_enabled') == 'true'

    def set_paper_trading_enabled(self, value: bool) -> None:
        self.set_setting('paper_trading_enabled', 'true' if value else 'false')

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
        today = datetime.now(timezone.utc).date().isoformat()
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

    def get_analytics_stats(self, source: str | None = None) -> dict[str, Any]:
        trades = self.get_trade_history(source=source)
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

    # -- Paper Trading Engine: sessions -------------------------------------

    def get_or_create_active_session(self, starting_equity: float, started_at: str | None = None) -> int:
        cursor = self._execute(
            "SELECT id FROM paper_sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
        )
        row = cursor.fetchone()
        if row:
            return row['id']
        started_at = started_at or datetime.now(timezone.utc).isoformat()
        cursor = self._execute(
            "INSERT INTO paper_sessions (started_at, starting_equity, ended_at) VALUES (?, ?, NULL)",
            (started_at, starting_equity),
        )
        return cursor.lastrowid

    def end_session(self, session_id: int, ended_at: str | None = None) -> None:
        ended_at = ended_at or datetime.now(timezone.utc).isoformat()
        self._execute(
            "UPDATE paper_sessions SET ended_at = ? WHERE id = ?",
            (ended_at, session_id),
        )

    # -- Paper Trading Engine: live positions --------------------------------

    def add_paper_position(
        self,
        session_id: int | None,
        symbol: str,
        direction: str,
        status: str,
        entry_price: float,
        planned_entry: float | None,
        quantity_initial: float,
        quantity_remaining: float,
        margin_used: float,
        leverage: float,
        stop_loss: float,
        take_profit_1: float,
        take_profit_2: float,
        opened_at: str,
        setup_id: int | None = None,
        strategy_version: str | None = None,
        setup_snapshot: str | None = None,
        risk_decision_snapshot: str | None = None,
        fees_total: float = 0.0,
    ) -> int:
        cursor = self._execute(
            """
            INSERT INTO paper_positions (
                session_id, symbol, direction, status, entry_price, planned_entry,
                quantity_initial, quantity_remaining, margin_used, leverage,
                stop_loss, take_profit_1, take_profit_2, tp1_hit, tp2_hit,
                realized_pnl, fees_total, opened_at, closed_at, setup_id,
                strategy_version, setup_snapshot, risk_decision_snapshot, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, NULL, ?, ?, ?, ?, 'paper')
            """,
            (
                session_id,
                symbol,
                direction,
                status,
                entry_price,
                planned_entry,
                quantity_initial,
                quantity_remaining,
                margin_used,
                leverage,
                stop_loss,
                take_profit_1,
                take_profit_2,
                fees_total,
                opened_at,
                setup_id,
                strategy_version,
                setup_snapshot,
                risk_decision_snapshot,
            ),
        )
        return cursor.lastrowid

    def update_paper_position(self, position_id: int, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            'status', 'quantity_remaining', 'tp1_hit', 'tp2_hit',
            'realized_pnl', 'fees_total', 'closed_at',
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown paper_positions fields: {sorted(unknown)}")
        set_clause = ', '.join(f"{key} = ?" for key in fields)
        params = list(fields.values()) + [position_id]
        self._execute(f"UPDATE paper_positions SET {set_clause} WHERE id = ?", tuple(params))

    def get_open_paper_positions(self, symbol: str | None = None) -> list[sqlite3.Row]:
        if symbol is None:
            cursor = self._execute(
                "SELECT * FROM paper_positions WHERE status IN ('OPEN', 'PARTIALLY_CLOSED') ORDER BY opened_at DESC"
            )
        else:
            cursor = self._execute(
                "SELECT * FROM paper_positions WHERE status IN ('OPEN', 'PARTIALLY_CLOSED') AND symbol = ? ORDER BY opened_at DESC",
                (symbol,),
            )
        return cursor.fetchall()

    def get_paper_position(self, position_id: int) -> sqlite3.Row | None:
        cursor = self._execute("SELECT * FROM paper_positions WHERE id = ?", (position_id,))
        return cursor.fetchone()

    # -- Profit Split V1: Virtual Spot Vault ---------------------------------

    def record_profit_split(
        self,
        trade_id: int,
        symbol: str,
        profit: float,
        spot_amount: float,
        futures_amount: float,
        spot_percent_used: float,
        futures_percent_used: float,
        created_at: str | None = None,
    ) -> int | None:
        """Inserts one profit-split row for a closed trade. `trade_id` is UNIQUE,
        so a second call for the same trade is rejected at the database level —
        this is the idempotency guard, not just an application-side check.
        Returns the new row id, or None if this trade was already processed."""
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        try:
            cursor = self._execute(
                """
                INSERT INTO spot_vault_transfers (
                    trade_id, symbol, profit, spot_amount, futures_amount,
                    spot_percent_used, futures_percent_used, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (trade_id, symbol, profit, spot_amount, futures_amount, spot_percent_used, futures_percent_used, created_at),
            )
        except sqlite3.IntegrityError:
            return None
        return cursor.lastrowid

    def get_spot_vault_balance(self) -> float:
        cursor = self._execute("SELECT COALESCE(SUM(spot_amount), 0.0) FROM spot_vault_transfers")
        row = cursor.fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    def get_last_profit_split(self) -> sqlite3.Row | None:
        cursor = self._execute("SELECT * FROM spot_vault_transfers ORDER BY id DESC LIMIT 1")
        return cursor.fetchone()

    def get_profit_split_for_trade(self, trade_id: int) -> sqlite3.Row | None:
        cursor = self._execute("SELECT * FROM spot_vault_transfers WHERE trade_id = ?", (trade_id,))
        return cursor.fetchone()

    def get_spot_vault_transfers(self) -> list[sqlite3.Row]:
        """All profit-split rows ever recorded — used by VirtualPortfolio to compute
        trading equity from each trade's ACTUALLY-applied split, never from
        whatever the current setting happens to be (old trades never recalculate)."""
        cursor = self._execute("SELECT * FROM spot_vault_transfers")
        return cursor.fetchall()

    # -- Profit Split V1: persistent spot/futures percentages ---------------

    def get_profit_split_settings(self) -> tuple[float, float]:
        """Returns (spot_percent, futures_percent), defaulting to 50/50 if never set."""
        spot_raw = self.get_setting('profit_split_spot_percent')
        futures_raw = self.get_setting('profit_split_futures_percent')
        if spot_raw is None or futures_raw is None:
            return 50.0, 50.0
        return float(spot_raw), float(futures_raw)

    def set_profit_split_settings(self, spot_percent: float, futures_percent: float) -> None:
        """Persists the Profit Split spot/futures percentages. Only ever affects
        NEW splits (see VirtualSpotVault.process_closed_trade) — trades already
        recorded in spot_vault_transfers keep the percentages they were split
        with, unaffected by this call."""
        if spot_percent < 0 or futures_percent < 0:
            raise ValueError('Проценты не могут быть отрицательными')
        if abs((spot_percent + futures_percent) - 100.0) > 1e-6:
            raise ValueError('Сумма процентов Spot и Futures должна быть равна 100%')
        self.set_setting('profit_split_spot_percent', str(spot_percent))
        self.set_setting('profit_split_futures_percent', str(futures_percent))
