"""Tests for IBKR account_new_risk_gate W1 wiring (fail-closed, no broker I/O)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest import mock

import pytest
from quant_platform_kit.common.models import OrderIntent
from quant_platform_kit.risk.account_new_risk_gate import (
    InjectedReconciliationSnapshot,
    NewRiskDisposition,
)

from application.account_new_risk_gate_support import (
    ACCOUNT_NEW_RISK_GATE_ENV,
    build_portfolio_from_account_values,
    evaluate_account_values_new_risk_admission,
    evaluate_cycle_new_risk_admission,
    evaluate_portfolio_new_risk_admission,
    is_account_new_risk_gate_enabled,
    set_cycle_snapshot,
)
from application.ibkr_order_execution import submit_order_intent


@pytest.fixture(autouse=True)
def _clear_cycle_snapshot():
    set_cycle_snapshot(None)
    yield
    set_cycle_snapshot(None)


def test_gate_enabled_by_default():
    with mock.patch.dict(os.environ, {}, clear=True):
        assert is_account_new_risk_gate_enabled() is True


def test_gate_disabled_when_env_zero():
    with mock.patch.dict(os.environ, {ACCOUNT_NEW_RISK_GATE_ENV: "0"}, clear=False):
        assert is_account_new_risk_gate_enabled() is False


def test_missing_equity_prohibits_new_risk():
    result = evaluate_account_values_new_risk_admission({"equity": 0.0})
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert "EQUITY_UNKNOWN_FAIL_CLOSED" in result.reason_codes
    assert result.live_authority_granted is False


def test_drawdown_brake_prohibits_new_risk():
    portfolio = {
        "total_equity": 80_000.0,
        "peak_equity_usd": 100_000.0,
    }
    result = evaluate_portfolio_new_risk_admission(portfolio)
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert result.live_authority_granted is False


def test_healthy_equity_allows_new_risk():
    portfolio = {
        "total_equity": 100_000.0,
        "peak_equity_usd": 100_000.0,
    }
    result = evaluate_portfolio_new_risk_admission(portfolio)
    assert result.disposition == NewRiskDisposition.ALLOW_NEW_RISK
    assert result.live_authority_granted is False


def test_build_portfolio_from_account_values_maps_equity():
    portfolio = build_portfolio_from_account_values(
        {"equity": 50_000.0},
        signal_metadata={"peak_equity_usd": 55_000.0},
    )
    assert portfolio["total_equity"] == 50_000.0
    assert portfolio["peak_equity_usd"] == 55_000.0


def test_submit_order_intent_rejects_buy_when_gate_prohibits():
    set_cycle_snapshot(
        InjectedReconciliationSnapshot(
            observation_status="COMPLETE",
            reconciliation_status="VERIFIED",
            circuit_breaker_state="CLOSED",
            equity_usd=None,
        )
    )
    ib = SimpleNamespace()
    with mock.patch(
        "application.ibkr_order_execution._submit_order_intent",
    ) as submit_mock:
        report = submit_order_intent(
            ib,
            OrderIntent(symbol="SPY", side="buy", quantity=1.0),
        )
    submit_mock.assert_not_called()
    assert report.status == "rejected"
    assert report.raw_payload.get("detail") == "account_new_risk_gate"
    assert "EQUITY_UNKNOWN_FAIL_CLOSED" in report.raw_payload.get("reason_codes", [])


def test_submit_order_intent_allows_sell_when_gate_prohibits():
    set_cycle_snapshot(
        InjectedReconciliationSnapshot(
            observation_status="COMPLETE",
            reconciliation_status="VERIFIED",
            circuit_breaker_state="CLOSED",
            equity_usd=None,
        )
    )
    ib = SimpleNamespace()
    expected = SimpleNamespace(status="Submitted")
    with mock.patch(
        "application.ibkr_order_execution._submit_order_intent",
        return_value=expected,
    ) as submit_mock:
        submit_order_intent(
            ib,
            OrderIntent(symbol="SPY", side="sell", quantity=1.0),
        )
    submit_mock.assert_called_once()


def test_submit_order_intent_skips_gate_when_disabled():
    set_cycle_snapshot(
        InjectedReconciliationSnapshot(
            observation_status="COMPLETE",
            reconciliation_status="VERIFIED",
            circuit_breaker_state="CLOSED",
            equity_usd=None,
        )
    )
    ib = SimpleNamespace()
    expected = SimpleNamespace(status="Submitted")
    with mock.patch.dict(os.environ, {ACCOUNT_NEW_RISK_GATE_ENV: "0"}, clear=False):
        with mock.patch(
            "application.ibkr_order_execution._submit_order_intent",
            return_value=expected,
        ) as submit_mock:
            submit_order_intent(
                ib,
                OrderIntent(symbol="SPY", side="buy", quantity=1.0),
            )
    submit_mock.assert_called_once()


def test_cycle_gate_without_snapshot_is_fail_closed():
    result = evaluate_cycle_new_risk_admission()
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert "EQUITY_UNKNOWN_FAIL_CLOSED" in result.reason_codes
