from __future__ import annotations

from types import SimpleNamespace

import pytest

from application.broker_reconciliation import (
    IBKRReconciliationObservations,
    IBKRReconciliationReadError,
    build_ibkr_order_key,
    build_reconciliation_candidate,
    calculate_legacy_reconciliation_observation_sha256,
    collect_read_only_reconciliation_observations,
)
from quant_platform_kit.common.broker_reconciliation import calculate_broker_observation_sha256
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
            clientId=7,
            orderId=456,
            permId=9001,
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
            clientId=7,
            execId="exec-1",
            orderId=456,
            permId=9001,
            cumQty=1,
            avgPrice=21.5,
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
    assert observations.open_orders[0]["order_key"] == observations.recent_executions[0]["order_key"]
    assert observations.open_orders[0]["order_identity"] == {
        "account_scope_sha256": "64a7152bdd91f6f345d04a789b802724d369ead611ed2f7252c27507a74b8fd1",
        "client_id": "7",
        "order_id": "456",
        "perm_id": "9001",
    }
    assert observations.open_orders[0]["cumulative_filled_quantity"] == 0.0
    assert observations.recent_executions[0]["cumulative_filled_quantity"] == 1.0


def test_api_order_key_is_stable_when_perm_id_arrives_later() -> None:
    submitted_key = build_ibkr_order_key(account_id="U123", client_id=0, order_id=456)
    reconciled_key = build_ibkr_order_key(
        account_id="U123",
        client_id=0,
        order_id=456,
        perm_id=9001,
    )

    assert submitted_key == reconciled_key
    assert submitted_key != build_ibkr_order_key(account_id="U123", client_id=1, order_id=456)


def test_manual_order_key_requires_perm_id() -> None:
    assert build_ibkr_order_key(account_id="U123", order_id=0, perm_id=9001).startswith(
        "ibkr-order-v1-"
    )
    with pytest.raises(ValueError, match="client_id/order_id or perm_id"):
        build_ibkr_order_key(account_id="U123", order_id=0)


def test_order_event_metadata_does_not_change_legacy_reconciliation_digest() -> None:
    legacy_open_order = {
        "account": "U123",
        "contract": {"symbol": "SOXL"},
        "perm_id": "9001",
        "action": "BUY",
        "order_type": "LMT",
        "total_quantity": 2.0,
        "limit_price": 22.0,
        "aux_price": 0.0,
        "status": "Submitted",
        "filled": 0.0,
        "remaining": 2.0,
    }
    enriched_open_order = {
        **legacy_open_order,
        "order_key": "ibkr-order-v1-example",
        "order_identity": {"account_scope_sha256": "digest", "client_id": "7", "order_id": "456"},
        "cumulative_filled_quantity": 0.0,
        "status_transitions": [{"from": "created", "to": "submitted"}],
    }

    assert calculate_legacy_reconciliation_observation_sha256((enriched_open_order,)) == (
        calculate_broker_observation_sha256((legacy_open_order,))
    )


def test_cash_reconciliation_ignores_dynamic_margin_and_valuation_tags() -> None:
    def fetch_snapshot(_ib, *, dynamic_net_liquidation: float, dynamic_available_funds: float, **_kwargs):
        return SimpleNamespace(
            positions=(),
            metadata={
                "cash_balances": (
                    {
                        "account_id": "U123",
                        "currency": "USD",
                        "CashBalance": 123.45,
                        "AvailableFunds": dynamic_available_funds,
                        "NetLiquidation": dynamic_net_liquidation,
                    },
                ),
                "option_positions": (),
            },
        )

    ib = _IB()
    first = collect_read_only_reconciliation_observations(
        ib,
        account_ids=("U123",),
        fetch_portfolio_snapshot=lambda *args, **kwargs: fetch_snapshot(
            *args,
            **kwargs,
            dynamic_net_liquidation=1_000.0,
            dynamic_available_funds=700.0,
        ),
        market_currency="USD",
        cash_only_execution=True,
    )
    second = collect_read_only_reconciliation_observations(
        ib,
        account_ids=("U123",),
        fetch_portfolio_snapshot=lambda *args, **kwargs: fetch_snapshot(
            *args,
            **kwargs,
            dynamic_net_liquidation=1_050.0,
            dynamic_available_funds=750.0,
        ),
        market_currency="USD",
        cash_only_execution=True,
    )

    assert first.cash == second.cash == (
        {"account": "U123", "currency": "USD", "tags": {"CashBalance": 123.45}},
    )


