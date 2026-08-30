from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QPK_SRC = ROOT.parent / "QuantPlatformKit" / "src"
UES_SRC = ROOT.parent / "UsEquityStrategies" / "src"
HES_SRC = ROOT.parent / "HkEquityStrategies" / "src"


def _has_catalog_marker(candidate: Path, package_name: str, marker: str) -> bool:
    catalog_path = candidate / package_name / "catalog.py"
    if not catalog_path.exists():
        return False
    return marker in catalog_path.read_text(encoding="utf-8")


def _should_add_local_src(candidate: Path) -> bool:
    if candidate == QPK_SRC:
        return (candidate / "quant_platform_kit" / "common" / "runtime_target.py").exists()
    if candidate == UES_SRC:
        return _has_catalog_marker(
            candidate,
            "us_equity_strategies",
            "russell_top50_leader_rotation",
        )
    if candidate == HES_SRC:
        return _has_catalog_marker(
            candidate,
            "hk_equity_strategies",
            "hk_global_etf_tactical_rotation",
        )
    return True


for candidate in (ROOT, QPK_SRC, UES_SRC, HES_SRC):
    if not _should_add_local_src(candidate):
        continue
    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)

from strategy_registry import (  # noqa: E402
    IBKR_PLATFORM,
    describe_platform_runtime_requirements,
    get_platform_profile_status_matrix,
    resolve_strategy_definition,
)
from runtime_config_support import (  # noqa: E402
    DEFAULT_MARKET,
    DEFAULT_MARKET_TIMEZONE,
    resolve_market,
)


TARGETS_JSON_ENV = "CLOUD_RUN_SERVICE_TARGETS_JSON"
SHARED_TARGET_FALLBACK_ENV = frozenset(
    {
        "GLOBAL_TELEGRAM_CHAT_ID",
        "NOTIFY_LANG",
        "IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME",
        "IBKR_EXECUTION_BACKEND",
        "IBKR_FORCE_RUN",
        "IBKR_MARKET",
        "IBKR_MARKET_CALENDAR",
        "IBKR_MARKET_CURRENCY",
        "IBKR_MARKET_DATA_SYMBOL_SUFFIX",
        "IBKR_MARKET_EXCHANGE",
        "IBKR_MARKET_TIMEZONE",
        "IB_GATEWAY_ZONE",
        "IB_GATEWAY_IP_MODE",
        "IBKR_EXECUTION_DEDUP_ENABLED",
        "EXECUTION_REPORT_GCS_URI",
    }
)
REQUIRED_ENV = (
    "GLOBAL_TELEGRAM_CHAT_ID",
    "NOTIFY_LANG",
    "ACCOUNT_GROUP",
    "IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME",
)
PLATFORM_GENERIC_ENV = (
    "IB_GATEWAY_ZONE",
    "IB_GATEWAY_IP_MODE",
    "IBKR_EXECUTION_BACKEND",
    "IBKR_MARKET",
    "IBKR_MARKET_CALENDAR",
    "IBKR_MARKET_CURRENCY",
    "IBKR_MARKET_DATA_SYMBOL_SUFFIX",
    "IBKR_MARKET_EXCHANGE",
    "IBKR_MARKET_TIMEZONE",
    "IBKR_FEATURE_SNAPSHOT_PATH",
    "IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH",
    "IBKR_FEATURE_SNAPSHOT_FALLBACK_MODE",
    "IBKR_FEATURE_SNAPSHOT_FALLBACK_CACHE_DIR",
    "IBKR_FEATURE_SNAPSHOT_MAX_STALE_DAYS",
    "IBKR_STRATEGY_CONFIG_PATH",
    "IBKR_STRATEGY_PLUGIN_MOUNTS_JSON",
    "IBKR_RECONCILIATION_OUTPUT_PATH",
    "IBKR_DRY_RUN_ONLY",
    "IBKR_EXECUTION_DEDUP_ENABLED",
    "IBKR_PAPER_LIQUIDATE_ONLY",
    "IBKR_FORCE_RUN",
    "IBKR_MIN_RESERVED_CASH_USD",
    "IBKR_RESERVED_CASH_RATIO",
    "IBKR_CASH_ONLY_EXECUTION",
    "IBKR_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD",
    "INCOME_LAYER_ENABLED",
    "INCOME_LAYER_START_USD",
    "INCOME_LAYER_MAX_RATIO",
    "DCA_MODE",
    "DCA_BASE_INVESTMENT_USD",
    "IBIT_ZSCORE_EXIT_ENABLED",
    "IBIT_ZSCORE_EXIT_MODE",
    "IBIT_ZSCORE_EXIT_PARKING_SYMBOL",
    "IBIT_ZSCORE_EXIT_RISK_REDUCED_EXPOSURE",
    "IBIT_ZSCORE_EXIT_RISK_OFF_EXPOSURE",
    "IBIT_ZSCORE_EXIT_ALLOW_OUTSIDE_EXECUTION_WINDOW",
    "IBKR_MARKET_SIGNAL_HANDOFF_INDEX_URI",
    "IBKR_MARKET_SIGNAL_HANDOFF_MANIFEST_URI",
    "IBKR_MARKET_SIGNAL_CONSUMPTION_AUDIT_URI",
    "IBKR_MARKET_SIGNAL_CACHE_DIR",
    "IBKR_MARKET_SIGNAL_REQUIRED",
    "IBKR_MARKET_SIGNAL_FALLBACK_MODE",
    "IBKR_MARKET_SIGNAL_MAX_STALE_DAYS",
    "CASH_ONLY_EXECUTION",
    "RUNTIME_TARGET_ENABLED",
    "EXECUTION_REPORT_GCS_URI",
)
SCHEDULER_TIME_DEFAULTS = {
    "main_time": "45 15 * * 1-5",
    "probe_time": "35 9,15 * * 1-5",
    "precheck_time": "45 9 * * 1-5",
}
SCHEDULER_TIME_ENV = {
    "main_time": "CLOUD_SCHEDULER_MAIN_TIME",
    "probe_time": "CLOUD_SCHEDULER_PROBE_TIME",
    "precheck_time": "CLOUD_SCHEDULER_PRECHECK_TIME",
}
RUN_SCHEDULER_ATTEMPT_DEADLINE = "330s"
WEEKDAY_CRON_DAYS = frozenset({1, 2, 3, 4, 5})
CRON_DAY_NAMES = {
    "SUN": 0,
    "MON": 1,
    "TUE": 2,
    "WED": 3,
    "THU": 4,
    "FRI": 5,
    "SAT": 6,
}


