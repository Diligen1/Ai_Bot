"""Risk Manager V2 — paper/virtual trading only.

Evaluates a Trade Setup against the current VirtualPortfolio state and a
centralized RiskConfig, and returns a RiskDecision describing whether a
(purely hypothetical) position would be approved and, if so, how it should
be sized. Nothing here ever opens a real or virtual position — it only
produces a decision object for display. No Binance account, no AI API.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from config.settings import DEFAULT_RISK_CONFIG, RiskConfig
from database.db import DatabaseManager
from trading.virtual_portfolio import VirtualPortfolio

NOT_READY_STATUSES = {'WAITING', 'INVALIDATED', 'EXPIRED', 'REJECTED', 'MISSED_ENTRY'}


class PositionSizeCalculator:
    """Sizes a paper position so that, simultaneously:

    - the loss at stop (including estimated fees/slippage) never exceeds the
      per-trade risk budget, and
    - the required margin never exceeds the configured max margin, and
    - leverage never exceeds the configured leverage.

    Both caps are enforced by construction: notional is capped at the lower
    of "risk budget implied notional" and "max margin implied notional".
    """

    def __init__(self, config: RiskConfig = DEFAULT_RISK_CONFIG) -> None:
        self.config = config

    def calculate(self, entry_price: float, stop_loss: float, current_equity: float) -> dict[str, float]:
        risk_budget = current_equity * self.config.risk_per_trade_percent / 100
        max_margin = current_equity * self.config.max_margin_percent / 100
        max_notional_by_margin = max_margin * self.config.leverage

        stop_distance = abs(entry_price - stop_loss)
        stop_distance_percent = (stop_distance / entry_price * 100) if entry_price else 0.0
        loss_rate_percent = stop_distance_percent + self.config.estimated_fee_percent + self.config.estimated_slippage_percent

        notional_by_risk = (risk_budget / (loss_rate_percent / 100)) if loss_rate_percent > 0 else 0.0
        notional = max(0.0, min(notional_by_risk, max_notional_by_margin))

        quantity = notional / entry_price if entry_price else 0.0
        margin_required = notional / self.config.leverage if self.config.leverage else 0.0
        planned_loss_at_stop = notional * (loss_rate_percent / 100)
        planned_loss_percent = (planned_loss_at_stop / current_equity * 100) if current_equity else 0.0

        return {
            'risk_budget': risk_budget,
            'max_margin': max_margin,
            'quantity': quantity,
            'notional': notional,
            'margin_required': margin_required,
            'planned_loss_at_stop': planned_loss_at_stop,
            'planned_loss_percent': planned_loss_percent,
            'stop_distance': stop_distance,
            'stop_distance_percent': stop_distance_percent,
        }


class RiskManager:
    """Evaluates Trade Setups against hard risk rules. Paper/virtual mode only."""

    def __init__(
        self,
        db: DatabaseManager,
        portfolio: VirtualPortfolio | None = None,
        config: RiskConfig = DEFAULT_RISK_CONFIG,
    ) -> None:
        self.db = db
        self.config = config
        self.portfolio = portfolio or VirtualPortfolio(db, config)
        self.sizer = PositionSizeCalculator(config)

    def evaluate(
        self,
        symbol: str,
        setup: dict[str, Any] | None,
        market_status: str = 'LIVE',
        now: datetime | None = None,
    ) -> dict[str, Any]:
        reasons: list[str] = []
        block_reasons: list[str] = []

        state = self.portfolio.get_state(now=now)

        if self.portfolio.is_kill_switch_on():
            block_reasons.append('KILL_SWITCH_ON')
            reasons.append('Аварийная защита (Kill Switch) включена')

        has_setup = bool(setup) and setup.get('setup_type') != 'NO_SETUP'
        if not has_setup:
            block_reasons.append('NO_SETUP')
            reasons.append('Сетап отсутствует')
        elif setup.get('status') in NOT_READY_STATUSES or setup.get('status') is None:
            block_reasons.append('SETUP_NOT_READY')
            reasons.append(f"Сетап не готов к исполнению (статус {setup.get('status')})")

        if market_status != 'LIVE':
            block_reasons.append('STALE_MARKET_DATA')
            reasons.append('Рыночные данные устарели или недоступны')

        direction = (setup or {}).get('direction', 'NEUTRAL')
        stop_loss = (setup or {}).get('stop_loss')
        risk_reward = (setup or {}).get('risk_reward_tp1')
        entry_price: float | None = None
        if setup:
            entry_low = setup.get('entry_zone_low')
            entry_high = setup.get('entry_zone_high')
            if direction == 'LONG':
                entry_price = entry_high
            elif direction == 'SHORT':
                entry_price = entry_low

        if has_setup and risk_reward is not None and risk_reward < self.config.minimum_risk_reward:
            block_reasons.append('RR_BELOW_MINIMUM')
            reasons.append(f'Risk/Reward {risk_reward} ниже минимума {self.config.minimum_risk_reward}')

        if state.daily_drawdown_percent >= self.config.daily_loss_limit_percent:
            block_reasons.append('DAILY_LOSS_LIMIT')
            reasons.append('Достигнут дневной лимит убытков')

        if state.weekly_drawdown_percent >= self.config.weekly_loss_limit_percent:
            block_reasons.append('WEEKLY_LOSS_LIMIT')
            reasons.append('Достигнут недельный лимит убытков')

        if state.consecutive_losses >= self.config.max_consecutive_losses:
            block_reasons.append('LOSS_STREAK_LIMIT')
            reasons.append('Превышена допустимая серия убыточных сделок подряд')

        if state.open_positions_count >= self.config.max_open_positions:
            block_reasons.append('MAX_OPEN_POSITIONS')
            reasons.append('Достигнут лимит открытых виртуальных позиций')

        if self.portfolio.has_open_position(symbol):
            block_reasons.append('DUPLICATE_SYMBOL_POSITION')
            reasons.append('По этому символу уже есть открытая виртуальная позиция')

        risk_budget = state.current_equity * self.config.risk_per_trade_percent / 100
        position_quantity = 0.0
        position_notional = 0.0
        margin_required = 0.0
        planned_loss_at_stop = 0.0
        planned_loss_percent = 0.0
        stop_distance = 0.0
        stop_distance_percent = 0.0

        valid_prices = (
            entry_price is not None
            and stop_loss is not None
            and entry_price > 0
            and stop_loss > 0
            and entry_price != stop_loss
        )
        if not valid_prices:
            if has_setup:
                block_reasons.append('INVALID_STOP')
                reasons.append('Некорректная цена входа или стоп-лосс')
        else:
            sizing = self.sizer.calculate(entry_price=entry_price, stop_loss=stop_loss, current_equity=state.current_equity)
            position_quantity = sizing['quantity']
            position_notional = sizing['notional']
            margin_required = sizing['margin_required']
            planned_loss_at_stop = sizing['planned_loss_at_stop']
            planned_loss_percent = sizing['planned_loss_percent']
            stop_distance = sizing['stop_distance']
            stop_distance_percent = sizing['stop_distance_percent']
            risk_budget = sizing['risk_budget']

            if position_quantity <= 0:
                block_reasons.append('POSITION_SIZE_ZERO')
                reasons.append('Рассчитанный размер позиции равен нулю')
            if margin_required > sizing['max_margin'] + 1e-9:
                block_reasons.append('MARGIN_EXCEEDS_LIMIT')
                reasons.append('Требуемая маржа превышает лимит')
            if planned_loss_at_stop > risk_budget + 1e-9:
                block_reasons.append('PLANNED_LOSS_EXCEEDS_BUDGET')
                reasons.append('Плановый убыток превышает риск-бюджет')

        approved = len(block_reasons) == 0 and position_quantity > 0
        if approved:
            reasons.append('Все риск-проверки пройдены')

        return {
            'approved': approved,
            'symbol': symbol,
            'direction': direction,
            'current_equity': state.current_equity,
            'available_equity': state.available_equity,
            'risk_budget': risk_budget,
            'entry_price': entry_price,
            'stop_loss': stop_loss,
            'stop_distance': stop_distance,
            'stop_distance_percent': stop_distance_percent,
            'leverage': self.config.leverage,
            'position_quantity': position_quantity,
            'position_notional': position_notional,
            'margin_required': margin_required,
            'planned_loss_at_stop': planned_loss_at_stop,
            'planned_loss_percent': planned_loss_percent,
            'risk_reward': risk_reward,
            'daily_drawdown_percent': state.daily_drawdown_percent,
            'weekly_drawdown_percent': state.weekly_drawdown_percent,
            'consecutive_losses': state.consecutive_losses,
            'open_positions_count': state.open_positions_count,
            'reasons': reasons,
            'block_reasons': block_reasons,
        }
