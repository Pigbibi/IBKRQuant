from __future__ import annotations

from types import SimpleNamespace

import pytest

from application.broker_reconciliation import (
    IBKRReconciliationReadError,
    collect_read_only_reconciliation_observations,
)


def _snapshot(*, account_id: str = "U123"):
    return SimpleNamespace(
        positions=(
            SimpleNamespace(symbol="SOXL", quantity=10.0, average_cost=24.5, currency="USD"),
        ),
        metadata={
            "cash_balances": (
                {
                    "account_id": account_id,
                    "currency": "USD",
                    "CashBalance": 123.45,
                },
            ),
            "option_positions": (),
        },
    )


def _trade(*, account_id: str = "U123"):
    return SimpleNamespace(
        order=SimpleNamespace(
            account=account_id,
            permId=456,
            action="BUY",
            orderType="LMT",
            totalQuantity=2,
            lmtPrice=22.0,
            auxPrice=0.0,
        ),
        orderStatus=SimpleNamespace(status="Submitted", filled=0, remaining=2),
        contract=SimpleNamespace(
            conId=789,
            symbol="SOXL",
            localSymbol="SOXL",
            secType="STK",
            currency="USD",
        ),
    )


def _fill(*, account_id: str = "U123"):
    return SimpleNamespace(
        execution=SimpleNamespace(
            acctNumber=account_id,
            execId="exec-1",
            orderId=456,
            time="20260830 13:30:00 UTC",
            side="BOT",
            shares=1,
            price=21.5,
        ),
        contract=SimpleNamespace(
            conId=789,
            symbol="SOXL",
            localSymbol="SOXL",
            secType="STK",
            currency="USD",
        ),
        trade=SimpleNamespace(order=SimpleNamespace(account=account_id)),
    )


class _IB:
    def __init__(self) -> None:
        self.open_orders_requested = 0

    def managedAccounts(self):
        return ["U123", "U999"]

    def reqAllOpenOrders(self):
        self.open_orders_requested += 1

    def openTrades(self):
        return [_trade(), _trade(account_id="U999")]

    def reqExecutions(self):
        return [_fill(), _fill(account_id="U999")]


def test_collects_scoped_read_only_broker_observations() -> None:
    ib = _IB()
    calls: list[dict[str, object]] = []

    def fetch_portfolio_snapshot(_ib, **kwargs):
        calls.append(kwargs)
        return _snapshot()

    observations = collect_read_only_reconciliation_observations(
        ib,
        account_ids=("U123",),
        fetch_portfolio_snapshot=fetch_portfolio_snapshot,
        market_currency="USD",
        cash_only_execution=True,
    )

    assert ib.open_orders_requested == 1
    assert calls == [{"account_ids": ("U123",), "currency": "USD", "cash_only_execution": True}]
    assert observations.account_scope == {"account_ids": ["U123"]}
    assert observations.account_identity_match is True
    assert len(observations.positions) == 1
    assert len(observations.cash) == 1
    assert len(observations.open_orders) == 1
    assert len(observations.recent_executions) == 1
    assert observations.open_orders[0]["account"] == "U123"


def test_missing_read_only_order_surface_fails_closed() -> None:
    class MissingOpenOrderReader(_IB):
        reqAllOpenOrders = None

    with pytest.raises(IBKRReconciliationReadError, match="all-open-orders"):
        collect_read_only_reconciliation_observations(
            MissingOpenOrderReader(),
            account_ids=("U123",),
            fetch_portfolio_snapshot=lambda *_args, **_kwargs: _snapshot(),
            market_currency="USD",
            cash_only_execution=True,
        )


def test_unscoped_active_order_fails_closed() -> None:
    class UnscopedOpenOrderReader(_IB):
        def openTrades(self):
            return [_trade(account_id="")]

    with pytest.raises(IBKRReconciliationReadError, match="unscoped open order"):
        collect_read_only_reconciliation_observations(
            UnscopedOpenOrderReader(),
            account_ids=("U123",),
            fetch_portfolio_snapshot=lambda *_args, **_kwargs: _snapshot(),
            market_currency="USD",
            cash_only_execution=True,
        )
