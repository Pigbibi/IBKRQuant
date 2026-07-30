"""Per-runtime-target schedule and market-session policy for heartbeat checks."""

from __future__ import annotations

import datetime as dt
import json
import sys
from collections.abc import Callable, Mapping
from typing import Any
from zoneinfo import ZoneInfo


SessionDatesLoader = Callable[..., set[dt.date]]
WarningLogger = Callable[[str], None]

_LATEST_DUE_AT_KEY = "_heartbeat_latest_due_at"
_MARKET_DEFAULTS = {
    "US": ("NYSE", "America/New_York"),
    "HK": ("XHKG", "Asia/Hong_Kong"),
    "CN": ("SSE", "Asia/Shanghai"),
    "SG": ("XSES", "Asia/Singapore"),
    "CRYPTO": ("24/7", "UTC"),
}
_TIMEZONE_MARKETS = {
    "America/New_York": "US",
    "Asia/Hong_Kong": "HK",
    "Asia/Shanghai": "CN",
    "Asia/Singapore": "SG",
}
_PLATFORM_MARKETS = {
    "schwab": "US",
    "firstrade": "US",
    "qmt": "CN",
    "binance": "CRYPTO",
}


def _split_values(raw: str | None) -> list[str]:
    if not raw:
        return []
    values = str(raw).replace(";", ",").replace("\n", ",").split(",")
    return [value.strip() for value in values if value.strip()]


def _enabled(value: Any, *, default: bool = True) -> bool:
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "n", "off"}


def _runtime_target(item: Mapping[str, Any]) -> dict[str, Any]:
    value = item.get("runtime_target") or item.get("runtime_target_json")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = {}
    if isinstance(value, dict):
        return value
    return dict(item)


def _first_value(sources: list[Mapping[str, Any]], keys: tuple[str, ...]) -> str:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return ""


def _suffix_value(sources: list[Mapping[str, Any]], suffix: str) -> str:
    for source in sources:
        for key, value in source.items():
            if str(key).upper().endswith(suffix) and value is not None and str(value).strip():
                return str(value).strip()
    return ""


def _service_values(
    item: Mapping[str, Any],
    runtime_target: Mapping[str, Any],
    environ: Mapping[str, str],
) -> list[str]:
    value = _first_value(
        [item, runtime_target],
        ("service", "service_name", "cloud_run_service"),
    )
    if value:
        return _split_values(value)
    services = _split_values(environ.get("CLOUD_RUN_SERVICES"))
    services.extend(_split_values(environ.get("CLOUD_RUN_SERVICE")))
    return list(dict.fromkeys(services))


def _normalize_target(
    item: Mapping[str, Any],
    runtime_target: Mapping[str, Any],
    service: str,
    environ: Mapping[str, str],
    *,
    use_global_market_fallback: bool,
) -> dict[str, Any]:
    sources = [runtime_target, item]
    scheduler = next(
        (
            value
            for source in sources
            if isinstance((value := source.get("scheduler")), dict)
        ),
        {},
    )
    account_scope = _first_value(
        sources,
        (
            "account_scope",
            "account_group",
            "account_region",
            "ACCOUNT_GROUP",
            "ACCOUNT_REGION",
        ),
    )
    target_market_timezone = (
        _first_value(sources, ("market_timezone", "MARKET_TIMEZONE"))
        or _suffix_value(sources, "_MARKET_TIMEZONE")
    )
    global_market_timezone = (
        str(environ.get("RUNTIME_HEARTBEAT_MARKET_TIMEZONE") or "").strip()
        or _suffix_value([environ], "_MARKET_TIMEZONE")
    )
    market_timezone = target_market_timezone or (
        global_market_timezone if use_global_market_fallback else ""
    )
    market = (
        _first_value(sources, ("market", "MARKET"))
        or _suffix_value(sources, "_MARKET")
    ).upper()
    if not market:
        market = _TIMEZONE_MARKETS.get(target_market_timezone, "")
    if market not in _MARKET_DEFAULTS:
        market = _TIMEZONE_MARKETS.get(str(scheduler.get("timezone") or "").strip(), "")
    if not market:
        platform_id = _first_value(sources, ("platform_id",)).lower()
        market = _PLATFORM_MARKETS.get(platform_id, "")
    if not market and account_scope.upper() in {"US", "HK", "CN"}:
        market = account_scope.upper()
    if market not in _MARKET_DEFAULTS and use_global_market_fallback:
        market = str(environ.get("RUNTIME_HEARTBEAT_MARKET") or "").strip().upper()
    target_market_calendar = (
        _first_value(sources, ("market_calendar", "MARKET_CALENDAR"))
        or _suffix_value(sources, "_MARKET_CALENDAR")
    )
    global_market_calendar = (
        str(environ.get("RUNTIME_HEARTBEAT_MARKET_CALENDAR") or "").strip()
        or _suffix_value([environ], "_MARKET_CALENDAR")
    )
    default_calendar, default_timezone = _MARKET_DEFAULTS.get(market, ("", ""))
    market_calendar = (
        target_market_calendar
        or (global_market_calendar if use_global_market_fallback else "")
        or default_calendar
    )
    market_timezone = (
        market_timezone
        or default_timezone
        or str(scheduler.get("timezone") or "").strip()
    )
    return {
        "service": str(service).strip(),
        "strategy_profile": _first_value(
            sources,
            ("strategy_profile", "strategy", "profile"),
        ),
        "account_scope": account_scope,
        "scheduler": dict(scheduler),
        "market": market,
        "market_calendar": market_calendar,
        "market_timezone": market_timezone,
    }


