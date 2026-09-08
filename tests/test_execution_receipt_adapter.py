from __future__ import annotations

import unittest
from copy import deepcopy

import pytest

from application.cycle_result import StrategyCycleResult
from application.execution_receipt_adapter import attach_cycle_execution_receipt


REVISION = "a" * 40


def _report() -> dict[str, object]:
    return {
        "platform": "interactive_brokers",
        "strategy_profile": "soxl_soxx_trend_income",
        "dry_run": False,
        "runtime_target": {"execution_mode": "live"},
        "runtime_release_receipt": {
            "attestation_state": "self_attested",
            "strategy_release": {"strategy_revision": REVISION},
        },
    }


class ExecutionReceiptAdapterTest(unittest.TestCase):
    def test_explicit_fill_is_the_only_fill_claim(self) -> None:
        report = _report()

        attach_cycle_execution_receipt(
            report,
            {"orders_filled": [{"symbol": "SOXL"}]},
            {},
            execution_failed=False,
        )

        self.assertEqual(report["execution_receipt"]["outcome"], "filled")

    def test_submission_is_not_reported_as_acknowledged_or_filled(self) -> None:
        report = _report()

        attach_cycle_execution_receipt(
            report,
            {"orders_submitted": [{"symbol": "SOXL"}]},
            {},
            execution_failed=False,
        )

        self.assertEqual(report["execution_receipt"]["outcome"], "submitted")
        self.assertEqual(report["execution_receipt"]["broker_confirmation"], "not_observed")

    def test_pending_order_requires_reconciliation(self) -> None:
        report = _report()

        attach_cycle_execution_receipt(
            report,
            {"orders_pending": [{"symbol": "SOXL"}]},
            {},
            execution_failed=False,
        )

        self.assertEqual(report["execution_receipt"]["outcome"], "reconciliation_required")

    def test_expected_block_is_risk_blocked(self) -> None:
        report = _report()

        attach_cycle_execution_receipt(
            report,
            {"execution_status": "blocked"},
            {},
            execution_failed=False,
        )

        self.assertEqual(report["execution_receipt"]["outcome"], "risk_blocked")


@pytest.mark.parametrize("revision", [None, "abc1234", "A" * 40])
def test_attested_invalid_revision_still_rejects_receipt(revision):
    report = _report()
    report["runtime_release_receipt"]["strategy_release"]["strategy_revision"] = revision

    with pytest.raises(ValueError, match="strategy_revision must be"):
        attach_cycle_execution_receipt(report, {}, {}, execution_failed=False)

    assert "execution_receipt" not in report


def test_missing_attestation_is_not_treated_as_legacy():
    report = _report()
    del report["runtime_release_receipt"]

    with pytest.raises(ValueError, match="strategy_revision must be"):
        attach_cycle_execution_receipt(report, {}, {}, execution_failed=False)

    assert "execution_receipt" not in report


@pytest.mark.parametrize("profile", ["soxl_soxx_trend_income", "tqqq_growth_income"])
@pytest.mark.parametrize("execution_status", ["executed", "blocked"])
def test_legacy_request_preserves_cycle_result_without_receipt(
    strategy_module_factory, monkeypatch, profile, execution_status
):
    module = strategy_module_factory(STRATEGY_PROFILE=profile, IBKR_DRY_RUN_ONLY="false")
    observed = {"cycles": 0, "notifications": []}
    submitted_count = 1 if execution_status == "executed" else 0
    result = "OK - executed" if submitted_count else "Blocked - no equity"

    def run_cycle(**_kwargs):
        observed["cycles"] += 1
        return StrategyCycleResult(
            result=result,
            execution_summary={
                "execution_status": execution_status,
                "orders_submitted": [{"symbol": "TEST"}] if submitted_count else [],
                **({"no_op_reason": "no_equity"} if not submitted_count else {}),
            },
            reconciliation_record_path="/tmp/offline-reconciliation.json",
        )

    def persist_report(report, **_kwargs):
        observed["report"] = deepcopy(report)
        return "/tmp/offline-runtime-report.json"

    monkeypatch.setattr(module, "is_market_open_now", lambda **_kwargs: True)
    monkeypatch.setattr(module, "load_strategy_plugin_signals", lambda: ((), None))
    monkeypatch.setattr(module, "run_strategy_core", run_cycle)
    monkeypatch.setattr(module, "persist_execution_report", persist_report)
    monkeypatch.setattr(
        module,
        "_publish_runtime_failure_notification",
        lambda **kwargs: observed["notifications"].append(type(kwargs["exc"]).__name__),
    )

    # Keep the real report builder, receipt adapter, and HTTP handler connected.
    response = module.app.test_client().post("/run")

    assert response.status_code == 200
    assert response.get_data(as_text=True) == result
    assert observed["cycles"] == 1
    assert observed["notifications"] == []
    report = observed["report"]
    assert report["runtime_release_receipt"]["attestation_state"] == "legacy_unattested"
    assert report["runtime_release_receipt"]["missing"] == ["strategy_release"]
    assert "execution_receipt" not in report
    assert report["summary"]["execution_status"] == execution_status
    assert report["summary"]["orders_submitted_count"] == submitted_count
    assert report["artifacts"]["reconciliation_record_path"] == "/tmp/offline-reconciliation.json"
    assert report["status"] == ("ok" if submitted_count else "error")
    if submitted_count:
        assert report["errors"] == []
    else:
        assert report["diagnostics"]["failure_category"] == "strategy_execution_blocked"
        assert report["errors"][0]["stage"] == "strategy_execution"
