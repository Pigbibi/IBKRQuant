import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "verify_deployed_runtime_target_admission.py"
)
SPEC = importlib.util.spec_from_file_location("deployed_target_admission", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
admission = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admission)


def service_payload(*, runtime_target: dict, profile: str, dry_run_only: str = "true") -> dict:
    return {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "env": [
                                {"name": "RUNTIME_TARGET_JSON", "value": json.dumps(runtime_target)},
                                {"name": "STRATEGY_PROFILE", "value": profile},
                                {"name": "IBKR_DRY_RUN_ONLY", "value": dry_run_only},
                                {"name": "RUNTIME_TARGET_ENABLED", "value": "true"},
                            ]
                        }
                    ]
                }
            }
        }
    }


def admitted_target(*, profile: str = "tqqq_growth_income", dry_run_only: bool = True) -> dict:
    return {
        "platform_id": "ibkr",
        "service_name": "shadow-service",
        "strategy_profile": profile,
        "execution_mode": "paper" if dry_run_only else "live",
        "dry_run_only": dry_run_only,
    }


def test_verify_service_accepts_admitted_shadow_target():
    result = admission.verify_service(
        service="shadow-service",
        service_json=service_payload(runtime_target=admitted_target(), profile="tqqq_growth_income"),
    )

    assert result == {
        "service": "shadow-service",
        "profile": "tqqq_growth_income",
        "execution_mode": "paper",
        "dry_run_only": True,
        "enabled": True,
    }


def test_verify_service_accepts_admitted_paper_broker_target():
    target = admitted_target(dry_run_only=False)
    target["execution_mode"] = "paper"

    result = admission.verify_service(
        service="shadow-service",
        service_json=service_payload(
            runtime_target=target,
            profile="tqqq_growth_income",
            dry_run_only="false",
        ),
    )

    assert result["execution_mode"] == "paper"
    assert result["dry_run_only"] is False


def test_verify_service_rejects_profile_drift():
    with pytest.raises(admission.AdmissionError, match="STRATEGY_PROFILE does not match"):
        admission.verify_service(
            service="shadow-service",
            service_json=service_payload(runtime_target=admitted_target(), profile="soxl_soxx_trend_income"),
        )


def test_verify_service_rejects_shadow_declared_as_live():
    target = admitted_target()
    target["execution_mode"] = "live"

    with pytest.raises(admission.AdmissionError, match="dry-run/shadow target"):
        admission.verify_service(
            service="shadow-service",
            service_json=service_payload(runtime_target=target, profile="tqqq_growth_income"),
        )


def test_verify_service_rejects_retired_profile():
    target = admitted_target(profile="retired_profile")

    with pytest.raises(admission.AdmissionError, match="not admitted"):
        admission.verify_service(
            service="shadow-service",
            service_json=service_payload(runtime_target=target, profile="retired_profile"),
        )