def load_runtime_targets(environ: Mapping[str, str]) -> list[dict[str, Any]]:
    raw_targets = str(environ.get("CLOUD_RUN_SERVICE_TARGETS_JSON") or "").strip()
    items: list[Mapping[str, Any]] = []
    if raw_targets:
        try:
            payload = json.loads(raw_targets)
        except json.JSONDecodeError:
            payload = {}
        targets = payload.get("targets") if isinstance(payload, dict) else payload
        if isinstance(targets, list):
            items = [target for target in targets if isinstance(target, dict)]

    if not items:
        raw_runtime_target = str(environ.get("RUNTIME_TARGET_JSON") or "").strip()
        if raw_runtime_target:
            try:
                runtime_target = json.loads(raw_runtime_target)
            except json.JSONDecodeError:
                runtime_target = {}
            if isinstance(runtime_target, dict):
                items = [runtime_target]

    expected_scope = str(environ.get("RUNTIME_HEARTBEAT_ACCOUNT_SCOPE") or "").strip().lower()
    eligible: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    for item in items:
        runtime_target = _runtime_target(item)
        enabled_value = item.get("runtime_target_enabled")
        if enabled_value is None:
            enabled_value = item.get("RUNTIME_TARGET_ENABLED")
        if enabled_value is None:
            enabled_value = runtime_target.get("runtime_target_enabled")
        if not _enabled(enabled_value):
            continue
        target_scope = _first_value(
            [item, runtime_target],
            (
                "account_scope",
                "account_group",
                "account_region",
                "ACCOUNT_GROUP",
                "ACCOUNT_REGION",
            ),
        )
        if expected_scope and target_scope and target_scope.lower() != expected_scope:
            continue
        eligible.append((item, runtime_target))

    normalized: list[dict[str, Any]] = []
    for item, runtime_target in eligible:
        for service in _service_values(item, runtime_target, environ):
            target = _normalize_target(
                item,
                runtime_target,
                service,
                environ,
                use_global_market_fallback=len(eligible) == 1,
            )
            key = target_key(target)
            if key and all(target_key(existing) != key for existing in normalized):
                normalized.append(target)
    return normalized


def target_key(target: Mapping[str, Any]) -> str:
    service = str(target.get("service") or "").strip().lower()
    strategy = str(target.get("strategy_profile") or "").strip().lower()
    scope = str(target.get("account_scope") or "").strip().lower()
    return f"{service}|{strategy or '*'}|{scope or '*'}" if service else ""


def target_label(target: Mapping[str, Any]) -> str:
    service = str(target.get("service") or "").strip() or "<unknown-service>"
    strategy = str(target.get("strategy_profile") or "").strip()
    scope = str(target.get("account_scope") or "").strip()
    qualifiers = "/".join(value for value in (strategy, scope) if value)
    return f"{service}[{qualifiers}]" if qualifiers else service


