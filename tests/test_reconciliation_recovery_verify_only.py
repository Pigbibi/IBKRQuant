from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from quant_platform_kit.common.broker_reconciliation import build_broker_reconciliation_evidence
from quant_platform_kit.common.broker_reconciliation_enrollment import (
    evaluate_broker_reconciliation_baseline_enrollment,
)
from quant_platform_kit.common.reconciliation_recovery import (
    calculate_reconciliation_recovery_confirmation_sha256,
)

from scripts.verify_reconciliation_recovery import (
    read_console_confirmation,
    verify_reconciliation_recovery,
)


def _digest(character: str) -> str:
    return character * 64


def _evidence(*, observed_at: datetime) -> object:
    return build_broker_reconciliation_evidence(
        platform_id="ibkr",
        strategy_profile="soxl_soxx_trend_income",
        account_scope_sha256=_digest("a"),
        baseline_id="soxl-ibkr-lkg-20260830",
        baseline_target_sha256=_digest("b"),
        runtime_target_sha256=_digest("b"),
        observed_at=observed_at,
        broker_connected=True,
        account_identity_match=True,
        positions_match=True,
        cash_match=True,
        open_orders_match=True,
        recent_executions_match=True,
        local_execution_ledger_match=True,
        positions_sha256=_digest("c"),
        cash_sha256=_digest("d"),
        open_orders_sha256=_digest("e"),
        recent_executions_sha256=_digest("f"),
        local_execution_ledger_sha256=_digest("0"),
    )


def _candidate_payload(start: datetime) -> dict[str, object]:
    enrollment = evaluate_broker_reconciliation_baseline_enrollment(
        [_evidence(observed_at=start), _evidence(observed_at=start + timedelta(minutes=2))],
        now=start + timedelta(minutes=3),
    )
    assert enrollment.candidate is not None
    return {
        "schema_version": "ibkr_reconciliation_baseline_enrollment.v1",
        "ready_for_independent_review": True,
        "findings": [],
        "candidate": enrollment.candidate.to_dict(),
    }


def _dual_review(candidate_sha256: str) -> dict[str, object]:
    return {
        "trigger": "reconciliation_baseline",
        "strategy_profile": "soxl_soxx_trend_income",
        "primary_review": {"verdict": "approve", "confidence": 0.99},
        "secondary_review": {
            "mode": "dual_api",
            "gpt": {"verdict": "approve", "confidence": 0.98},
            "claude": {"verdict": "approve", "confidence": 0.98},
        },
        "escalated": True,
        "outcome": "pass",
        "evidence_binding_sha256": candidate_sha256,
        "requires_human_recovery_approval": True,
        "recovery_authority": {"human_review_required": True, "final_action": "escalate"},
    }


def _confirmation(candidate_sha256: str, *, confirmed_at: datetime) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "qsl_reconciliation_recovery_confirmation.v1",
        "recovery_id": "ibkr-soxl-live-recovery",
        "candidate_sha256": candidate_sha256,
        "dual_review_binding_sha256": candidate_sha256,
        "confirmed_at": confirmed_at.isoformat().replace("+00:00", "Z"),
        "confirmed_by": "recovery-admin",
        "no_order": True,
        "execution_authority_granted": False,
        "confirmation_sha256": "0" * 64,
    }
    value["confirmation_sha256"] = calculate_reconciliation_recovery_confirmation_sha256(value)
    return value


def _console_response(candidate_sha256: str, *, confirmed_at: datetime) -> dict[str, object]:
    return {
        "ok": True,
        "schema_version": "qsl_reconciliation_recovery_controller_read.v1",
        "recovery": {
            "recovery_id": "ibkr-soxl-live-recovery",
            "platform": "ibkr",
            "strategy_profile": "soxl_soxx_trend_income",
            "environment": "live",
            "reconciliation_state": "RECONCILE_ONLY",
            "candidate_sha256": candidate_sha256,
            "dual_review_binding_sha256": candidate_sha256,
            "evidence_sample_count": 2,
            "first_observed_at": "2026-08-31T01:00:00Z",
            "last_observed_at": "2026-08-31T01:02:00Z",
        },
        "confirmation": _confirmation(candidate_sha256, confirmed_at=confirmed_at),
        "policy": {
            "no_order": True,
            "execution_authority_granted": False,
            "controller_must_reverify": True,
        },
    }


