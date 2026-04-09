"""
Convert a Signal into an Order.

This is the only place where Signal → Order translation happens.
Keeping it here means the runner stays a thin coordinator and the
translation logic is independently testable.
"""

from __future__ import annotations

from bot.core.models import ContractSide, Order, Side, Signal, SignalAction


def signal_to_order(signal: Signal) -> Order:
    """
    Translate a Signal into an Order ready for submission.

    SignalAction → (Side, ContractSide) mapping:
        BUY_YES  → BUY  YES
        BUY_NO   → BUY  NO
        SELL_YES → SELL YES
        SELL_NO  → SELL NO

    Raises ValueError for HOLD signals — callers should check
    signal.is_actionable() before calling this.
    """
    if not signal.is_actionable():
        raise ValueError(f"Cannot convert non-actionable signal: {signal.action}")

    action_map: dict[SignalAction, tuple[Side, ContractSide]] = {
        SignalAction.BUY_YES:  (Side.BUY,  ContractSide.YES),
        SignalAction.BUY_NO:   (Side.BUY,  ContractSide.NO),
        SignalAction.SELL_YES: (Side.SELL, ContractSide.YES),
        SignalAction.SELL_NO:  (Side.SELL, ContractSide.NO),
    }

    side, contract_side = action_map[signal.action]

    return Order(
        ticker=signal.ticker,
        side=side,
        contract_side=contract_side,
        quantity=signal.quantity,
        limit_price=signal.limit_price,
        signal_ref=signal.strategy_id,
    )