def target_latest_due_at(target: Mapping[str, Any]) -> dt.datetime | None:
    value = target.get(_LATEST_DUE_AT_KEY)
    return value if isinstance(value, dt.datetime) else None


def _payload_value(payload: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    runtime_target = payload.get("runtime_target")
    sources = [payload]
    if isinstance(runtime_target, Mapping):
        sources.append(runtime_target)
    return _first_value(sources, keys)


def match_payload_target(
    payload: Mapping[str, Any],
    targets: list[dict[str, Any]],
) -> tuple[str | None, str]:
    service = _payload_value(
        payload,
        ("service_name", "service", "cloud_run_service"),
    ).lower()
    strategy = _payload_value(
        payload,
        ("strategy_profile", "strategy", "profile"),
    ).lower()
    scope = _payload_value(
        payload,
        ("account_scope", "account_group", "account_region"),
    ).lower()
    for target in targets:
        expected_service = str(target.get("service") or "").strip().lower()
        expected_strategy = str(target.get("strategy_profile") or "").strip().lower()
        expected_scope = str(target.get("account_scope") or "").strip().lower()
        if service != expected_service:
            continue
        if expected_strategy and strategy != expected_strategy:
            continue
        if expected_scope and scope != expected_scope:
            continue
        return target_key(target), "matched runtime target"
    return None, (
        f"runtime_target={service or '-'}/{strategy or '-'}/{scope or '-'}"
    )


def _cron_token_value(token: str, *, names: dict[str, int] | None = None) -> int:
    normalized = token.strip().lower()
    if names and normalized in names:
        return names[normalized]
    return int(normalized)


def _cron_field_values(
    field: str,
    *,
    minimum: int,
    maximum: int,
    names: dict[str, int] | None = None,
) -> set[int] | None:
    text = str(field or "").strip().lower()
    if text in {"", "*"}:
        return None
    values: set[int] = set()
    for raw_part in text.split(","):
        part = raw_part.strip()
        if not part:
            continue
        base, raw_step = part, "1"
        if "/" in part:
            base, raw_step = part.split("/", 1)
        step = max(1, int(raw_step))
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            raw_start, raw_end = base.split("-", 1)
            start = _cron_token_value(raw_start, names=names)
            end = _cron_token_value(raw_end, names=names)
        else:
            start = end = _cron_token_value(base, names=names)
        for value in range(start, end + 1, step):
            if minimum <= value <= maximum:
                values.add(value)
            elif maximum == 6 and value == 7:
                values.add(0)
    return values


def cron_matches(schedule: str, value: dt.datetime) -> bool:
    fields = str(schedule or "").split()
    if len(fields) == 2:
        fields.extend(("*", "*", "*"))
    if len(fields) != 5:
        return False
    minute, hour, day_of_month, month, day_of_week = fields
    dow_names = {
        "sun": 0,
        "mon": 1,
        "tue": 2,
        "wed": 3,
        "thu": 4,
        "fri": 5,
        "sat": 6,
    }
    minute_values = _cron_field_values(minute, minimum=0, maximum=59)
    hour_values = _cron_field_values(hour, minimum=0, maximum=23)
    dom_values = _cron_field_values(day_of_month, minimum=1, maximum=31)
    month_values = _cron_field_values(month, minimum=1, maximum=12)
    dow_values = _cron_field_values(day_of_week, minimum=0, maximum=6, names=dow_names)
    if minute_values is not None and value.minute not in minute_values:
        return False
    if hour_values is not None and value.hour not in hour_values:
        return False
    if month_values is not None and value.month not in month_values:
        return False
    dom_matches = dom_values is None or value.day in dom_values
    dow_matches = dow_values is None or value.isoweekday() % 7 in dow_values
    if dom_values is not None and dow_values is not None:
        return dom_matches or dow_matches
    return dom_matches and dow_matches


def _market_session_dates(
    calendar: str,
    *,
    start_date: dt.date,
    end_date: dt.date,
) -> set[dt.date]:
    import pandas_market_calendars as mcal

    schedule = mcal.get_calendar(calendar).schedule(
        start_date=start_date,
        end_date=end_date,
    )
    return {value.date() for value in schedule.index}


def _target_due_status(
    target: Mapping[str, Any],
    *,
    since: dt.datetime,
    now: dt.datetime,
    market_aware: bool,
    session_dates_loader: SessionDatesLoader,
    warning_logger: WarningLogger,
) -> tuple[bool | None, dt.datetime | None]:
    scheduler = target.get("scheduler")
    if not isinstance(scheduler, Mapping):
        return None, None
    schedule = str(scheduler.get("main_time") or "").strip()
    fields = schedule.split()
    if len(fields) == 2:
        schedule = f"{schedule} * * *"
    elif len(fields) != 5:
        return None, None
    timezone_name = str(scheduler.get("timezone") or "UTC").strip() or "UTC"
    try:
        scheduler_timezone = ZoneInfo(timezone_name)
    except Exception as exc:  # noqa: BLE001
        warning_logger(
            f"Unable to evaluate heartbeat scheduler timezone {timezone_name}: "
            f"{type(exc).__name__}; keeping target required"
        )
        return None, None

    since_utc = since.astimezone(dt.timezone.utc)
    now_utc = now.astimezone(dt.timezone.utc)
    session_dates: set[dt.date] | None = None
    market_calendar = str(target.get("market_calendar") or "").strip()
    if market_aware and market_calendar:
        market_timezone_name = (
            str(target.get("market_timezone") or "").strip() or timezone_name
        )
        try:
            market_timezone = ZoneInfo(market_timezone_name)
            session_dates = session_dates_loader(
                market_calendar,
                start_date=since_utc.astimezone(market_timezone).date(),
                end_date=now_utc.astimezone(market_timezone).date(),
            )
        except Exception as exc:  # noqa: BLE001
            warning_logger(
                f"Unable to evaluate heartbeat market calendar {market_calendar}: "
                f"{type(exc).__name__}; keeping target required"
            )
            return None, None

    cursor = since_utc.replace(second=0, microsecond=0)
    if cursor < since_utc:
        cursor += dt.timedelta(minutes=1)
    latest_due_at: dt.datetime | None = None
    while cursor <= now_utc:
        local_time = cursor.astimezone(scheduler_timezone)
        try:
            matches = cron_matches(schedule, local_time)
        except (TypeError, ValueError) as exc:
            warning_logger(
                f"Unable to evaluate heartbeat cron for {target_label(target)}: "
                f"{type(exc).__name__}; keeping target required"
            )
            return None, None
        if matches:
            if session_dates is None:
                latest_due_at = cursor
            else:
                market_timezone = ZoneInfo(
                    str(target.get("market_timezone") or "").strip() or timezone_name
                )
                if cursor.astimezone(market_timezone).date() in session_dates:
                    latest_due_at = cursor
        cursor += dt.timedelta(minutes=1)
    return latest_due_at is not None, latest_due_at


def filter_due_targets(
    targets: list[dict[str, Any]],
    *,
    since: dt.datetime,
    now: dt.datetime,
    market_aware: bool = True,
    session_dates_loader: SessionDatesLoader = _market_session_dates,
    warning_logger: WarningLogger = lambda message: print(message, file=sys.stderr),
) -> tuple[list[dict[str, Any]], bool]:
    due: list[dict[str, Any]] = []
    evaluated = False
    for target in targets:
        status, latest_due_at = _target_due_status(
            target,
            since=since,
            now=now,
            market_aware=market_aware,
            session_dates_loader=session_dates_loader,
            warning_logger=warning_logger,
        )
        if status is not None:
            evaluated = True
        if status is not False:
            due_target = dict(target)
            if latest_due_at is not None:
                due_target[_LATEST_DUE_AT_KEY] = latest_due_at
            due.append(due_target)
    return due, evaluated


def filter_services_for_targets(
    services: list[str],
    targets: list[dict[str, Any]],
    *,
    all_targets: list[dict[str, Any]] | None = None,
) -> list[str]:
    if not targets:
        return services
    target_services = {
        str(target.get("service") or "").strip()
        for target in targets
        if str(target.get("service") or "").strip()
    }
    configured_services = {
        str(target.get("service") or "").strip()
        for target in (all_targets or targets)
        if str(target.get("service") or "").strip()
    }
    return [
        service
        for service in services
        if service not in configured_services or service in target_services
    ]
