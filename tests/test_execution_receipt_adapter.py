from __future__ import annotations

import unittest

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