def _cron_day_value(raw: str) -> int:
    value = raw.strip().upper()
    if value in CRON_DAY_NAMES:
        return CRON_DAY_NAMES[value]
    numeric = int(value)
    if not 0 <= numeric <= 7:
        raise ValueError(f"Invalid cron day-of-week value: {raw!r}")
    return numeric % 7


def _cron_days_of_week(raw: str) -> set[int]:
    days: set[int] = set()
    for item in raw.split(","):
        base, separator, raw_step = item.strip().partition("/")
        step = int(raw_step) if separator else 1
        if step <= 0:
            raise ValueError(f"Invalid cron day-of-week step: {item!r}")
        if base == "*":
            values = list(range(7))
        elif "-" in base:
            raw_start, raw_end = base.split("-", 1)
            start = _cron_day_value(raw_start)
            end = _cron_day_value(raw_end)
            values = [start]
            while values[-1] != end:
                values.append((values[-1] + 1) % 7)
                if len(values) > 7:
                    raise ValueError(f"Invalid cron day-of-week range: {item!r}")
        else:
            values = [_cron_day_value(base)]
        days.update(values[::step])
    return days

# Strategy-derived vars: auto-populated from platform-config.json defaults.
def _derive_strategy_env_defaults(strategy_config: dict) -> dict[str, str]:
    """Derive env var defaults from a strategy's platform-config.json entry."""
    if not strategy_config:
        return {}
    features = strategy_config.get("features", {})
    income = strategy_config.get("income_layer_defaults", {})
    options = strategy_config.get("option_overlay_defaults", {})
    dca = strategy_config.get("dca_defaults", {})
    defaults: dict[str, str] = {}

    # --- Features ---
    for feat_key, env_key in (
        ("income_layer", "INCOME_LAYER_ENABLED"),
        ("option_overlay", "OPTION_OVERLAY_ENABLED"),
    ):
        if feat_key in features:
            defaults[env_key] = str(features[feat_key]).lower()

    # --- Income-layer defaults ---
    for cfg_key, env_key in (
        ("start_usd", "INCOME_LAYER_START_USD"),
        ("max_ratio", "INCOME_LAYER_MAX_RATIO"),
    ):
        if cfg_key in income and income[cfg_key] is not None:
            defaults[env_key] = str(income[cfg_key])

    allocations = income.get("allocations") if isinstance(income, dict) else None
    if isinstance(allocations, dict):
        for symbol in ("QQQI", "SPYI"):
            weight = allocations.get(symbol)
            if weight is not None:
                defaults[f"{symbol}_INCOME_RATIO"] = str(weight)

    # --- Option-overlay defaults ---
    for cfg_key, env_key in (
        ("growth_enabled", "OPTION_GROWTH_OVERLAY_ENABLED"),
        ("income_enabled", "OPTION_INCOME_OVERLAY_ENABLED"),
        ("income_recipe", "OPTION_INCOME_OVERLAY_RECIPE"),
        ("income_start_usd", "OPTION_INCOME_OVERLAY_START_USD"),
        ("nav_risk_ratio", "OPTION_INCOME_OVERLAY_NAV_RISK_RATIO"),
        ("growth_recipe", "OPTION_GROWTH_OVERLAY_RECIPE"),
        ("growth_start_usd", "OPTION_GROWTH_OVERLAY_START_USD"),
        ("nav_budget_ratio", "OPTION_GROWTH_OVERLAY_NAV_BUDGET_RATIO"),
    ):
        if cfg_key in options and options[cfg_key] is not None:
            value = options[cfg_key]
            defaults[env_key] = str(value).lower() if isinstance(value, bool) else str(value)

    # --- DCA defaults ---
    for cfg_key, env_key in (
        ("default_mode", "DCA_MODE"),
        ("default_base_investment_usd", "DCA_BASE_INVESTMENT_USD"),
    ):
        if cfg_key in dca and dca[cfg_key] is not None:
            defaults[env_key] = str(dca[cfg_key])

    return defaults


