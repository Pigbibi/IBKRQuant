from __future__ import annotations

from types import SimpleNamespace

import pytest

from application.broker_reconciliation import (
    IBKRReconciliationObservations,
    IBKRReconciliationReadError,
    build_reconciliation_candidate,
    collect_read_only_reconciliation_observations,
)
from quant_platform_kit.common.live_continuity import runtime_target_fingerprint
from quant_platform_kit.common.runtime_target import build_runtime_target


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


def _frozen_runtime_target():
    base_target = build_runtime_target(
        platform_id="ibkr",
        strategy_profile="soxl_soxx_trend_income",
        dry_run_only=False,
        deployment_selector="live",
        account_selector=("group",),
        account_scope="live",
        service_name="ibkr-live",
    )
    payload = base_target.to_dict()
    payload.pop("execution_mode")
    return build_runtime_target(
        **payload,
        live_continuity={
            "state": "RECONCILE_ONLY",
            "baseline_kind": "legacy_authorized",
            "baseline_id": "ibkr-soxl-lkg-20260830",
            "baseline_target_sha256": runtime_target_fingerprint(base_target.to_dict()),
            "captured_at": "2026-08-30",
        },
    )


def _observations() -> IBKRReconciliationObservations:
    return IBKRReconciliationObservations(
        account_scope={"account_ids": ["U123"]},
        account_identity_match=True,
        positions=({"symbol": "SOXL", "quantity": 10.0},),
        cash=({"currency": "USD", "tags": {"CashBalance": 123.45}},),
        open_orders=(),
        recent_executions=(),
    )


def test_candidate_stays_frozen_without_private_expected_digests(tmp_path) -> None:
    candidate = build_reconciliation_candidate(
        observations=_observations(),
        runtime_target=_frozen_runtime_target(),
        platform_id="ibkr",
        strategy_profile="soxl_soxx_trend_income",
        account_group="LIVE",
        project_id=None,
        env_reader=lambda name, default=None: (
            str(tmp_path) if name == "IBKR_EXECUTION_STATE_DIR" else default
        ),
    )

    assert candidate.permits_active_lkg is False
    assert candidate.expected_digests_configured is False
    assert set(candidate.to_safe_dict()) == {
        "schema_version",
        "permits_active_lkg",
        "expected_digests_configured",
        "execution_ledger_records_count",
        "recovery_blockers",
        "evidence",
    }
    assert candidate.to_safe_dict()["evidence"]["positions_sha256"]


def test_candidate_can_only_pass_with_all_matching_private_digests(tmp_path) -> None:
    target = _frozen_runtime_target()

    def empty_env(name, default=None):
        return str(tmp_path) if name == "IBKR_EXECUTION_STATE_DIR" else default

    seed = build_reconciliation_candidate(
        observations=_observations(),
        runtime_target=target,
        platform_id="ibkr",
        strategy_profile="soxl_soxx_trend_income",
        account_group="LIVE",
        project_id=None,
        env_reader=empty_env,
    )
    expected = {
        key: seed.evidence.to_dict()[key]
        for key in (
            "positions_sha256",
            "cash_sha256",
            "open_orders_sha256",
            "recent_executions_sha256",
            "local_execution_ledger_sha256",
        )
    }

    def configured_env(name, default=None):
        if name == "IBKR_EXECUTION_STATE_DIR":
            return str(tmp_path)
        if name == "IBKR_RECONCILIATION_EXPECTED_DIGESTS_JSON":
            import json

            return json.dumps(expected)
        return default

    candidate = build_reconciliation_candidate(
        observations=_observations(),
        runtime_target=target,
        platform_id="ibkr",
        strategy_profile="soxl_soxx_trend_income",
        account_group="LIVE",
        project_id=None,
        env_reader=configured_env,
    )

    assert candidate.permits_active_lkg is True
    assert candidate.recovery_blockers == ()