def test_cash_reconciliation_fails_closed_without_a_cash_balance_tag() -> None:
    with pytest.raises(IBKRReconciliationReadError, match="stable cash-balance tag"):
        collect_read_only_reconciliation_observations(
            _IB(),
            account_ids=("U123",),
            fetch_portfolio_snapshot=lambda *_args, **_kwargs: SimpleNamespace(
                positions=(),
                metadata={
                    "cash_balances": (
                        {
                            "account_id": "U123",
                            "currency": "USD",
                            "AvailableFunds": 700.0,
                            "NetLiquidation": 1_000.0,
                        },
                    ),
                    "option_positions": (),
                },
            ),
            market_currency="USD",
            cash_only_execution=True,
        )


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


def _frozen_runtime_target_from_minimal_json():
    """Mirror a valid legacy target JSON that omits derived execution fields."""

    payload = {
        "platform_id": "ibkr",
        "strategy_profile": "soxl_soxx_trend_income",
        "dry_run_only": False,
        "deployment_selector": "live",
        "account_selector": ["group"],
        "account_scope": "live",
        "service_name": "ibkr-live",
    }
    return build_runtime_target(
        **payload,
        live_continuity={
            "state": "RECONCILE_ONLY",
            "baseline_kind": "legacy_authorized",
            "baseline_id": "ibkr-soxl-lkg-20260830",
            "baseline_target_sha256": runtime_target_fingerprint(payload),
            "captured_at": "2026-08-30",
        },
        continuity_fingerprint_payload=payload,
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


def test_candidate_accepts_startup_validated_legacy_json_baseline(tmp_path) -> None:
    candidate = build_reconciliation_candidate(
        observations=_observations(),
        runtime_target=_frozen_runtime_target_from_minimal_json(),
        platform_id="ibkr",
        strategy_profile="soxl_soxx_trend_income",
        account_group="LIVE",
        project_id=None,
        env_reader=lambda name, default=None: (
            str(tmp_path) if name == "IBKR_EXECUTION_STATE_DIR" else default
        ),
    )

    assert "broker_reconciliation_baseline_target_mismatch" not in {
        finding.value for finding in candidate.recovery_blockers
    }
    assert candidate.permits_active_lkg is False


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


def test_candidate_keeps_legacy_order_digests_after_order_event_wiring(tmp_path) -> None:
    target = _frozen_runtime_target()

    def empty_env(name, default=None):
        return str(tmp_path) if name == "IBKR_EXECUTION_STATE_DIR" else default

    legacy_observations = IBKRReconciliationObservations(
        account_scope={"account_ids": ["U123"]},
        account_identity_match=True,
        positions=(),
        cash=(),
        open_orders=(
            {
                "account": "U123",
                "contract": {"symbol": "SOXL"},
                "perm_id": "9001",
                "status": "Submitted",
                "filled": 0.0,
                "remaining": 2.0,
            },
        ),
        recent_executions=(
            {
                "account": "U123",
                "contract": {"symbol": "SOXL"},
                "order_id": "456",
                "execution_id": "exec-1",
                "shares": 1.0,
                "price": 21.5,
            },
        ),
    )
    seed = build_reconciliation_candidate(
        observations=legacy_observations,
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
    enriched_observations = IBKRReconciliationObservations(
        account_scope=legacy_observations.account_scope,
        account_identity_match=legacy_observations.account_identity_match,
        positions=legacy_observations.positions,
        cash=legacy_observations.cash,
        open_orders=(
            {
                **legacy_observations.open_orders[0],
                "order_key": "ibkr-order-v1-example",
                "order_identity": {"account_scope_sha256": "digest", "client_id": "7", "order_id": "456"},
                "cumulative_filled_quantity": 0.0,
            },
        ),
        recent_executions=(
            {
                **legacy_observations.recent_executions[0],
                "order_key": "ibkr-order-v1-example",
                "order_identity": {"account_scope_sha256": "digest", "client_id": "7", "order_id": "456"},
                "cumulative_filled_quantity": 1.0,
            },
        ),
    )

    def configured_env(name, default=None):
        if name == "IBKR_EXECUTION_STATE_DIR":
            return str(tmp_path)
        if name == "IBKR_RECONCILIATION_EXPECTED_DIGESTS_JSON":
            import json

            return json.dumps(expected)
        return default

    candidate = build_reconciliation_candidate(
        observations=enriched_observations,
        runtime_target=target,
        platform_id="ibkr",
        strategy_profile="soxl_soxx_trend_income",
        account_group="LIVE",
        project_id=None,
        env_reader=configured_env,
    )

    assert candidate.permits_active_lkg is True
