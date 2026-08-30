from __future__ import annotations

from datetime import datetime, timezone

import pytest

from quant_platform_kit.common.reconciliation_recovery import ReconciliationRecoveryTransitionPlan

from scripts.publish_reconciliation_recovery_state_ledger import archive_recovery_state_ledger
from scripts.reconciliation_recovery_state_ledger import (
    RECOVERY_STATE_LEDGER_SCHEMA_VERSION,
    build_recovery_state_ledger,
)


def _digest(character: str) -> str:
    return character * 64


def _verification() -> dict[str, object]:
    plan = ReconciliationRecoveryTransitionPlan(
        recovery_id="ibkr-soxl-live-recovery",
        candidate_sha256=_digest("a"),
        confirmation_sha256=_digest("b"),
        baseline_id="soxl-ibkr-lkg-20260830",
        baseline_target_sha256=_digest("c"),
        expected_digests={
            "positions_sha256": _digest("d"),
            "cash_sha256": _digest("e"),
            "open_orders_sha256": _digest("f"),
            "recent_executions_sha256": _digest("0"),
            "local_execution_ledger_sha256": _digest("1"),
        },
        verified_at=datetime(2026, 8, 31, 1, 5, tzinfo=timezone.utc),
    )
    return {
        "schema_version": "ibkr_reconciliation_recovery_verification.v1",
        "recovery_id": plan.recovery_id,
        "candidate_sha256": plan.candidate_sha256,
        "confirmation_sha256": plan.confirmation_sha256,
        "ready_for_atomic_state_transition": True,
        "findings": [],
        "transition_plan": plan.to_dict(),
        "policy": {
            "controller_mode": "verify_only",
            "no_order": True,
            "execution_authority_granted": False,
            "state_write_attempted": False,
        },
    }


def test_build_state_ledger_requires_a_complete_non_executable_verification() -> None:
    ledger = build_recovery_state_ledger(
        verification=_verification(),
        service_name="interactive-brokers-live-service",
    )

    assert ledger["schema_version"] == RECOVERY_STATE_LEDGER_SCHEMA_VERSION
    assert ledger["service_name"] == "interactive-brokers-live-service"
    assert set(ledger) == {"schema_version", "recovery_id", "service_name", "transition_plan"}

    invalid = _verification()
    invalid["ready_for_atomic_state_transition"] = False
    with pytest.raises(ValueError, match="not ready"):
        build_recovery_state_ledger(
            verification=invalid,
            service_name="interactive-brokers-live-service",
        )


def test_archive_state_ledger_uses_create_only_private_state_prefix() -> None:
    ledger = build_recovery_state_ledger(
        verification=_verification(),
        service_name="interactive-brokers-live-service",
    )
    received: dict[str, object] = {}

    class Blob:
        def upload_from_string(self, payload: str, **kwargs: object) -> None:
            received["payload"] = payload
            received["kwargs"] = kwargs

    class Bucket:
        @staticmethod
        def blob(name: str) -> Blob:
            received["object_name"] = name
            return Blob()

    class Client:
        @staticmethod
        def bucket(name: str) -> Bucket:
            received["bucket_name"] = name
            return Bucket()

    archive = archive_recovery_state_ledger(
        ledger,
        state_ledger_uri="gs://private-bucket/reconciliation-recovery/ibkr/state/recovery-1.json",
        storage_client_factory=Client,
    )

    assert archive["uri"] == "gs://private-bucket/reconciliation-recovery/ibkr/state/recovery-1.json"
    assert received["bucket_name"] == "private-bucket"
    assert received["object_name"] == "reconciliation-recovery/ibkr/state/recovery-1.json"
    assert received["kwargs"] == {
        "content_type": "application/json",
        "if_generation_match": 0,
    }

    with pytest.raises(ValueError, match="state prefix"):
        archive_recovery_state_ledger(
            ledger,
            state_ledger_uri="gs://private-bucket/reconciliation-recovery/ibkr/source/recovery-1.json",
            storage_client_factory=Client,
        )