# All vars that can appear as env values (union of platform-generic + strategy-derived).
OPTIONAL_TARGET_ENV = PLATFORM_GENERIC_ENV + (
    "INCOME_LAYER_ENABLED",
    "INCOME_LAYER_START_USD",
    "INCOME_LAYER_MAX_RATIO",
    "INCOME_THRESHOLD_USD",
    "QQQI_INCOME_RATIO",
    "SPYI_INCOME_RATIO",
    "OPTION_OVERLAY_ENABLED",
    "OPTION_GROWTH_OVERLAY_ENABLED",
    "OPTION_INCOME_OVERLAY_ENABLED",
    "OPTION_INCOME_OVERLAY_RECIPE",
    "OPTION_INCOME_OVERLAY_START_USD",
    "OPTION_INCOME_OVERLAY_NAV_RISK_RATIO",
    "OPTION_GROWTH_OVERLAY_RECIPE",
    "OPTION_GROWTH_OVERLAY_START_USD",
    "OPTION_GROWTH_OVERLAY_NAV_BUDGET_RATIO",
    "DCA_MODE",
    "DCA_BASE_INVESTMENT_USD",
    "IBIT_ZSCORE_EXIT_ENABLED",
    "IBIT_ZSCORE_EXIT_MODE",
    "IBIT_ZSCORE_EXIT_PARKING_SYMBOL",
    "IBIT_ZSCORE_EXIT_RISK_REDUCED_EXPOSURE",
    "IBIT_ZSCORE_EXIT_RISK_OFF_EXPOSURE",
    "IBIT_ZSCORE_EXIT_ALLOW_OUTSIDE_EXECUTION_WINDOW",
)




