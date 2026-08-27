from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from application.ibkr_portfolio import (
    IBKRPortfolioSnapshotUnavailableError,
    fetch_reconciled_paper_portfolio_snapshot,
)
from application.paper_execution_command_consumer import (
    IBKR_PAPER_EXECUTION_INTENT_SCHEMA_VERSION,
    consume_due_paper_execution_commands,
    resolve_paper_execution_command_consumer_enabled,
)
from quant_platform_kit.common.execution_commands import ExecutionCommand, ExecutionCommandState, ExecutionCommandStore
from quant_platform_kit.common.models import PortfolioSnapshot, Position, QuoteSnapshot
from quant_platform_kit.common.paper_execution_admission import (
    PAPER_RISK_ADMISSION_RECEIPT_INTENT_FIELD,
    build_paper_risk_admission_receipt,
)
from quant_platform_kit.common.strategy_release import build_runtime_loaded_receipt


def _release() -> dict[str, str]:
    return {
        "release_id": "soxl-p2-v3.20260824",
        "manifest_sha256": "a" * 64,
        "strategy_revision": "soxl-p2-v3",
        "config_sha256": "b" * 64,
        "risk_policy_sha256": "c" * 64,
        "evidence_sha256": "d" * 64,
        "plugin_bundle_sha256": "e" * 64,
        "effective_session": "2026-08-25",
    }


def _command(*, platform: str = "ibkr") -> ExecutionCommand:
    release = _release()
    intent = {
        "schema_version": IBKR_PAPER_EXECUTION_INTENT_SCHEMA_VERSION,
        "target_mode": "value",
        "targets": {"SOXL": 300.0, "BOXX": 100.0},
        "strategy_symbols": ["SOXL", "BOXX"],
        "strategy_release": release,
    }
    decision_digest = hashlib.sha256(
        json.dumps(intent, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    intent[PAPER_RISK_ADMISSION_RECEIPT_INTENT_FIELD] = build_paper_risk_admission_receipt(
        strategy_profile="soxl_soxx_trend_income",
        release_id=release["release_id"],
        risk_policy_sha256=release["risk_policy_sha256"],
        decision_digest=decision_digest,
        effective_session="2026-08-25",
        disposition="allow_new_risk",
        reason_codes=(),
    ).to_dict()
    return ExecutionCommand.from_decision(
        platform=platform,
        account_scope="paper",
        strategy_profile="soxl_soxx_trend_income",
        execution_mode="paper",
        signal_date="2026-08-24",
        effective_date="2026-08-25",
        execution_timing_contract="next_trading_day",
        decision_digest=decision_digest,
        intent=intent,
    )


def _portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        as_of=datetime(2026, 8, 25, tzinfo=timezone.utc),
        total_equity=1_000.0,
        cash_balance=800.0,
        buying_power=800.0,
        positions=(
            Position(
                symbol="SOXL",
                quantity=20.0,
                market_value=200.0,
                average_cost=8.0,
                currency="USD",
            ),
        ),
        metadata={"market_currency_cash": 800.0},
    )


def _quote(symbol: str) -> QuoteSnapshot:
    return QuoteSnapshot(
        symbol=symbol,
        as_of=datetime(2026, 8, 25, tzinfo=timezone.utc),
        last_price=10.0,
    )


def _binding() -> dict[str, str]:
    return {
        "platform": "ibkr",
        "account_scope": "paper",
        "strategy_profile": "soxl_soxx_trend_income",
    }


def test_consumer_fills_reconciled_paper_command_without_execution_adapter(tmp_path: Path) -> None:
    store = ExecutionCommandStore(local_dir=tmp_path)
    command = _command()
    assert store.enqueue(command)

    result = consume_due_paper_execution_commands(
        store=store,
        as_of_session="2026-08-25",
        claimant="ibkr-paper-command-consumer",
        portfolio_loader=_portfolio,
        quote_loader=_quote,
        managed_symbols=("SOXL", "BOXX"),
        runtime_release_receipt=build_runtime_loaded_receipt(strategy_release=_release()),
        expected_strategy_release=_release(),
        expected_command_binding=_binding(),
    )

    assert result["status"] == "ok"
    assert result["commands"][0]["status"] == "filled"
    assert store.current_state(command) is ExecutionCommandState.FILLED
    proposals = store.events(command)[1].details["proposals"]
    assert [proposal["exposure_effect"] for proposal in proposals] == ["increases", "increases"]
    assert all("order" not in proposal["details"] for proposal in proposals)


def test_consumer_rejects_cross_platform_command_before_portfolio_read(tmp_path: Path) -> None:
    store = ExecutionCommandStore(local_dir=tmp_path)
    command = _command(platform="schwab")
    assert store.enqueue(command)
    reads = {"portfolio": 0, "quote": 0}

    def portfolio_loader():
        reads["portfolio"] += 1
        return _portfolio()

    def quote_loader(symbol: str):
        reads["quote"] += 1
        return _quote(symbol)

    result = consume_due_paper_execution_commands(
        store=store,
        as_of_session="2026-08-25",
        claimant="ibkr-paper-command-consumer",
        portfolio_loader=portfolio_loader,
        quote_loader=quote_loader,
        managed_symbols=("SOXL", "BOXX"),
        runtime_release_receipt=build_runtime_loaded_receipt(strategy_release=_release()),
        expected_strategy_release=_release(),
        expected_command_binding=_binding(),
    )

    assert result["commands"][0]["status"] == "rejected"
    assert reads == {"portfolio": 0, "quote": 0}
    assert store.current_state(command) is ExecutionCommandState.REJECTED


def test_consumer_flag_cannot_be_enabled_outside_dry_run() -> None:
    with pytest.raises(RuntimeError, match="IBKR_DRY_RUN_ONLY=true"):
        resolve_paper_execution_command_consumer_enabled(
            env_reader=lambda *_args: "true",
            dry_run_only=False,
        )


def test_reconciled_snapshot_uses_current_market_value_not_cost_basis() -> None:
    class FakeIB:
        def portfolio(self, account: str):
            assert account == "DU123"
            return [
                SimpleNamespace(
                    account="DU123",
                    contract=SimpleNamespace(symbol="SOXL", currency="USD", conId=1),
                    position=20.0,
                    marketValue=200.0,
                    averageCost=8.0,
                )
            ]

        def accountValues(self):
            return [
                SimpleNamespace(
                    account="DU123",
                    currency="USD",
                    tag="CashBalance",
                    value="800",
                )
            ]

    snapshot = fetch_reconciled_paper_portfolio_snapshot(
        FakeIB(),
        account_ids=("DU123",),
        currency="USD",
    )

    assert snapshot.positions[0].market_value == 200.0
    assert snapshot.positions[0].average_cost == 8.0
    assert snapshot.total_equity == 1_000.0


def test_reconciled_snapshot_requires_current_market_value() -> None:
    class IncompleteIB:
        def portfolio(self, _account: str):
            return [
                SimpleNamespace(
                    account="DU123",
                    contract=SimpleNamespace(symbol="SOXL", currency="USD", conId=1),
                    position=20.0,
                    marketValue=None,
                    averageCost=8.0,
                )
            ]

        def accountValues(self):
            return [
                SimpleNamespace(
                    account="DU123",
                    currency="USD",
                    tag="CashBalance",
                    value="800",
                )
            ]

    with pytest.raises(IBKRPortfolioSnapshotUnavailableError, match="market values"):
        fetch_reconciled_paper_portfolio_snapshot(
            IncompleteIB(),
            account_ids=("DU123",),
            currency="USD",
        )
