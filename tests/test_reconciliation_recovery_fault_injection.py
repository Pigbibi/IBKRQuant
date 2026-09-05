"""Fault injection for the non-executable IBKR legacy recovery path.

Each case simulates an untrusted or stale input at a trust boundary.  A test
passes only when the verifier rejects it or returns no transition plan; none of
these paths has a state-write or broker-order implementation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quant_platform_kit.common.broker_reconciliation import (
    calculate_broker_reconciliation_evidence_sha256,
)

from tests.test_reconciliation_recovery_verify_only import (
    _candidate_payload,
    _candidate_source_inputs,
    _console_response,
    _dual_review,
    _evidence,
    _runtime_target,
)
from scripts.verify_reconciliation_recovery import (
    read_console_confirmation,
    verify_reconciliation_recovery,
)


def _inputs(start: datetime) -> tuple[dict[str, object], str]:
    candidate = _candidate_payload(start)
    candidate_value = candidate["candidate"]
    assert isinstance(candidate_value, dict)
    return candidate, str(candidate_value["candidate_sha256"])


def test_fault_injection_tampered_console_policy_is_rejected_before_evidence_evaluation() -> None:
    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    candidate, candidate_sha256 = _inputs(start)
    confirmation = _console_response(candidate_sha256, confirmed_at=start + timedelta(minutes=3))
    confirmation["policy"] = {
        "no_order": True,
        "execution_authority_granted": True,
        "controller_must_reverify": True,
    }

    with pytest.raises(ValueError, match="non-execution policy"):
        verify_reconciliation_recovery(
            candidate_payload=candidate,
            **_candidate_source_inputs(start),
            dual_review_payload=_dual_review(candidate_sha256),
            confirmation_payload=confirmation,
            current_receipt_payload={"evidence": _evidence(observed_at=start + timedelta(minutes=4)).to_dict()},
            runtime_target_payload=_runtime_target(),
            recovery_id="ibkr-soxl-live-recovery",
            now=start + timedelta(minutes=5),
        )


def test_fault_injection_current_cash_digest_drift_closes_transition_plan() -> None:
    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    candidate, candidate_sha256 = _inputs(start)
    current = _evidence(observed_at=start + timedelta(minutes=4)).to_dict()
    current["cash_sha256"] = "9" * 64
    current["evidence_sha256"] = calculate_broker_reconciliation_evidence_sha256(current)

    result = verify_reconciliation_recovery(
        candidate_payload=candidate,
        **_candidate_source_inputs(start),
        dual_review_payload=_dual_review(candidate_sha256),
        confirmation_payload=_console_response(candidate_sha256, confirmed_at=start + timedelta(minutes=3)),
        current_receipt_payload={"evidence": current},
        runtime_target_payload=_runtime_target(),
        recovery_id="ibkr-soxl-live-recovery",
        now=start + timedelta(minutes=5),
    )

    assert result["ready_for_atomic_state_transition"] is False
    assert result["transition_plan"] is None
    assert "broker_reconciliation_cash_mismatch" in result["findings"]
    assert result["policy"]["state_write_attempted"] is False  # type: ignore[index]


def test_fault_injection_stale_human_confirmation_closes_transition_plan() -> None:
    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    candidate, candidate_sha256 = _inputs(start)

    result = verify_reconciliation_recovery(
        candidate_payload=candidate,
        **_candidate_source_inputs(start),
        dual_review_payload=_dual_review(candidate_sha256),
        confirmation_payload=_console_response(candidate_sha256, confirmed_at=start + timedelta(minutes=3)),
        current_receipt_payload={"evidence": _evidence(observed_at=start + timedelta(minutes=40)).to_dict()},
        runtime_target_payload=_runtime_target(),
        recovery_id="ibkr-soxl-live-recovery",
        now=start + timedelta(minutes=40),
    )

    assert result["ready_for_atomic_state_transition"] is False
    assert result["transition_plan"] is None
    assert "reconciliation_recovery_confirmation_stale" in result["findings"]


def test_fault_injection_controller_read_url_cannot_be_a_general_console_endpoint() -> None:
    with pytest.raises(ValueError, match="controller read endpoint"):
        read_console_confirmation(
            confirmation_url="https://console.example/api/reconciliation-recovery",
            recovery_id="ibkr-soxl-live-recovery",
            token="controller-token",
        )
