#!/usr/bin/env python3
"""Fail closed before a Cloud Run image rollout reaches an unadmitted target.

The deployment plan validates the desired configuration.  This checker protects
the other half of the boundary: a service which is already configured with a
retired or inconsistent runtime target must not receive a new image and become
an accidental compatibility migration.

Only non-sensitive target identity fields are read from Cloud Run.  The script
does not read Secret Manager values and never mutates a service or scheduler.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from strategy_registry import IBKR_PLATFORM, resolve_strategy_definition


class AdmissionError(ValueError):
    """A deployed runtime target is not safe to receive a new image."""


def _run(command: Sequence[str]) -> str:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise AdmissionError(detail or f"Command failed: {' '.join(command)}")
    return result.stdout


def _describe_service(*, service: str, project: str, region: str) -> Mapping[str, Any]:
    payload = _run(
        [
            "gcloud",
            "run",
            "services",
            "describe",
            service,
            f"--project={project}",
            f"--region={region}",
            "--format=json",
        ]
    )
    loaded = json.loads(payload)
    if not isinstance(loaded, Mapping):
        raise AdmissionError(f"{service}: Cloud Run describe returned a non-object payload")
    return loaded


def _container_env(service_json: Mapping[str, Any]) -> dict[str, str]:
    containers = (
        service_json.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
    )
    if not isinstance(containers, list) or not containers:
        raise AdmissionError("Cloud Run service has no container configuration")
    env_entries = containers[0].get("env", [])
    if not isinstance(env_entries, list):
        raise AdmissionError("Cloud Run container environment is malformed")
    env: dict[str, str] = {}
    for entry in env_entries:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or "").strip()
        if name and "value" in entry:
            env[name] = str(entry.get("value") or "").strip()
    return env


def _parse_bool(value: object, *, field: str, service: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise AdmissionError(f"{service}: {field} must be a boolean")


def _runtime_target(env: Mapping[str, str], *, service: str) -> Mapping[str, Any]:
    raw = env.get("RUNTIME_TARGET_JSON") or env.get("QSL_RUNTIME_TARGET_JSON")
    if not raw:
        raise AdmissionError(f"{service}: RUNTIME_TARGET_JSON is required for image admission")
    try:
        target = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AdmissionError(f"{service}: RUNTIME_TARGET_JSON is invalid JSON") from exc
    if not isinstance(target, Mapping):
        raise AdmissionError(f"{service}: RUNTIME_TARGET_JSON must be an object")
    return target


def verify_service(*, service: str, service_json: Mapping[str, Any]) -> dict[str, object]:
    """Validate one deployed service without printing account or secret data."""

    env = _container_env(service_json)
    target = _runtime_target(env, service=service)
    target_service = str(target.get("service_name") or "").strip()
    if target_service and target_service != service:
        raise AdmissionError(
            f"{service}: runtime target service_name does not match the deployed service"
        )

    raw_profile = str(target.get("strategy_profile") or "").strip()
    if not raw_profile:
        raise AdmissionError(f"{service}: runtime target strategy_profile is required")
    try:
        definition = resolve_strategy_definition(raw_profile, platform_id=IBKR_PLATFORM)
    except (TypeError, ValueError) as exc:
        raise AdmissionError(f"{service}: strategy profile is not admitted") from exc
    canonical_profile = definition.profile
    configured_profile = str(env.get("STRATEGY_PROFILE") or "").strip()
    if configured_profile != canonical_profile:
        raise AdmissionError(
            f"{service}: STRATEGY_PROFILE does not match the admitted runtime target profile"
        )

    execution_mode = str(target.get("execution_mode") or "").strip().lower()
    if execution_mode not in {"paper", "live"}:
        raise AdmissionError(f"{service}: execution_mode must be paper or live")
    if "dry_run_only" not in target:
        raise AdmissionError(f"{service}: runtime target dry_run_only is required")
    target_dry_run = _parse_bool(
        target["dry_run_only"], field="runtime target dry_run_only", service=service
    )
    configured_dry_run = env.get("IBKR_DRY_RUN_ONLY")
    if configured_dry_run is not None and _parse_bool(
        configured_dry_run, field="IBKR_DRY_RUN_ONLY", service=service
    ) != target_dry_run:
        raise AdmissionError(
            f"{service}: IBKR_DRY_RUN_ONLY does not match runtime target dry_run_only"
        )
    if target_dry_run and execution_mode != "paper":
        raise AdmissionError(
            f"{service}: a dry-run/shadow target must declare execution_mode=paper"
        )

    enabled = _parse_bool(
        env.get("RUNTIME_TARGET_ENABLED", "true"),
        field="RUNTIME_TARGET_ENABLED",
        service=service,
    )
    return {
        "service": service,
        "profile": canonical_profile,
        "execution_mode": execution_mode,
        "dry_run_only": target_dry_run,
        "enabled": enabled,
    }


def _services_from_plan(raw_plan: str) -> list[str]:
    try:
        plan = json.loads(raw_plan)
    except json.JSONDecodeError as exc:
        raise AdmissionError("SYNC_PLAN_JSON is invalid JSON") from exc
    targets = plan.get("targets") if isinstance(plan, Mapping) else None
    if not isinstance(targets, list):
        raise AdmissionError("SYNC_PLAN_JSON.targets must be a list")
    services: list[str] = []
    for target in targets:
        if not isinstance(target, Mapping):
            raise AdmissionError("SYNC_PLAN_JSON targets must be objects")
        service = str(target.get("service_name") or "").strip()
        if not service:
            raise AdmissionError("SYNC_PLAN_JSON target is missing service_name")
        services.append(service)
    return list(dict.fromkeys(services))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--service", action="append", default=[])
    args = parser.parse_args()

    services = [str(service).strip() for service in args.service if str(service).strip()]
    if not services:
        raw_plan = (os.environ.get("SYNC_PLAN_JSON") or "").strip()
        if not raw_plan:
            parser.error("--service or SYNC_PLAN_JSON is required")
        services = _services_from_plan(raw_plan)

    try:
        for service in services:
            result = verify_service(
                service=service,
                service_json=_describe_service(
                    service=service,
                    project=args.project,
                    region=args.region,
                ),
            )
            print(
                "Verified deployed runtime target admission: "
                f"service={result['service']}, profile={result['profile']}, "
                f"mode={result['execution_mode']}, dry_run_only={result['dry_run_only']}, "
                f"enabled={result['enabled']}"
            )
    except AdmissionError as exc:
        print(f"Deployed runtime target admission failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
