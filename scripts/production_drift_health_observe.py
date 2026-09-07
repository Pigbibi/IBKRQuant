"""Read-only production drift observe for runtime-target-lifecycle.

Resolves strategy_profile/domain from RUNTIME_TARGET_JSON (or env overrides),
loads a sanitized drift_score from PerformanceStore when configured, and emits
a JSON summary. Never optimizes, promotes, or contacts a broker.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from datetime import date
from typing import Any


def _text(value: object) -> str:
    return str(value or "").strip()


def _load_target(environ: Mapping[str, str]) -> dict[str, Any]:
    raw = _text(environ.get("RUNTIME_TARGET_JSON"))
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("RUNTIME_TARGET_JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("RUNTIME_TARGET_JSON must be an object")
    return payload


def _strategy_profile(target: Mapping[str, Any], environ: Mapping[str, str]) -> str:
    profile = _text(environ.get("PRODUCTION_DRIFT_STRATEGY_PROFILE")) or _text(
        target.get("strategy_profile")
    )
    if not profile:
        raise ValueError("strategy_profile is required for production drift observe")
    return profile


def _domain(target: Mapping[str, Any], environ: Mapping[str, str]) -> str:
    domain = _text(environ.get("PRODUCTION_DRIFT_DOMAIN")) or _text(target.get("domain"))
    if domain:
        return domain
    market = _text(target.get("market")).upper()
    if market in {"US", "USA"}:
        return "us_equity"
    if market in {"HK", "HKG"}:
        return "hk_equity"
    if market in {"SG", "SGP"}:
        return "sg_equity"
    return "us_equity"


def observe_production_drift(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return a sanitized drift probe summary without side effects."""

    environment = os.environ if environ is None else environ
    target = _load_target(environment)
    strategy_profile = _strategy_profile(target, environment)
    domain = _domain(target, environment)

    from quant_platform_kit.strategy_lifecycle.production_drift_health_probe import (
        probe_production_drift_health,
        probe_production_drift_health_from_store,
    )

    injected = _text(environment.get("PRODUCTION_DRIFT_SCORE"))
    if injected:
        as_of = _text(environment.get("PRODUCTION_DRIFT_AS_OF")) or date.today().isoformat()
        summary = probe_production_drift_health(
            strategy_profile=strategy_profile,
            domain=domain,
            as_of=as_of,
            drift_score=float(injected),
        )
        summary = {**summary, "reason": "env_injected"}
    else:
        as_of = _text(environment.get("PRODUCTION_DRIFT_AS_OF")) or None
        summary = probe_production_drift_health_from_store(
            strategy_profile=strategy_profile,
            domain=domain,
            as_of=as_of,
        )

    summary["strategy_profile"] = strategy_profile
    summary["domain"] = domain
    return summary


def main(argv: list[str] | None = None) -> int:
    del argv  # CLI has no flags; configuration is env-driven.
    try:
        summary = observe_production_drift()
    except Exception as exc:  # noqa: BLE001 — observe must stay fail-soft for lifecycle
        summary = {
            "status": "unavailable",
            "score": None,
            "threshold_version": "production_drift.v1",
            "actionable": False,
            "reason": "observe_error",
            "error_type": type(exc).__name__,
        }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
