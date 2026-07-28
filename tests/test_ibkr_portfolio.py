from types import SimpleNamespace

import pytest

from application import ibkr_portfolio
from application.ibkr_portfolio import fetch_portfolio_snapshot


class FakeIB:
    def __init__(self):
        self.req_positions_called = 0

    def reqPositions(self):
        self.req_positions_called += 1

    def positions(self):
        return [
            SimpleNamespace(
                account="UHK123",
                contract=SimpleNamespace(secType="STK", symbol="00700", currency="HKD"),
                position=100,
                avgCost=320.5,
            ),
            SimpleNamespace(
                account="UUS999",
                contract=SimpleNamespace(secType="STK", symbol="AAPL", currency="USD"),
                position=5,
                avgCost=190.0,
            ),
            SimpleNamespace(
                account="UHK123",
                contract=SimpleNamespace(
                    secType="OPT",
                    symbol="00700",
                    currency="HKD",
                    localSymbol="TCEHY 260619C00350000",
                    lastTradeDateOrContractMonth="20260619",
                    right="C",
                    strike=350.0,
                ),
                position=1,
                avgCost=12.0,
            ),
        ]

    def accountValues(self):
        return [
            SimpleNamespace(account="UHK123", currency="HKD", tag="NetLiquidation", value="100000"),
            SimpleNamespace(account="UHK123", currency="HKD", tag="AvailableFunds", value="80000"),
            SimpleNamespace(account="UHK123", currency="USD", tag="NetLiquidation", value="999"),
            SimpleNamespace(account="UUS999", currency="HKD", tag="NetLiquidation", value="123"),
        ]


def test_fetch_portfolio_snapshot_filters_account_and_market_currency():
    ib = FakeIB()

    snapshot = fetch_portfolio_snapshot(
        ib,
        account_ids=("UHK123",),
        wait_seconds=0,
        currency="HKD",
    )

    assert ib.req_positions_called == 1
    assert snapshot.total_equity == 32050.0
    assert snapshot.buying_power == 0.0
    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].symbol == "00700"
    assert snapshot.positions[0].currency == "HKD"
    assert snapshot.metadata["currency"] == "HKD"
    assert snapshot.metadata["account_ids"] == ("UHK123",)
    assert snapshot.metadata["option_positions"][0]["currency"] == "HKD"


def test_fetch_portfolio_snapshot_prefers_market_currency_cash_balance():
    class MultiCurrencyIB(FakeIB):
        def positions(self):
            return []

        def accountValues(self):
            return [
                SimpleNamespace(account="U16608560", currency="USD", tag="NetLiquidation", value="1130"),
                SimpleNamespace(account="U16608560", currency="USD", tag="AvailableFunds", value="885.99"),
                SimpleNamespace(account="U16608560", currency="USD", tag="CashBalance", value="477.10"),
                SimpleNamespace(account="U16608560", currency="HKD", tag="CashBalance", value="408.98"),
            ]

    snapshot = fetch_portfolio_snapshot(
        MultiCurrencyIB(),
        account_ids=("U16608560",),
        wait_seconds=0,
        currency="USD",
    )

    assert snapshot.total_equity == 477.10
    assert snapshot.buying_power == 477.10
    assert snapshot.metadata["market_currency_cash"] == 477.10
    assert snapshot.metadata["available_funds"] == 885.99


def test_fetch_portfolio_snapshot_supports_ibkr_ledger_cash_balance():
    class LedgerCashIB(FakeIB):
        def positions(self):
            return []

        def accountValues(self):
            return [
                SimpleNamespace(account="U16608560", currency="USD", tag="NetLiquidation", value="1130"),
                SimpleNamespace(account="U16608560", currency="USD", tag="AvailableFunds", value="885.99"),
                SimpleNamespace(
                    account="U16608560",
                    currency="USD",
                    tag="$LEDGER-CashBalance",
                    value="477.10",
                ),
                SimpleNamespace(account="U16608560", currency="USD", tag="TotalCashValue", value="500.00"),
            ]

    snapshot = fetch_portfolio_snapshot(
        LedgerCashIB(),
        account_ids=("U16608560",),
        wait_seconds=0,
        currency="USD",
    )

    assert snapshot.total_equity == 477.10
    assert snapshot.buying_power == 477.10
    assert snapshot.metadata["market_currency_cash"] == 477.10


def test_fetch_portfolio_snapshot_rejects_total_cash_value_as_currency_cash():
    class AggregateCashIB(FakeIB):
        def positions(self):
            return []

        def accountValues(self):
            return [
                SimpleNamespace(account="U16608560", currency="USD", tag="NetLiquidation", value="1130"),
                SimpleNamespace(account="U16608560", currency="USD", tag="TotalCashValue", value="500.00"),
            ]

    with pytest.raises(
        ibkr_portfolio.IBKRPortfolioSnapshotUnavailableError,
        match="cash balance",
    ):
        fetch_portfolio_snapshot(
            AggregateCashIB(),
            account_ids=("U16608560",),
            wait_seconds=0,
            currency="USD",
        )


def test_fetch_portfolio_snapshot_allows_negative_cash_balance():
    class NegativeCashIB(FakeIB):
        def positions(self):
            return []

        def accountValues(self):
            return [
                SimpleNamespace(account="U16608560", currency="USD", tag="NetLiquidation", value="2160"),
                SimpleNamespace(account="U16608560", currency="USD", tag="AvailableFunds", value="1588.89"),
                SimpleNamespace(account="U16608560", currency="USD", tag="CashBalance", value="-284.0"),
            ]

    snapshot = fetch_portfolio_snapshot(
        NegativeCashIB(),
        account_ids=("U16608560",),
        wait_seconds=0,
        currency="USD",
    )

    assert snapshot.buying_power == -284.0
    assert snapshot.metadata["market_currency_cash"] == -284.0


def test_fetch_portfolio_snapshot_rejects_incomplete_cash_only_account_data():
    class MissingCashIB(FakeIB):
        def positions(self):
            return []

        def accountValues(self):
            return [
                SimpleNamespace(
                    account="U16608560",
                    currency="USD",
                    tag="NetLiquidation",
                    value="1130",
                )
            ]

    with pytest.raises(
        ibkr_portfolio.IBKRPortfolioSnapshotUnavailableError,
        match="cash balance",
    ):
        fetch_portfolio_snapshot(
            MissingCashIB(),
            account_ids=("U16608560",),
            wait_seconds=0,
            currency="USD",
        )


def test_fetch_portfolio_snapshot_allows_explicit_zero_cash_balance():
    class ZeroCashIB(FakeIB):
        def positions(self):
            return []

        def accountValues(self):
            return [
                SimpleNamespace(
                    account="U16608560",
                    currency="USD",
                    tag="CashBalance",
                    value="0",
                )
            ]

    snapshot = fetch_portfolio_snapshot(
        ZeroCashIB(),
        account_ids=("U16608560",),
        wait_seconds=0,
        currency="USD",
    )

    assert snapshot.total_equity == 0.0
    assert snapshot.metadata["market_currency_cash"] == 0.0