def _runtime_target(*, state: str = "RECONCILE_ONLY") -> dict[str, object]:
    return {
        "platform_id": "ibkr",
        "strategy_profile": "soxl_soxx_trend_income",
        "live_continuity": {
            "state": state,
            "baseline_id": "soxl-ibkr-lkg-20260830",
            "baseline_target_sha256": _digest("b"),
        },
    }


def test_verify_only_returns_a_plan_without_attempting_state_write() -> None:
    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    candidate = _candidate_payload(start)
    candidate_value = candidate["candidate"]
    assert isinstance(candidate_value, dict)
    candidate_sha256 = str(candidate_value["candidate_sha256"])

    result = verify_reconciliation_recovery(
        candidate_payload=candidate,
        dual_review_payload=_dual_review(candidate_sha256),
        confirmation_payload=_console_response(candidate_sha256, confirmed_at=start + timedelta(minutes=3)),
        current_receipt_payload={"evidence": _evidence(observed_at=start + timedelta(minutes=4)).to_dict()},
        runtime_target_payload=_runtime_target(),
        recovery_id="ibkr-soxl-live-recovery",
        now=start + timedelta(minutes=5),
    )

    assert result["ready_for_atomic_state_transition"] is True
    assert result["findings"] == []
    assert result["transition_plan"]["next_live_continuity_state"] == "ACTIVE_LKG"  # type: ignore[index]
    assert result["policy"] == {
        "controller_mode": "verify_only",
        "no_order": True,
        "execution_authority_granted": False,
        "state_write_attempted": False,
    }


def test_verify_only_keeps_plan_closed_when_deployed_target_is_not_reconcile_only() -> None:
    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    candidate = _candidate_payload(start)
    candidate_value = candidate["candidate"]
    assert isinstance(candidate_value, dict)
    candidate_sha256 = str(candidate_value["candidate_sha256"])

    result = verify_reconciliation_recovery(
        candidate_payload=candidate,
        dual_review_payload=_dual_review(candidate_sha256),
        confirmation_payload=_console_response(candidate_sha256, confirmed_at=start + timedelta(minutes=3)),
        current_receipt_payload={"evidence": _evidence(observed_at=start + timedelta(minutes=4)).to_dict()},
        runtime_target_payload=_runtime_target(state="PAUSED"),
        recovery_id="ibkr-soxl-live-recovery",
        now=start + timedelta(minutes=5),
    )

    assert result["ready_for_atomic_state_transition"] is False
    assert result["transition_plan"] is None
    assert "reconciliation_recovery_current_state_not_reconcile_only" in result["findings"]


def test_read_console_confirmation_uses_dedicated_bearer_header() -> None:
    received: dict[str, object] = {}

    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def read() -> bytes:
            return json.dumps({"ok": True}).encode("utf-8")

    def opener(request: object, **kwargs: object) -> FakeResponse:
        received["request"] = request
        received["kwargs"] = kwargs
        return FakeResponse()

    result = read_console_confirmation(
        confirmation_url="https://console.example/api/internal/reconciliation-recovery-confirmation",
        recovery_id="ibkr-soxl-live-recovery",
        token="controller-token",
        opener=opener,
    )

    assert result == {"ok": True}
    request = received["request"]
    assert getattr(request, "get_header")("Authorization") == "Bearer controller-token"
    assert "recovery_id=ibkr-soxl-live-recovery" in getattr(request, "full_url")
