from __future__ import annotations

import pytest

from application.paper_execution_admission import (
    PAPER_EXECUTION_COMMAND_SIGNAL_FIELD,
    evaluate_ibkr_paper_execution_admission,
    resolve_paper_execution_admission_enabled,
)
from quant_platform_kit.common.execution_commands import ExecutionCommand
from quant_platform_kit.common.paper_execution_admission import (
    PAPER_RISK_ADMISSION_RECEIPT_INTENT_FIELD,
    PaperRiskAdmissionDisposition,
    build_paper_risk_admission_receipt,
)
from quant_platform_kit.common.strategy_release import build_runtime_loaded_receipt


def _release_identity() -> dict[str, str]:
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


def _command(
    *,
    disposition: PaperRiskAdmissionDisposition = PaperRiskAdmissionDisposition.ALLOW_NEW_RISK,
):
    release = _release_identity()
    receipt = build_paper_risk_admission_receipt(
        strategy_profile="soxl_soxx_trend_income",
        release_id=release["release_id"],
        risk_policy_sha256=release["risk_policy_sha256"],
        decision_digest="f" * 64,
        effective_session="2026-08-25",
        disposition=disposition,
        reason_codes=() if disposition is PaperRiskAdmissionDisposition.ALLOW_NEW_RISK else ("DATA_STALE",),
    )
    return ExecutionCommand.from_decision(
        platform="ibkr",
        account_scope="paper",
        strategy_profile="soxl_soxx_trend_income",
        execution_mode="paper",
        signal_date="2026-08-24",
        effective_date="2026-08-25",
        execution_timing_contract="next_trading_day",
        decision_digest="f" * 64,
        intent={
            "strategy_release": release,
            PAPER_RISK_ADMISSION_RECEIPT_INTENT_FIELD: receipt.to_dict(),
        },
    )


def _evaluate(command: ExecutionCommand | None):
    release = _release_identity()
    metadata = {"effective_date": "2026-08-25"}
    if command is not None:
        metadata[PAPER_EXECUTION_COMMAND_SIGNAL_FIELD] = command.to_dict()
    return evaluate_ibkr_paper_execution_admission(
        signal_metadata=metadata,
        strategy_profile="soxl_soxx_trend_income",
        account_scope="paper",
        positions={"SOXL": {"quantity": 1.0}},
        prices={"SOXL": 100.0},
        target_market_values={"SOXL": 200.0},
        runtime_release_receipt=build_runtime_loaded_receipt(strategy_release=release),
        expected_strategy_release=release,
    )


def test_paper_admission_is_opt_in_and_rejects_non_paper_enablement():
    assert not resolve_paper_execution_admission_enabled(
        env_reader=lambda _name, _default: "",
        dry_run_only=False,
        execution_mode="paper",
    )
    assert resolve_paper_execution_admission_enabled(
        env_reader=lambda _name, _default: "true",
        dry_run_only=False,
        execution_mode="paper",
    )
    with pytest.raises(RuntimeError, match="execution_mode=paper"):
        resolve_paper_execution_admission_enabled(
            env_reader=lambda _name, _default: "true",
            dry_run_only=False,
            execution_mode="live",
        )


def test_paper_admission_blocks_missing_immutable_command_before_a_broker_write():
    observation = _evaluate(None)

    assert observation["broker_write_allowed"] is False
    assert observation["command_id"] is None
    receipt = observation["runtime_command_gate_receipts"][0]
    assert receipt["enforcement"] == "enforce"
    assert receipt["broker_write_allowed"] is False
    assert "paper_risk_admission_receipt_missing" in receipt["reasons"]


def test_paper_admission_persists_a_bound_risk_receipt_and_uses_quote_position_facts():
    command = _command()

    observation = _evaluate(command)

    assert observation["broker_write_allowed"] is True
    assert observation["command_id"] == command.command_id
    assert observation["risk_admission_receipt"]["receipt_sha256"] == observation[
        "risk_admission_receipt_sha256"
    ]
    assert observation["exposure_facts"] == [
        {
            "symbol": "SOXL",
            "position_quantity": 1.0,
            "quote_price": 100.0,
            "current_market_value": 100.0,
            "target_market_value": 200.0,
            "exposure_effect": "increases",
        }
    ]
    assert observation["runtime_command_gate_receipts"][0]["broker_write_allowed"] is True


def test_reducing_only_receipt_blocks_an_increase_from_reconciled_facts():
    command = _command(disposition=PaperRiskAdmissionDisposition.REDUCING_ONLY)

    observation = _evaluate(command)

    assert observation["risk_disposition"] == "reducing_only"
    assert observation["broker_write_allowed"] is False
    receipt = observation["runtime_command_gate_receipts"][0]
    assert receipt["mode"] == "reducing"
    assert receipt["exposure_effect"] == "increases"
    assert receipt["broker_write_allowed"] is False
