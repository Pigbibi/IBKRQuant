"""Tests for read-only production drift observe script."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    import importlib.util

    path = ROOT / "scripts" / "production_drift_health_observe.py"
    spec = importlib.util.spec_from_file_location("production_drift_health_observe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_observe_script_exists() -> None:
    assert (ROOT / "scripts" / "production_drift_health_observe.py").is_file()


def test_observe_env_injected_score_is_read_only() -> None:
    module = _load_module()
    with (
        patch(
            "quant_platform_kit.strategy_lifecycle.production_drift_health_probe."
            "probe_production_drift_health",
            return_value={
                "status": "review",
                "score": 0.6,
                "threshold_version": "production_drift.v1",
                "actionable": True,
            },
        ) as probe,
        patch(
            "quant_platform_kit.strategy_lifecycle.research_promotion_cycle."
            "run_research_promotion_cycle"
        ) as promotion,
    ):
        summary = module.observe_production_drift(
            {
                "RUNTIME_TARGET_JSON": json.dumps(
                    {"strategy_profile": "demo", "market": "US"}
                ),
                "PRODUCTION_DRIFT_SCORE": "0.6",
                "PRODUCTION_DRIFT_AS_OF": "2026-09-07",
            }
        )

    assert summary["status"] == "review"
    assert summary["reason"] == "env_injected"
    assert summary["strategy_profile"] == "demo"
    assert summary["domain"] == "us_equity"
    probe.assert_called_once()
    promotion.assert_not_called()


def test_observe_from_store_parks_without_optimize() -> None:
    module = _load_module()
    with (
        patch(
            "quant_platform_kit.strategy_lifecycle.production_drift_health_probe."
            "probe_production_drift_health_from_store",
            return_value={
                "status": "parked",
                "score": None,
                "threshold_version": "production_drift.v1",
                "actionable": False,
                "reason": "drift_score_unavailable",
            },
        ) as probe,
        patch(
            "quant_platform_kit.strategy_lifecycle.research_promotion_cycle."
            "run_research_promotion_cycle"
        ) as promotion,
    ):
        summary = module.observe_production_drift(
            {
                "RUNTIME_TARGET_JSON": json.dumps(
                    {"strategy_profile": "demo", "domain": "us_equity"}
                ),
            }
        )

    assert summary["status"] == "parked"
    assert summary["actionable"] is False
    probe.assert_called_once()
    promotion.assert_not_called()


def test_main_fail_soft_on_bad_target(capsys: pytest.CaptureFixture[str]) -> None:
    module = _load_module()
    with patch.dict("os.environ", {"RUNTIME_TARGET_JSON": "{"}, clear=False):
        assert module.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "unavailable"
    assert payload["reason"] == "observe_error"