PLATFORM_CONFIG_ENV = "PLATFORM_CONFIG_JSON"


def _load_platform_config(env: Mapping[str, str]) -> dict:
    """Load platform-config.json, preferring explicit env var, falling back to a
    bundled copy or fetching from the QuantRuntimeSettings repo."""
    raw = str(env.get(PLATFORM_CONFIG_ENV, "") or "").strip()
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        path = Path(raw)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        raise FileNotFoundError(f"PLATFORM_CONFIG_JSON file not found: {raw}")

    bundled = ROOT / "platform-config.json"
    if bundled.exists():
        return json.loads(bundled.read_text(encoding="utf-8"))

    import subprocess
    try:
        result = subprocess.run(
            ["gh", "api",
             "repos/QuantStrategyLab/QuantRuntimeSettings/contents/platform-config.json",
             "--jq", ".content"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            import base64
            return json.loads(base64.b64decode(result.stdout.strip()).decode("utf-8"))
    except Exception:
        pass

    return {}

def build_sync_plan(env: Mapping[str, str] = os.environ) -> dict[str, object]:
    platform_config = _load_platform_config(env)
    target_entries, defaults, per_service_mode = _load_target_entries(env)
    status_rows = {
        str(row["canonical_profile"]): {
            **row,
            **describe_platform_runtime_requirements(
                str(row["canonical_profile"]),
                platform_id=IBKR_PLATFORM,
            ),
        }
        for row in get_platform_profile_status_matrix()
    }
    planned_targets = [
        _build_target_plan(
            target=target,
            defaults=defaults,
            env=env,
            status_rows=status_rows,
            per_service_mode=per_service_mode,
            platform_config=platform_config,
        )
        for target in target_entries
    ]
    if not planned_targets:
        raise ValueError(
            f"{TARGETS_JSON_ENV}, CLOUD_RUN_SERVICES, or CLOUD_RUN_SERVICE is required"
        )
    return {
        "mode": "per_service" if per_service_mode else "legacy",
        "targets": planned_targets,
    }


def _load_target_entries(
    env: Mapping[str, str],
) -> tuple[list[dict[str, object]], Mapping[str, object], bool]:
    raw_targets = str(env.get(TARGETS_JSON_ENV, "") or "").strip()
    if raw_targets:
        payload = json.loads(raw_targets)
        if isinstance(payload, Mapping):
            raw_entries = payload.get("targets")
            defaults = _coerce_mapping(payload.get("defaults") or {})
        else:
            raw_entries = payload
            defaults = {}
        if not isinstance(raw_entries, Sequence) or isinstance(raw_entries, (str, bytes)):
            raise ValueError(f"{TARGETS_JSON_ENV} must be a JSON array or object with targets")
        entries = [_coerce_mapping(item) for item in raw_entries]
        return entries, defaults, True

    raw_services = str(env.get("CLOUD_RUN_SERVICES", "") or "").strip()
    if not raw_services:
        raw_services = str(env.get("CLOUD_RUN_SERVICE", "") or "").strip()
    services = [
        item.strip()
        for chunk in raw_services.replace(";", ",").replace("\n", ",").split(",")
        for item in [chunk]
        if item.strip()
    ]
    return [{"service": service} for service in services], {}, False


def _build_target_plan(
    *,
    target: Mapping[str, object],
    defaults: Mapping[str, object],
    env: Mapping[str, str],
    status_rows: Mapping[str, Mapping[str, object]],
    per_service_mode: bool,
    platform_config: dict,
) -> dict[str, object]:
    service_name = _first_non_empty(
        _target_field(target, defaults, "service"),
        _target_field(target, defaults, "service_name"),
        _target_field(target, defaults, "cloud_run_service"),
    )
    runtime_target = _resolve_runtime_target(target, defaults, env, per_service_mode)
    if not service_name:
        service_name = str(runtime_target.get("service_name") or "").strip()
    if not service_name:
        raise ValueError("Each Cloud Run sync target requires service/service_name")

    runtime_target_service = str(runtime_target.get("service_name") or "").strip()
    if runtime_target_service and runtime_target_service != service_name:
        raise ValueError(
            f"Target {service_name} runtime_target.service_name={runtime_target_service!r} does not match"
        )
    runtime_target["service_name"] = service_name

    raw_profile = str(runtime_target.get("strategy_profile") or "").strip()
    if not raw_profile:
        raise ValueError(f"Target {service_name} runtime_target.strategy_profile is required")
    definition = resolve_strategy_definition(raw_profile, platform_id=IBKR_PLATFORM)
    canonical_profile = definition.profile
    runtime_target["strategy_profile"] = canonical_profile

    status = status_rows.get(canonical_profile)
    if status is None:
        supported = ", ".join(sorted(status_rows))
        raise ValueError(
            f"Unsupported STRATEGY_PROFILE={raw_profile!r} for {service_name}; supported: {supported}"
        )
    if not status.get("eligible") or not status.get("enabled"):
        raise ValueError(
            f"STRATEGY_PROFILE={raw_profile!r} is not eligible/enabled for {service_name}: {status}"
        )

    # Resolve strategy defaults from platform-config.json
    strategies_cfg = platform_config.get("strategies", {}) if platform_config else {}
    strategy_config = strategies_cfg.get(canonical_profile, {})
    strategy_defaults = _derive_strategy_env_defaults(strategy_config)

    # Overrides from RUNTIME_TARGET_JSON take precedence over strategy defaults
    overrides = runtime_target.get("overrides") if isinstance(runtime_target, Mapping) else None
    if isinstance(overrides, Mapping):
        for key, value in overrides.items():
            strategy_defaults[str(key).upper()] = _coerce_env_value(value) or ""

    env_values: dict[str, str] = {}
    missing: list[str] = []
    for name in REQUIRED_ENV:
        value = _target_env_value(
            target,
            defaults,
            env,
            name,
            per_service_mode=per_service_mode,
            allow_shared_fallback=name in SHARED_TARGET_FALLBACK_ENV,
        )
        if value is None and name == "ACCOUNT_GROUP":
            value = _first_non_empty(
                runtime_target.get("account_scope"),
                runtime_target.get("deployment_selector"),
            )
        if value is None:
            missing.append(f"{service_name}:{name}")
        else:
            env_values[name] = value

    env_values["STRATEGY_PROFILE"] = canonical_profile
    env_values["RUNTIME_TARGET_JSON"] = json.dumps(
        runtime_target,
        separators=(",", ":"),
        sort_keys=True,
    )

    remove_env_vars: list[str] = []
    for name in OPTIONAL_TARGET_ENV:
        value = _target_env_value(
            target,
            defaults,
            env,
            name,
            per_service_mode=per_service_mode,
            allow_shared_fallback=name in SHARED_TARGET_FALLBACK_ENV,
        )
        if value is None and name == "IBKR_DRY_RUN_ONLY":
            dry_run_value = runtime_target.get("dry_run_only")
            if dry_run_value is not None:
                value = _coerce_env_value(dry_run_value)
        # 3. Strategy default from platform-config.json
        if value is None:
            value = strategy_defaults.get(name)
        # 4. Override from RUNTIME_TARGET_JSON.overrides
        if value is None and isinstance(overrides, Mapping):
            override_value = (
                overrides[name]
                if name in overrides
                else overrides.get(name.lower())
            )
            if override_value is not None:
                value = _coerce_env_value(override_value)
        if value is None:
            remove_env_vars.append(name)
        else:
            env_values[name] = value

    if _runtime_target_enabled(env_values):
        _validate_profile_inputs(
            service_name=service_name,
            env_values=env_values,
            status=status,
            missing=missing,
        )
    if missing:
        raise ValueError(
            "Cloud Run env sync target values are missing:\n"
            + "\n".join(f"  - {item}" for item in missing)
        )

    if (
        _runtime_target_enabled(env_values)
        and not _runtime_target_is_dry_run_only(runtime_target, env_values)
        and str(env_values.get("IBKR_FORCE_RUN") or "").strip().lower() == "true"
    ):
        raise ValueError("IBKR_FORCE_RUN=true is not allowed for live Cloud Run targets")

    scheduler = _build_scheduler_plan(
        runtime_target=runtime_target,
        target=target,
        defaults=defaults,
        env=env,
        env_values=env_values,
        per_service_mode=per_service_mode,
    )
    if _requires_extended_run_deadline(runtime_target, env_values):
        scheduler["attempt_deadline"] = RUN_SCHEDULER_ATTEMPT_DEADLINE

    return {
        "service_name": service_name,
        "strategy_profile": canonical_profile,
        "env": env_values,
        "scheduler": scheduler,
        "remove_env_vars": sorted(set(remove_env_vars) - set(env_values)),
    }


def _build_scheduler_plan(
    *,
    runtime_target: Mapping[str, object],
    target: Mapping[str, object],
    defaults: Mapping[str, object],
    env: Mapping[str, str],
    env_values: Mapping[str, str],
    per_service_mode: bool,
) -> dict[str, str]:
    runtime_scheduler = runtime_target.get("scheduler") if isinstance(runtime_target, Mapping) else {}
    if not isinstance(runtime_scheduler, Mapping):
        runtime_scheduler = {}
    market = resolve_market(
        env_values.get("IBKR_MARKET") or runtime_target.get("market"),
        account_group=str(env_values.get("ACCOUNT_GROUP") or ""),
    )
    timezone = str(runtime_scheduler.get("timezone") or env_values.get("IBKR_MARKET_TIMEZONE") or "").strip()
    if not timezone:
        timezone = "Asia/Hong_Kong" if market == "HK" else "America/New_York"

    scheduler = {"timezone": timezone}
    for key, env_name in SCHEDULER_TIME_ENV.items():
        configured_value = _target_env_value(
            target,
            defaults,
            env,
            env_name,
            per_service_mode=per_service_mode,
            allow_shared_fallback=True,
        )
        scheduler[key] = str(runtime_scheduler.get(key) or configured_value or SCHEDULER_TIME_DEFAULTS[key])
    if (
        market == DEFAULT_MARKET
        and _runtime_target_enabled(env_values)
        and not _runtime_target_is_dry_run_only(runtime_target, env_values)
    ):
        if timezone != DEFAULT_MARKET_TIMEZONE:
            raise ValueError(
                f"US live account scheduler timezone must be {DEFAULT_MARKET_TIMEZONE}: "
                f"{timezone!r}"
            )
        for key in SCHEDULER_TIME_ENV:
            fields = scheduler[key].split()
            if len(fields) == 2:
                scheduler[key] = " ".join([*fields, "*", "*", "1-5"])
                fields = scheduler[key].split()
            try:
                scheduled_days = _cron_days_of_week(fields[4]) if len(fields) == 5 else set()
            except (TypeError, ValueError):
                scheduled_days = set()
            if (
                len(fields) != 5
                or fields[2] != "*"
                or not scheduled_days
                or not scheduled_days <= WEEKDAY_CRON_DAYS
            ):
                raise ValueError(
                    f"US live account scheduler {key} must be Mon-Fri cron: "
                    f"{scheduler[key]!r}"
                )
    return scheduler


def _runtime_target_is_dry_run_only(
    runtime_target: Mapping[str, object],
    env_values: Mapping[str, str],
) -> bool:
    raw_dry_run = env_values.get("IBKR_DRY_RUN_ONLY")
    if raw_dry_run is None:
        raw_dry_run = runtime_target.get("dry_run_only")
    return str(raw_dry_run or "").strip().lower() in {"1", "true", "yes", "on"}


def _requires_extended_run_deadline(
    runtime_target: Mapping[str, object],
    env_values: Mapping[str, str],
) -> bool:
    """Reserve enough scheduler time for any live target backed by IB Gateway."""
    backend = str(env_values.get("IBKR_EXECUTION_BACKEND") or "gateway").strip().lower()
    execution_mode = str(runtime_target.get("execution_mode") or "").strip().lower()
    return (
        backend == "gateway"
        and execution_mode != "paper"
        and not _runtime_target_is_dry_run_only(runtime_target, env_values)
    )


def _validate_profile_inputs(
    *,
    service_name: str,
    env_values: Mapping[str, str],
    status: Mapping[str, object],
    missing: list[str],
) -> None:
    if bool(status.get("requires_snapshot_artifacts")) and not env_values.get(
        "IBKR_FEATURE_SNAPSHOT_PATH"
    ):
        missing.append(f"{service_name}:IBKR_FEATURE_SNAPSHOT_PATH")
    if bool(status.get("requires_snapshot_manifest_path")) and not env_values.get(
        "IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH"
    ):
        missing.append(f"{service_name}:IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH")
    if (
        bool(status.get("requires_strategy_config_path"))
        and str(status.get("config_source_policy") or "none") == "env_only"
        and not env_values.get("IBKR_STRATEGY_CONFIG_PATH")
    ):
        missing.append(f"{service_name}:IBKR_STRATEGY_CONFIG_PATH")
    if str(status.get("reconciliation_output_policy") or "none") == "required" and not env_values.get(
        "IBKR_RECONCILIATION_OUTPUT_PATH"
    ):
        missing.append(f"{service_name}:IBKR_RECONCILIATION_OUTPUT_PATH")


def _runtime_target_enabled(env_values: Mapping[str, str]) -> bool:
    raw = str(env_values.get("RUNTIME_TARGET_ENABLED") or "").strip().lower()
    if not raw:
        return True
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError("RUNTIME_TARGET_ENABLED must be true or false")


def _resolve_runtime_target(
    target: Mapping[str, object],
    defaults: Mapping[str, object],
    env: Mapping[str, str],
    per_service_mode: bool,
) -> dict[str, object]:
    configured_service = str(_first_non_empty(
        _target_field(target, defaults, "service"),
        _target_field(target, defaults, "service_name"),
        _target_field(target, defaults, "cloud_run_service"),
    ) or "").strip()
    current_service = str(env.get("CLOUD_RUN_SERVICE", "") or "").strip()
    environment_target = str(env.get("RUNTIME_TARGET_JSON", "") or "").strip()

    # An environment-scoped switch is the desired state for its exact service.
    # It must not be silently replaced by a legacy repository service inventory;
    # sibling services continue to use their own inventory entries.
    raw = environment_target if (
        per_service_mode and environment_target and configured_service == current_service
    ) else _first_non_empty(
        _target_field(target, defaults, "runtime_target"),
        _target_field(target, defaults, "runtime_target_json"),
    )
    if raw is None and not per_service_mode:
        raw = str(env.get("RUNTIME_TARGET_JSON", "") or "").strip() or None
    if raw is None:
        raise ValueError(
            f"{TARGETS_JSON_ENV}.targets[].runtime_target is required in per-service mode"
        )
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        loaded = json.loads(raw)
        if not isinstance(loaded, Mapping):
            raise ValueError("runtime_target_json must decode to a JSON object")
        return dict(loaded)
    raise ValueError("runtime_target must be a JSON object or JSON object string")


def _target_env_value(
    target: Mapping[str, object],
    defaults: Mapping[str, object],
    env: Mapping[str, str],
    env_name: str,
    *,
    per_service_mode: bool,
    allow_shared_fallback: bool,
) -> str | None:
    raw = _first_non_empty(
        _target_field(target, defaults, env_name),
        _target_field(target, defaults, env_name.lower()),
    )
    if raw is None and (not per_service_mode or allow_shared_fallback):
        raw = str(env.get(env_name, "") or "").strip() or None
    return _coerce_env_value(raw)


def _target_field(
    target: Mapping[str, object],
    defaults: Mapping[str, object],
    name: str,
) -> object | None:
    for source in (target, _coerce_mapping(target.get("env") or {}), defaults, _coerce_mapping(defaults.get("env") or {})):
        if name in source:
            return source[name]
    return None


def _coerce_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("Expected a JSON object")
    return value


def _coerce_env_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    text = str(value).strip()
    return text or None


def _first_non_empty(*values: object | None) -> object | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="Print compact JSON.")
    args = parser.parse_args()

    try:
        plan = build_sync_plan()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(plan, separators=(",", ":"), sort_keys=True))
    else:
        print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
