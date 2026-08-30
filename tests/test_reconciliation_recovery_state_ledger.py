from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from quant_platform_kit.common.live_continuity import runtime_target_fingerprint
from quant_platform_kit.common.reconciliation_recovery import ReconciliationRecoveryTransitionPlan

from scripts.reconciliation_recovery_state_ledger import (
    RECOVERY_STATE_LEDGER_PATH_ENV,
    RECOVERY_STATE_LEDGER_SCHEMA_VERSION,
    apply_recovery_state_ledger,
    apply_recovery_state_ledger_from_env,
)


SYNC_PLAN_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_cloud_run_env_sync_plan.py"


def _digest(character: str) -> str:
    return character * 64


def _runtime_target() -> dict[str, object]:
    target: dict[str, object] = {
        "platform_id": "ibkr",
        "strategy_profile": "soxl_soxx_trend_income",
        "execution_mode": "live",
        "dry_run_only": False,
        "deployment_selector": "live",
        "account_scope": "live",
        "service_name": "interactive-brokers-live-service",
    }
    target["live_continuity"] = {
        "state": "RECONCILE_ONLY",
        "baseline_kind": "legacy_authorized",
        "baseline_id": "soxl-ibkr-lkg-20260830",
        "baseline_target_sha256": runtime_target_fingerprint(target),
        "captured_at": "2026-08-30",
    }
    return target


def _ledger(target: dict[str, object]) -> dict[str, object]:
    continuity = target["live_continuity"]
    assert isinstance(continuity, dict)
    plan = ReconciliationRecoveryTransitionPlan(
        recovery_id="ibkr-soxl-live-recovery",
        candidate_sha256=_digest("a"),
        confirmation_sha256=_digest("b"),
        baseline_id=str(continuity["baseline_id"]),
        baseline_target_sha256=str(continuity["baseline_target_sha256"]),
        expected_digests={
            "positions_sha256": _digest("c"),
            "cash_sha256": _digest("d"),
            "open_orders_sha256": _digest("e"),
            "recent_executions_sha256": _digest("f"),
            "local_execution_ledger_sha256": _digest("0"),
        },
        verified_at=datetime(2026, 8, 31, 1, 5, tzinfo=timezone.utc),
    )
    return {
        "schema_version": RECOVERY_STATE_LEDGER_SCHEMA_VERSION,
        "recovery_id": plan.recovery_id,
        "service_name": str(target["service_name"]),
        "transition_plan": plan.to_dict(),
    }


def test_state_ledger_changes_only_continuity_state_and_returns_exact_digests() -> None:
    target = _runtime_target()
    updated, expected_digests = apply_recovery_state_ledger(
        runtime_target=target,
        ledger=_ledger(target),
    )

    assert target["live_continuity"] != updated["live_continuity"]
    assert updated["live_continuity"] == {
        **target["live_continuity"],  # type: ignore[dict-item]
        "state": "ACTIVE_LKG",
    }
    assert runtime_target_fingerprint(updated) == runtime_target_fingerprint(target)
    assert expected_digests == _ledger(target)["transition_plan"]["expected_digests"]  # type: ignore[index]


def test_state_ledger_rejects_drifted_or_replayed_target() -> None:
    target = _runtime_target()
    ledger = _ledger(target)

    drifted = copy.deepcopy(target)
    drifted["strategy_profile"] = "tqqq_growth_income"
    with pytest.raises(ValueError, match="does not match"):
        apply_recovery_state_ledger(runtime_target=drifted, ledger=ledger)

    replayed = copy.deepcopy(target)
    continuity = replayed["live_continuity"]
    assert isinstance(continuity, dict)
    continuity["state"] = "ACTIVE_LKG"
    with pytest.raises(ValueError, match="current continuity state mismatch"):
        apply_recovery_state_ledger(runtime_target=replayed, ledger=ledger)


def test_state_ledger_path_is_opt_in_and_rejects_invalid_content(tmp_path) -> None:
    target = _runtime_target()
    unchanged, expected_digests = apply_recovery_state_ledger_from_env(runtime_target=target, env={})
    assert unchanged == target
    assert expected_digests is None

    path = tmp_path / "recovery-ledger.json"
    path.write_text(json.dumps(_ledger(target)), encoding="utf-8")
    updated, expected_digests = apply_recovery_state_ledger_from_env(
        runtime_target=target,
        env={RECOVERY_STATE_LEDGER_PATH_ENV: str(path)},
    )
    assert updated["live_continuity"]["state"] == "ACTIVE_LKG"  # type: ignore[index]
    assert set(expected_digests or {}) == {
        "positions_sha256",
        "cash_sha256",
        "open_orders_sha256",
        "recent_executions_sha256",
        "local_execution_ledger_sha256",
    }

    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        apply_recovery_state_ledger_from_env(
            runtime_target=target,
            env={RECOVERY_STATE_LEDGER_PATH_ENV: str(path)},
        )


def test_sync_plan_uses_opt_in_ledger_for_state_and_digest_env(tmp_path) -> None:
    target = _runtime_target()
    target["strategy_profile"] = "tqqq_growth_income"
    continuity = target["live_continuity"]
    assert isinstance(continuity, dict)
    target_without_continuity = dict(target)
    target_without_continuity.pop("live_continuity")
    continuity["baseline_target_sha256"] = runtime_target_fingerprint(target_without_continuity)
    ledger_path = tmp_path / "recovery-ledger.json"
    ledger_path.write_text(json.dumps(_ledger(target)), encoding="utf-8")

    payload = {
        "defaults": {
            "GLOBAL_TELEGRAM_CHAT_ID": "5992562050",
            "NOTIFY_LANG": "zh",
            "IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME": "ibkr-account-groups",
        },
        "targets": [{
            "service": "interactive-brokers-live-service",
            "account_group": "live",
            "runtime_target": target,
        }],
    }
    result = subprocess.run(
        [sys.executable, str(SYNC_PLAN_SCRIPT), "--json"],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CLOUD_RUN_SERVICE_TARGETS_JSON": json.dumps(payload),
            RECOVERY_STATE_LEDGER_PATH_ENV: str(ledger_path),
        },
    )

    plan = json.loads(result.stdout)
    env_values = plan["targets"][0]["env"]
    resolved_target = json.loads(env_values["RUNTIME_TARGET_JSON"])
    assert resolved_target["live_continuity"]["state"] == "ACTIVE_LKG"
    assert json.loads(env_values["IBKR_RECONCILIATION_EXPECTED_DIGESTS_JSON"]) == (
        _ledger(target)["transition_plan"]["expected_digests"]
    )
