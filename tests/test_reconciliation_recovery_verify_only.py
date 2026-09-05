from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from quant_platform_kit.common.broker_reconciliation import build_broker_reconciliation_evidence
from scripts.build_reconciliation_baseline_candidate import evaluate_receipts
from tests.test_reconciliation_baseline_candidate_script import _sources_for_evidences
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
    observations = [_evidence(observed_at=start), _evidence(observed_at=start + timedelta(minutes=2))]
    return evaluate_receipts(
        [{"evidence": item.to_dict()} for item in observations], now=start + timedelta(minutes=3),
        **_sources_for_evidences(observations),
    )


def _candidate_source_inputs(start):
    return _sources_for_evidences([_evidence(observed_at=start), _evidence(observed_at=start + timedelta(minutes=2))])



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
        **_candidate_source_inputs(start),
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
        **_candidate_source_inputs(start),
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


def test_verify_only_rejects_a_current_receipt_with_confirmation_second_timestamp() -> None:
    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    candidate = _candidate_payload(start)
    candidate_value = candidate["candidate"]
    assert isinstance(candidate_value, dict)
    candidate_sha256 = str(candidate_value["candidate_sha256"])
    confirmed_at = start + timedelta(minutes=3)

    result = verify_reconciliation_recovery(
        candidate_payload=candidate,
        **_candidate_source_inputs(start),
        dual_review_payload=_dual_review(candidate_sha256),
        confirmation_payload=_console_response(candidate_sha256, confirmed_at=confirmed_at),
        current_receipt_payload={"evidence": _evidence(observed_at=confirmed_at).to_dict()},
        runtime_target_payload=_runtime_target(),
        recovery_id="ibkr-soxl-live-recovery",
        now=start + timedelta(minutes=5),
    )

    assert result["ready_for_atomic_state_transition"] is False
    assert result["transition_plan"] is None
    assert "reconciliation_recovery_evidence_not_reobserved_after_confirmation" in result["findings"]


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


def test_single_source_three_cli_path_needs_no_model_review(tmp_path, monkeypatch, capsys):
    from dataclasses import asdict
    from scripts import build_reconciliation_baseline_candidate as builder
    from scripts import publish_reconciliation_recovery_source as publisher
    from scripts import verify_reconciliation_recovery as verifier

    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    evidence = _evidence(observed_at=start)
    sources = _sources_for_evidences([evidence])

    def save(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    source_args = [
        "--source-records", save("records.json", sources["source_receipt_records"]),
        "--source-expectations", save("expectations.json", [asdict(item) for item in sources["expectations"]]),
    ]
    assert builder.main([
        "--receipt", save("receipt.json", {"evidence": evidence.to_dict()}),
        "--now", start.isoformat(), *source_args,
    ]) == 0
    candidate = json.loads(capsys.readouterr().out)
    candidate_path = save("candidate.json", candidate)
    digest = candidate["candidate"]["candidate_sha256"]
    assert candidate["candidate"]["schema_version"] == "broker_reconciliation_baseline_candidate.v2"
    assert publisher.main([
        "--candidate", candidate_path, "--recovery-id", "ibkr-soxl-live-recovery",
        "--now", start.isoformat(), *source_args,
    ]) == 0
    snapshot = json.loads(capsys.readouterr().out)
    record = snapshot["recoveries"][0]
    assert record["readiness"] == "awaiting_human_confirmation"
    assert record["evidence_sample_count"] == 1
    assert record["dual_review"] == {"outcome": "unavailable", "reviewer_count": 0, "evidence_binding_sha256": digest}

    response = _console_response(digest, confirmed_at=start + timedelta(minutes=1))
    response["recovery"]["evidence_sample_count"] = 1
    monkeypatch.setattr(verifier, "read_console_confirmation", lambda **_kwargs: response)
    assert verifier.main([
        "--candidate", candidate_path, "--recovery-id", "ibkr-soxl-live-recovery",
        "--current-receipt", save("current.json", {"evidence": _evidence(observed_at=start + timedelta(minutes=2)).to_dict()}),
        "--runtime-target", save("runtime.json", _runtime_target()),
        "--confirmation-url", "https://console.example/api/internal/reconciliation-recovery-confirmation",
        "--now", (start + timedelta(minutes=3)).isoformat(), *source_args,
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ready_for_atomic_state_transition"]
    assert result["transition_plan"]["no_order"] is True
    assert result["transition_plan"]["execution_authority_granted"] is False
    assert result["transition_plan"]["requires_atomic_compare_and_set"] is True
    assert result["policy"]["state_write_attempted"] is False


def test_verifier_rejects_console_sample_count_not_matching_candidate():
    import pytest
    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    candidate = _candidate_payload(start)
    digest = candidate["candidate"]["candidate_sha256"]
    response = _console_response(digest, confirmed_at=start + timedelta(minutes=3))
    response["recovery"]["evidence_sample_count"] = 1
    with pytest.raises(ValueError, match="sample count"):
        verify_reconciliation_recovery(
            candidate_payload=candidate, **_candidate_source_inputs(start),
            confirmation_payload=response,
            current_receipt_payload={"evidence": _evidence(observed_at=start + timedelta(minutes=4)).to_dict()},
            runtime_target_payload=_runtime_target(), recovery_id="ibkr-soxl-live-recovery",
            now=start + timedelta(minutes=5),
        )
