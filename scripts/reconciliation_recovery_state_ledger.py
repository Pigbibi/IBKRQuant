"""Fail-closed, local consumption of an immutable recovery state ledger.

This module deliberately has no Google Cloud, GitHub, Cloud Run, broker, or
order client.  The deployment workflow may opt in by supplying a locally
downloaded ledger file.  Without that explicit path, callers retain their
legacy runtime-target behaviour exactly.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from quant_platform_kit.common.live_continuity import build_live_continuity
from quant_platform_kit.common.reconciliation_recovery import ReconciliationRecoveryTransitionPlan


RECOVERY_STATE_LEDGER_SCHEMA_VERSION = "ibkr_reconciliation_recovery_state_ledger.v1"
RECOVERY_STATE_LEDGER_PATH_ENV = "IBKR_RECONCILIATION_RECOVERY_STATE_LEDGER_PATH"
RECOVERY_VERIFICATION_SCHEMA_VERSION = "ibkr_reconciliation_recovery_verification.v1"


def _ledger_service_name(ledger: Mapping[str, object]) -> str:
    service_name = ledger.get("service_name")
    if not isinstance(service_name, str) or service_name != service_name.strip() or not 3 <= len(service_name) <= 127:
        raise ValueError("reconciliation recovery state ledger service_name is invalid")
    return service_name


def build_recovery_state_ledger(
    *,
    verification: Mapping[str, object],
    service_name: str,
) -> dict[str, object]:
    """Build one minimal state ledger from an already fresh verify-only result.

    This is pure serialization.  It never creates a cloud object or changes a
    runtime target.  Requiring the complete verify-only receipt prevents a
    caller from turning a standalone plan into an activation intent.
    """

    required = {
        "schema_version",
        "recovery_id",
        "candidate_sha256",
        "confirmation_sha256",
        "ready_for_atomic_state_transition",
        "findings",
        "transition_plan",
        "policy",
    }
    if not isinstance(verification, Mapping) or set(verification) != required:
        raise ValueError("reconciliation recovery verification has invalid fields")
    if verification.get("schema_version") != RECOVERY_VERIFICATION_SCHEMA_VERSION:
        raise ValueError("unsupported reconciliation recovery verification schema")
    if verification.get("ready_for_atomic_state_transition") is not True or verification.get("findings") != []:
        raise ValueError("reconciliation recovery verification is not ready for a state ledger")
    expected_policy = {
        "controller_mode": "verify_only",
        "no_order": True,
        "execution_authority_granted": False,
        "state_write_attempted": False,
    }
    if verification.get("policy") != expected_policy:
        raise ValueError("reconciliation recovery verification has an invalid non-execution policy")
    raw_plan = verification.get("transition_plan")
    if not isinstance(raw_plan, Mapping):
        raise ValueError("reconciliation recovery verification is missing transition_plan")
    plan = ReconciliationRecoveryTransitionPlan.from_dict(raw_plan)
    recovery_id = str(verification.get("recovery_id") or "").strip()
    candidate_sha256 = str(verification.get("candidate_sha256") or "").strip().lower().removeprefix("sha256:")
    confirmation_sha256 = str(verification.get("confirmation_sha256") or "").strip().lower().removeprefix("sha256:")
    if recovery_id != plan.recovery_id:
        raise ValueError("reconciliation recovery verification recovery_id mismatch")
    if candidate_sha256 != plan.candidate_sha256:
        raise ValueError("reconciliation recovery verification candidate digest mismatch")
    if confirmation_sha256 != plan.confirmation_sha256:
        raise ValueError("reconciliation recovery verification confirmation digest mismatch")
    _ledger_service_name({"service_name": service_name})
    return {
        "schema_version": RECOVERY_STATE_LEDGER_SCHEMA_VERSION,
        "recovery_id": plan.recovery_id,
        "service_name": service_name,
        "transition_plan": plan.to_dict(),
    }


def apply_recovery_state_ledger(
    *,
    runtime_target: Mapping[str, object],
    ledger: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, str]]:
    """Return the one allowed target transition and its five fixed digests.

    A ledger contains only a QPK transition plan.  The desired target is never
    supplied by the ledger: it is derived from the current target by changing
    exactly ``live_continuity.state`` after every frozen-baseline precondition
    has been revalidated.
    """

    required = {"schema_version", "recovery_id", "service_name", "transition_plan"}
    if not isinstance(ledger, Mapping) or set(ledger) != required:
        raise ValueError("reconciliation recovery state ledger has invalid fields")
    if ledger.get("schema_version") != RECOVERY_STATE_LEDGER_SCHEMA_VERSION:
        raise ValueError("unsupported reconciliation recovery state ledger schema")
    ledger_service_name = _ledger_service_name(ledger)

    raw_plan = ledger.get("transition_plan")
    if not isinstance(raw_plan, Mapping):
        raise ValueError("reconciliation recovery state ledger is missing transition_plan")
    plan = ReconciliationRecoveryTransitionPlan.from_dict(raw_plan)
    if str(ledger.get("recovery_id") or "").strip() != plan.recovery_id:
        raise ValueError("reconciliation recovery state ledger recovery_id mismatch")

    target = copy.deepcopy(dict(runtime_target))
    if str(target.get("platform_id") or "").strip().lower() != "ibkr":
        raise ValueError("reconciliation recovery state ledger only supports ibkr targets")
    if str(target.get("service_name") or "").strip() != ledger_service_name:
        raise ValueError("reconciliation recovery state ledger service_name mismatch")
    continuity_payload = target.get("live_continuity")
    continuity = build_live_continuity(continuity_payload)
    continuity.assert_matches_target(target)
    if continuity.state != plan.expected_live_continuity_state:
        raise ValueError("reconciliation recovery state ledger current continuity state mismatch")
    if continuity.baseline_id != plan.baseline_id:
        raise ValueError("reconciliation recovery state ledger baseline_id mismatch")
    if continuity.baseline_target_sha256 != plan.baseline_target_sha256:
        raise ValueError("reconciliation recovery state ledger baseline digest mismatch")

    next_continuity = continuity.to_dict()
    next_continuity["state"] = plan.next_live_continuity_state
    target["live_continuity"] = next_continuity
    return target, dict(plan.expected_digests)


def apply_recovery_state_ledger_from_env(
    *,
    runtime_target: Mapping[str, object],
    env: Mapping[str, str],
) -> tuple[dict[str, object], dict[str, str] | None]:
    """Apply an explicitly supplied local ledger, or retain the original target."""

    raw_path = str(env.get(RECOVERY_STATE_LEDGER_PATH_ENV) or "").strip()
    if not raw_path:
        return dict(runtime_target), None
    try:
        value = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError("reconciliation recovery state ledger cannot be read") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("reconciliation recovery state ledger is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("reconciliation recovery state ledger must be a JSON object")
    ledger_service_name = _ledger_service_name(value)
    target_service_name = str(runtime_target.get("service_name") or "").strip()
    if target_service_name != ledger_service_name:
        return dict(runtime_target), None
    target, expected_digests = apply_recovery_state_ledger(
        runtime_target=runtime_target,
        ledger=value,
    )
    return target, expected_digests


__all__ = [
    "RECOVERY_STATE_LEDGER_PATH_ENV",
    "RECOVERY_STATE_LEDGER_SCHEMA_VERSION",
    "RECOVERY_VERIFICATION_SCHEMA_VERSION",
    "apply_recovery_state_ledger",
    "apply_recovery_state_ledger_from_env",
    "build_recovery_state_ledger",
]
