#!/usr/bin/env python3
"""Verify that an expected runtime report was written recently."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from quant_platform_kit.common.operational_notification_localization import (
    format_operational_alert,
    operational_notification_text,
    resolve_operational_notification_locale,
)

try:
    from scripts.runtime_heartbeat_policy import (
        filter_due_targets,
        filter_services_for_targets,
        load_runtime_targets,
        match_payload_target,
        runtime_target_configuration_has_enabled_targets,
        runtime_target_configuration_present,
        runtime_target_permits_standard_execution,
        target_key,
        target_label,
        target_latest_due_at,
    )
except ModuleNotFoundError:
    from runtime_heartbeat_policy import (  # type: ignore[no-redef]
        filter_due_targets,
        filter_services_for_targets,
        load_runtime_targets,
        match_payload_target,
        runtime_target_configuration_has_enabled_targets,
        runtime_target_configuration_present,
        runtime_target_permits_standard_execution,
        target_key,
        target_label,
        target_latest_due_at,
    )


DEFAULT_ACCEPT_STATUSES = {"ok", "skipped", "success", "completed", "no_action"}
DEFAULT_REJECT_STATUSES = {"error", "failed", "failure", "cancelled", "canceled", "timed_out"}
DEFAULT_REJECT_EXECUTION_STATUSES = {"error", "failed", "failure"}
REPORTABLE_EXECUTION_BACKENDS = frozenset({"gateway", "quantconnect"})
EXPECTED_EXECUTION_GUARD_REASON_PREFIXES = (
    "pending_orders_detected:",
    "same_day_fills_detected:",
    "same_day_execution_locked:",
)


def _report_execution_backend(payload: Mapping[str, Any]) -> str:
    backend = payload.get("execution_backend")
    if isinstance(backend, str) and backend in REPORTABLE_EXECUTION_BACKENDS:
        return backend
    return "unknown"


DEFAULT_ACCEPT_STAGES = {
    "DRY_RUN_COMPLETED",
    "FUNDING_BLOCKED",
    "NO_ACTION",
    "ORDERS_PLANNED",
    "PARTIAL_SUBMITTED",
    "RECONCILED",
    "SUBMITTED",
    "COMPLETED",
}
DEFAULT_REJECT_STAGES = {"ERROR", "EXECUTION_BLOCKED", "FAILED", "FAILURE"}


def _split_values(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in re.split(r"[,;\n]+", raw) if part.strip()]


def _env_bool(name: str, default: bool = False) -> bool:
    value = (os.environ.get(name) or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on"}


def _notification_locale() -> str:
    return resolve_operational_notification_locale(os.environ.get("NOTIFY_LANG"))


def _notice(key: str, /, **values: object) -> str:
    return operational_notification_text(_notification_locale(), key, **values)


def _enabled_value(value: Any, *, default: bool = True) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return True


def _target_runtime_target(target: dict[str, Any]) -> dict[str, Any]:
    runtime_target = target.get("runtime_target") or target.get("runtime_target_json")
    if isinstance(runtime_target, str):
        try:
            runtime_target = json.loads(runtime_target)
        except json.JSONDecodeError:
            runtime_target = {}
    return runtime_target if isinstance(runtime_target, dict) else {}


def _target_account_scope(target: dict[str, Any], runtime_target: dict[str, Any]) -> str:
    for source in (target, runtime_target):
        for key in (
            "account_scope",
            "account_group",
            "account_region",
            "ACCOUNT_GROUP",
            "ACCOUNT_REGION",
        ):
            value = source.get(key)
            if value:
                return str(value).strip()
    return ""


def _target_matches_expected_scope(target: dict[str, Any], runtime_target: dict[str, Any]) -> bool:
    expected_scope = (os.environ.get("RUNTIME_HEARTBEAT_ACCOUNT_SCOPE") or "").strip().lower()
    if not expected_scope:
        return True
    target_scope = _target_account_scope(target, runtime_target).lower()
    return not target_scope or target_scope == expected_scope


def _target_enabled(target: dict[str, Any], runtime_target: dict[str, Any]) -> bool:
    value = target.get("runtime_target_enabled")
    if value is None:
        value = target.get("RUNTIME_TARGET_ENABLED")
    if value is None:
        value = runtime_target.get("runtime_target_enabled")
    return (
        _enabled_value(value, default=True)
        and runtime_target_permits_standard_execution(runtime_target)
    )


def _target_service_values(target: dict[str, Any], runtime_target: dict[str, Any]) -> list[str]:
    for key in ("service", "service_name", "cloud_run_service"):
        value = target.get(key) or runtime_target.get(key)
        if value:
            return _split_values(str(value))
    return []


def _parse_schedule_day_of_month_field(raw: str) -> set[int] | None:
    text = str(raw or "").strip()
    if not text or text in {"*", "?"}:
        return None
    days: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            try:
                step = max(1, int(step_text))
            except ValueError:
                return None
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            try:
                start = int(start_text)
                end = int(end_text)
            except ValueError:
                return None
            if start > end:
                return None
            days.update(day for day in range(start, end + 1, step) if 1 <= day <= 31)
            continue
        try:
            day = int(part)
        except ValueError:
            return None
        if 1 <= day <= 31:
            days.add(day)
    return days or None


def _scheduler_window_status(
    scheduler: dict[str, Any],
    *,
    since: dt.datetime,
    now: dt.datetime,
) -> tuple[bool, str] | None:
    cron = str(scheduler.get("main_time") or "").strip()
    fields = cron.split()
    if len(fields) != 5:
        return None
    expected_days = _parse_schedule_day_of_month_field(fields[2])
    if not expected_days:
        return None
    timezone_name = str(scheduler.get("timezone") or "UTC").strip() or "UTC"
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone = dt.timezone.utc
        timezone_name = "UTC"
    local_since_date = since.astimezone(timezone).date()
    local_now_date = now.astimezone(timezone).date()
    cursor = local_since_date
    due = False
    while cursor <= local_now_date:
        if cursor.day in expected_days:
            due = True
            break
        cursor += dt.timedelta(days=1)
    day_text = ",".join(str(day) for day in sorted(expected_days))
    reason = (
        f"{timezone_name} date_window={local_since_date.isoformat()}.."
        f"{local_now_date.isoformat()}; expected day(s)={day_text}"
    )
    return due, reason


def _target_scheduler(target: dict[str, Any], runtime_target: dict[str, Any]) -> dict[str, Any]:
    for source in (runtime_target, target):
        scheduler = source.get("scheduler") if isinstance(source, dict) else None
        if isinstance(scheduler, dict):
            return scheduler
    return {}


def _runtime_target_payload() -> dict[str, Any]:
    raw_runtime_target = (os.environ.get("RUNTIME_TARGET_JSON") or "").strip()
    if not raw_runtime_target:
        return {}
    try:
        runtime_target = json.loads(raw_runtime_target)
    except json.JSONDecodeError:
        return {}
    return runtime_target if isinstance(runtime_target, dict) else {}


def _runtime_target_enabled() -> bool:
    value: Any = os.environ.get("RUNTIME_TARGET_ENABLED")
    if value is None:
        value = _runtime_target_payload().get("runtime_target_enabled")
    return _enabled_value(value, default=True)


def _runtime_target_schedule_candidates() -> list[tuple[str, dict[str, Any]]]:
    candidates: list[tuple[str, dict[str, Any]]] = []
    raw_targets = (os.environ.get("CLOUD_RUN_SERVICE_TARGETS_JSON") or "").strip()
    if raw_targets:
        try:
            payload = json.loads(raw_targets)
        except json.JSONDecodeError:
            payload = {}
        targets = payload.get("targets") if isinstance(payload, dict) else payload
        if isinstance(targets, list):
            for target in targets:
                if not isinstance(target, dict):
                    continue
                runtime_target = _target_runtime_target(target)
                if not _target_matches_expected_scope(target, runtime_target):
                    continue
                if not _target_enabled(target, runtime_target):
                    continue
                scheduler = _target_scheduler(target, runtime_target)
                if not scheduler:
                    continue
                services = _target_service_values(target, runtime_target) or [
                    str(runtime_target.get("service_name") or "runtime_target")
                ]
                candidates.extend((service, scheduler) for service in services)
        if isinstance(targets, list):
            return candidates

    runtime_target = _runtime_target_payload()
    if not runtime_target:
        return []
    scheduler = _target_scheduler(runtime_target, runtime_target)
    if not scheduler:
        return []
    service = str(runtime_target.get("service_name") or "runtime_target")
    return [(service, scheduler)]


def _runtime_target_scheduler_skip_reason(since: dt.datetime, now: dt.datetime) -> str | None:
    candidates = _runtime_target_schedule_candidates()
    if not candidates:
        return None
    reasons: list[str] = []
    for service, scheduler in candidates:
        status = _scheduler_window_status(scheduler, since=since, now=now)
        if status is None:
            return None
        due, reason = status
        if due:
            return None
        reasons.append(f"{service}: {reason}")
    if not reasons:
        return None
    return "runtime target scheduler main_time is not due today (" + "; ".join(reasons) + ")"


def _parse_timestamp(value: Any) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _month_segments(start: dt.datetime, end: dt.datetime) -> list[str]:
    months = []
    cursor = dt.datetime(start.year, start.month, 1, tzinfo=dt.timezone.utc)
    end_cursor = dt.datetime(end.year, end.month, 1, tzinfo=dt.timezone.utc)
    while cursor <= end_cursor:
        months.append(f"{cursor.year:04d}-{cursor.month:02d}")
        if cursor.month == 12:
            cursor = dt.datetime(cursor.year + 1, 1, 1, tzinfo=dt.timezone.utc)
        else:
            cursor = dt.datetime(cursor.year, cursor.month + 1, 1, tzinfo=dt.timezone.utc)
    return months


def _base_report_uris() -> list[str]:
    uris = _split_values(os.environ.get("RUNTIME_HEARTBEAT_GCS_URIS"))
    uris.extend(_split_values(os.environ.get("EXECUTION_REPORT_GCS_URI")))

    state_bucket = (os.environ.get("FIRSTRADE_GCS_STATE_BUCKET") or "").strip()
    if state_bucket:
        state_prefix = (os.environ.get("FIRSTRADE_STATE_PREFIX") or "firstrade-platform").strip("/")
        base = f"gs://{state_bucket}"
        if state_prefix:
            base = f"{base}/{state_prefix}"
        uris.append(f"{base}/strategy-runs")

    seen = set()
    unique = []
    for uri in uris:
        clean = uri.rstrip("/")
        if clean and clean not in seen:
            seen.add(clean)
            unique.append(clean)
    return unique


def _load_target_service_candidates() -> tuple[list[str], list[str]]:
    targets = load_runtime_targets(os.environ, include_disabled=True)
    enabled_target_services = [
        str(target.get("service") or "").strip()
        for target in targets
        if target.get("enabled") is True
    ]
    disabled_target_services = [
        str(target.get("service") or "").strip()
        for target in targets
        if target.get("enabled") is False
    ]
    return _unique_values(enabled_target_services), _unique_values(disabled_target_services)


def _filter_disabled_services(
    services: list[str],
    enabled_services: list[str],
    disabled_services: list[str],
) -> list[str]:
    if not disabled_services:
        return services
    enabled_service_set = set(enabled_services)
    disabled_service_set = set(disabled_services) - enabled_service_set
    return [service for service in services if service not in disabled_service_set]


def _load_required_service_candidates() -> tuple[list[str], bool]:
    enabled_target_services, disabled_target_services = _load_target_service_candidates()
    explicit_services = _split_values(os.environ.get("RUNTIME_HEARTBEAT_REQUIRED_SERVICES"))
    if explicit_services:
        services = _filter_disabled_services(
            explicit_services,
            enabled_target_services,
            disabled_target_services,
        )
        return _unique_values(services), True

    services = []
    for name in (
        "CLOUD_RUN_SERVICES",
        "CLOUD_RUN_SERVICE",
    ):
        services.extend(_split_values(os.environ.get(name)))

    services = _filter_disabled_services(
        services,
        enabled_target_services,
        disabled_target_services,
    )
    services.extend(enabled_target_services)

    return _unique_values(services), False


def _load_required_services(
    *,
    project: str | None = None,
    since: dt.datetime | None = None,
    now: dt.datetime | None = None,
) -> list[str]:
    services, _skip_reason, _scheduler_checked = _resolve_required_services(
        project=project,
        since=since,
        now=now,
    )
    return services


def _resolve_required_services(
    *,
    project: str | None = None,
    since: dt.datetime | None = None,
    now: dt.datetime | None = None,
) -> tuple[list[str], str | None, bool]:
    services, explicit = _load_required_service_candidates()
    if explicit and not services:
        return [], "all explicitly required heartbeat services are disabled", False
    if explicit or not services:
        return services, None, False
    if not _env_bool("RUNTIME_HEARTBEAT_SCHEDULER_AWARE", True):
        return services, None, False
    if since is None or now is None:
        return services, None, False
    try:
        due_services = _filter_scheduler_due_services(
            services,
            project=project,
            since=since,
            now=now,
        )
    except RuntimeError as exc:
        print(
            f"Unable to resolve Cloud Scheduler-backed heartbeat services: {exc}; "
            "falling back to all configured services.",
            file=sys.stderr,
        )
        return services, None, False
    if not due_services:
        return (
            [],
            "no configured Cloud Scheduler main job was due in the heartbeat lookback window",
            True,
        )
    return due_services, None, True


def _unique_values(values: list[str]) -> list[str]:
    seen = set()
    unique = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def _scheduler_location() -> str:
    return (
        os.environ.get("RUNTIME_HEARTBEAT_SCHEDULER_LOCATION")
        or os.environ.get("CLOUD_RUN_REGION")
        or "us-central1"
    )


def _list_scheduler_jobs(*, project: str | None) -> list[dict[str, Any]]:
    command = [
        "gcloud",
        "scheduler",
        "jobs",
        "list",
        "--location",
        _scheduler_location(),
        "--format=json",
    ]
    if project:
        command.extend(["--project", project])
    result = _run_gcloud(command)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or "gcloud scheduler jobs list failed")
    if not result.stdout.strip():
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gcloud scheduler jobs list returned invalid JSON: {exc}") from exc
    return payload if isinstance(payload, list) else []


def _describe_scheduler_job(job_name: str, *, project: str | None) -> dict[str, Any] | None:
    command = [
        "gcloud",
        "scheduler",
        "jobs",
        "describe",
        job_name,
        "--location",
        _scheduler_location(),
        "--format=json",
    ]
    if project:
        command.extend(["--project", project])
    result = _run_gcloud(command)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if "not_found" in detail.lower() or "not found" in detail.lower():
            return None
        raise RuntimeError(detail or f"gcloud scheduler jobs describe failed for {job_name}")
    if not result.stdout.strip():
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gcloud scheduler jobs describe returned invalid JSON for {job_name}: {exc}") from exc
    return payload if isinstance(payload, dict) else None


def _describe_scheduler_jobs_for_services(
    services: list[str],
    *,
    project: str | None,
) -> list[dict[str, Any]]:
    jobs = []
    for service in services:
        for job_name in _scheduler_job_name_candidates(service):
            job = _describe_scheduler_job(job_name, project=project)
            if job:
                jobs.append(job)
                break
    return jobs


def _scheduler_job_name_candidates(service: str) -> list[str]:
    service_name = str(service or "").strip()
    if not service_name:
        return []
    candidates = [f"{service_name}-scheduler"]
    if service_name.endswith("-service"):
        candidates.append(f"{service_name.removesuffix('-service')}-scheduler")
    return _unique_values(candidates)


def _hydrate_runtime_target_schedules(
    targets: list[dict[str, Any]],
    *,
    project: str | None,
) -> list[dict[str, Any]]:
    hydrated: list[dict[str, Any]] = []
    for target in targets:
        scheduler = target.get("scheduler")
        effective_scheduler = dict(scheduler) if isinstance(scheduler, dict) else {}
        if len(str(effective_scheduler.get("main_time") or "").split()) == 5:
            hydrated.append(target)
            continue
        if not _env_bool("RUNTIME_HEARTBEAT_SCHEDULER_AWARE", True):
            raise RuntimeError(
                "runtime target schedule is incomplete and scheduler lookup is disabled"
            )
        service = str(target.get("service") or "").strip()
        job = None
        for job_name in _scheduler_job_name_candidates(service):
            job = _describe_scheduler_job(job_name, project=project)
            if job is not None:
                break
        schedule = str((job or {}).get("schedule") or "").strip()
        if len(schedule.split()) != 5:
            raise RuntimeError(
                f"unable to resolve effective five-field scheduler cron for {service or '<unknown-service>'}"
            )
        timezone_name = str((job or {}).get("timeZone") or "").strip()
        effective_scheduler["main_time"] = schedule
        if timezone_name:
            effective_scheduler["timezone"] = timezone_name
        hydrated.append({**target, "scheduler": effective_scheduler})
    return hydrated


def _scheduler_job_targets_strategy_run(job: dict[str, Any], service: str) -> bool:
    if str(job.get("state") or "").strip().upper() not in {"", "ENABLED"}:
        return False
    uri = str((job.get("httpTarget") or {}).get("uri") or "").strip()
    if not uri:
        return False
    parsed = urllib.parse.urlparse(uri)
    path = parsed.path or "/"
    if path != "/":
        return False
    service_text = str(service or "").strip().lower()
    return bool(service_text and service_text in parsed.netloc.lower())


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


def _cron_matches(schedule: str, value: dt.datetime) -> bool:
    fields = str(schedule or "").split()
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
    cron_weekday = value.isoweekday() % 7
    dow_matches = dow_values is None or cron_weekday in dow_values
    if dom_values is not None and dow_values is not None:
        return dom_matches or dow_matches
    return dom_matches and dow_matches


def _scheduler_job_due_between(
    job: dict[str, Any],
    *,
    since: dt.datetime,
    now: dt.datetime,
) -> bool:
    schedule = str(job.get("schedule") or "").strip()
    if not schedule:
        return False
    try:
        timezone = ZoneInfo(str(job.get("timeZone") or "UTC"))
    except Exception:  # noqa: BLE001
        timezone = dt.timezone.utc

    since_utc = since.astimezone(dt.timezone.utc)
    now_utc = now.astimezone(dt.timezone.utc)
    cursor = since_utc.replace(second=0, microsecond=0)
    if cursor < since_utc:
        cursor += dt.timedelta(minutes=1)
    while cursor <= now_utc:
        if _cron_matches(schedule, cursor.astimezone(timezone)):
            return True
        cursor += dt.timedelta(minutes=1)
    return False


def _filter_scheduler_due_services(
    services: list[str],
    *,
    project: str | None,
    since: dt.datetime,
    now: dt.datetime,
) -> list[str]:
    try:
        jobs = _list_scheduler_jobs(project=project)
    except RuntimeError as exc:
        print(
            f"Unable to list Cloud Scheduler jobs: {exc}; trying named scheduler job lookup.",
            file=sys.stderr,
        )
        jobs = _describe_scheduler_jobs_for_services(services, project=project)
    due_services = []
    for service in services:
        service_jobs = [
            job for job in jobs if _scheduler_job_targets_strategy_run(job, service)
        ]
        if not service_jobs or any(
            _scheduler_job_due_between(job, since=since, now=now)
            for job in service_jobs
        ):
            due_services.append(service)
    return due_services


def _report_globs(since: dt.datetime, now: dt.datetime) -> list[str]:
    explicit = _split_values(os.environ.get("RUNTIME_HEARTBEAT_GCS_GLOBS"))
    if explicit:
        return explicit
    globs = []
    platform = (os.environ.get("RUNTIME_HEARTBEAT_REPORT_PLATFORM") or "").strip("/")
    for base in _base_report_uris():
        if platform and not base.rstrip("/").endswith(f"/{platform}"):
            base = f"{base}/{platform}"
        for month in _month_segments(since, now):
            globs.append(f"{base}/**/{month}/*.json")
    return globs


def _run_gcloud(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False)


def _list_gcs_objects(gcs_glob: str, *, project: str | None) -> list[dict[str, Any]]:
    command = ["gcloud", "storage", "ls", "--json", gcs_glob]
    if project:
        command.extend(["--project", project])
    result = _run_gcloud(command)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if "matched no objects" in detail.lower() or "no urls matched" in detail.lower():
            return []
        raise RuntimeError(detail or f"gcloud storage ls failed for {gcs_glob}")
    if not result.stdout.strip():
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gcloud storage ls returned invalid JSON for {gcs_glob}: {exc}") from exc
    return payload if isinstance(payload, list) else []


def _cat_gcs_json(uri: str, *, project: str | None) -> dict[str, Any] | None:
    clean_uri = uri.split("#", 1)[0]
    command = ["gcloud", "storage", "cat", clean_uri]
    if project:
        command.extend(["--project", project])
    result = _run_gcloud(command)
    if result.returncode != 0:
        print(f"Unable to read report {clean_uri}: {(result.stderr or result.stdout).strip()}", file=sys.stderr)
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"Report is not valid JSON: {clean_uri}", file=sys.stderr)
        return None
    return payload if isinstance(payload, dict) else None


def _object_uri(entry: dict[str, Any]) -> str:
    return str(entry.get("url") or "").split("#", 1)[0]


def _object_updated_at(entry: dict[str, Any]) -> dt.datetime | None:
    metadata = entry.get("metadata") or {}
    return _parse_timestamp(metadata.get("updated") or metadata.get("timeCreated"))


def _report_errors(payload: dict[str, Any]) -> list[Any]:
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        return errors
    error_summary = payload.get("error_summary")
    if isinstance(error_summary, dict):
        nested = error_summary.get("errors")
        if isinstance(nested, list) and nested:
            return nested
    if payload.get("error"):
        return [payload.get("error")]
    return []


def _report_status(payload: dict[str, Any]) -> tuple[str, str]:
    status = str(payload.get("status") or payload.get("summary", {}).get("status") or "").strip()
    stage = str(payload.get("stage") or payload.get("summary", {}).get("stage") or "").strip()
    return status, stage


def _report_notification_failure(payload: dict[str, Any]) -> str:
    scopes = [payload]
    summary = payload.get("summary")
    if isinstance(summary, dict):
        scopes.append(summary)
    for scope in scopes:
        delivery_summary = scope.get("notification_delivery_summary")
        if isinstance(delivery_summary, dict) and delivery_summary.get("all_acknowledged") is False:
            return "notification delivery not acknowledged"
        if scope.get("notification_error") and scope.get("notification_suppressed") is not True:
            return "notification delivery failed"
    return ""


def _report_execution_status(payload: dict[str, Any]) -> str:
    summary = payload.get("summary")
    nested_status = summary.get("execution_status") if isinstance(summary, dict) else None
    return str(payload.get("execution_status") or nested_status or "").strip()


def _report_no_op_reason(payload: dict[str, Any]) -> str:
    summary = payload.get("summary")
    nested_reason = summary.get("no_op_reason") if isinstance(summary, dict) else None
    return str(payload.get("no_op_reason") or nested_reason or "").strip()


def _is_expected_execution_guard(execution_status: str, no_op_reason: str) -> bool:
    return execution_status.lower() == "blocked" and no_op_reason.lower().startswith(
        EXPECTED_EXECUTION_GUARD_REASON_PREFIXES
    )


def _payload_service_name(payload: dict[str, Any]) -> str:
    runtime_target = payload.get("runtime_target")
    service = payload.get("service_name")
    if not service and isinstance(runtime_target, dict):
        service = runtime_target.get("service_name")
    return str(service or "").strip()


def _payload_account_scope(payload: dict[str, Any]) -> str:
    for key in ("account_scope", "account_group", "account_region"):
        value = payload.get(key)
        if value:
            return str(value).strip()
    return ""


def _payload_matches(
    payload: dict[str, Any],
    required_services: list[str],
    *,
    required_targets: list[dict[str, Any]] | None = None,
) -> tuple[bool, str, str]:
    expected_platform = (os.environ.get("RUNTIME_HEARTBEAT_REPORT_PLATFORM") or "").strip().lower()
    if expected_platform:
        platform = str(payload.get("platform") or "").strip().lower()
        if platform != expected_platform:
            return False, "", f"platform={platform or '-'}"

    expected_scope = (os.environ.get("RUNTIME_HEARTBEAT_ACCOUNT_SCOPE") or "").strip().lower()
    if expected_scope:
        account_scope = _payload_account_scope(payload).lower()
        if account_scope != expected_scope:
            return False, "", f"account_scope={account_scope or '-'}"

    service_name = _payload_service_name(payload)
    if required_services and service_name not in required_services:
        return False, service_name, f"service_name={service_name or '-'}"
    if required_targets:
        matched_target, reason = match_payload_target(payload, required_targets)
        if matched_target:
            return True, matched_target, reason
        target_services = {
            str(target.get("service") or "").strip()
            for target in required_targets
        }
        if service_name in target_services:
            return False, service_name, reason
    return True, service_name, "matched filters"


def _is_accepted_report(payload: dict[str, Any]) -> tuple[bool, str]:
    accepted_statuses = {
        value.lower()
        for value in (_split_values(os.environ.get("RUNTIME_HEARTBEAT_ACCEPT_STATUSES")) or DEFAULT_ACCEPT_STATUSES)
    }
    reject_statuses = {
        value.lower()
        for value in (_split_values(os.environ.get("RUNTIME_HEARTBEAT_REJECT_STATUSES")) or DEFAULT_REJECT_STATUSES)
    }
    accepted_stages = {
        value.upper()
        for value in (_split_values(os.environ.get("RUNTIME_HEARTBEAT_ACCEPT_STAGES")) or DEFAULT_ACCEPT_STAGES)
    }
    reject_stages = {
        value.upper()
        for value in (_split_values(os.environ.get("RUNTIME_HEARTBEAT_REJECT_STAGES")) or DEFAULT_REJECT_STAGES)
    }
    allow_errors = _env_bool("RUNTIME_HEARTBEAT_ACCEPT_REPORTS_WITH_ERRORS", False)

    status, stage = _report_status(payload)
    status_key = status.lower()
    stage_key = stage.upper()
    execution_status = _report_execution_status(payload)
    execution_status_key = execution_status.lower()
    no_op_reason = _report_no_op_reason(payload)
    errors = _report_errors(payload)
    notification_failure = _report_notification_failure(payload)
    if errors and not allow_errors:
        return False, f"errors={len(errors)} status={status or '-'} stage={stage or '-'}"
    if notification_failure:
        return False, notification_failure
    if status_key in reject_statuses or stage_key in reject_stages:
        return False, f"rejected status={status or '-'} stage={stage or '-'}"
    if execution_status_key in DEFAULT_REJECT_EXECUTION_STATUSES or (
        execution_status_key == "blocked"
        and not _is_expected_execution_guard(execution_status, no_op_reason)
    ):
        return False, f"rejected execution_status={execution_status}"
    if status_key and status_key in accepted_statuses:
        return True, f"status={status}"
    if stage_key and stage_key in accepted_stages:
        return True, f"stage={stage}"
    if not status_key and not stage_key and not errors and not _env_bool("RUNTIME_HEARTBEAT_REQUIRE_STATUS", False):
        return True, "report exists"
    return False, f"unaccepted status={status or '-'} stage={stage or '-'}"




def _telegram_secret_project() -> str | None:
    return (
        os.environ.get("RUNTIME_HEARTBEAT_GCP_PROJECT_ID")
        or os.environ.get("RUNTIME_GUARD_GCP_PROJECT_ID")
        or os.environ.get("GCP_PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
    )


def _load_telegram_token_from_secret() -> str:
    secret_name = (os.environ.get("TELEGRAM_TOKEN_SECRET_NAME") or "").strip()
    if not secret_name:
        return ""
    command = ["gcloud", "secrets", "versions", "access", "latest", "--secret", secret_name]
    project = _telegram_secret_project()
    if project:
        command.extend(["--project", project])
    result = _run_gcloud(command)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        print(
            f"Unable to read Telegram token from Secret Manager: {detail or 'gcloud failed'}",
            file=sys.stderr,
        )
        return ""
    return result.stdout.strip()


def _telegram_token() -> str:
    direct_token = (os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TG_TOKEN") or "").strip()
    if direct_token:
        return direct_token
    return _load_telegram_token_from_secret()

def _send_telegram(message: str) -> bool:
    targets: list[tuple[str, str]] = []
    token = _telegram_token()
    for chat_id in _split_values(os.environ.get("GLOBAL_TELEGRAM_CHAT_ID")):
        if token:
            targets.append((token, chat_id))
    unique_targets = list(dict.fromkeys(targets))
    if not unique_targets:
        print("No Telegram token/chat configured; unable to send heartbeat alert.", file=sys.stderr)
        return False
    base_url = "https://api.telegram.org"
    ok = True
    for token_value, chat_id in unique_targets:
        body = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/bot{token_value}/sendMessage",
            data=body,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                ok = ok and response.status < 400
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"Telegram send failed: {type(exc).__name__}", file=sys.stderr)
    return ok


def main(now: dt.datetime | None = None) -> int:
    project = (
        os.environ.get("RUNTIME_HEARTBEAT_GCP_PROJECT_ID")
        or os.environ.get("GCP_PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
    )
    name = os.environ.get("RUNTIME_HEARTBEAT_NAME") or os.environ.get("GITHUB_REPOSITORY") or "runtime"
    if not _runtime_target_enabled():
        print(f"Execution report heartbeat skipped for {name}: runtime target is disabled")
        return 0
    if (
        runtime_target_configuration_present(os.environ)
        and not runtime_target_configuration_has_enabled_targets(os.environ)
    ):
        print(
            f"Execution report heartbeat skipped for {name}: "
            "no enabled runtime target matches this heartbeat"
        )
        return 0
    lookback_hours = float(os.environ.get("RUNTIME_HEARTBEAT_LOOKBACK_HOURS") or "36")
    max_reports = int(os.environ.get("RUNTIME_HEARTBEAT_MAX_REPORTS_TO_READ") or "20")
    fail_workflow = _env_bool("RUNTIME_HEARTBEAT_FAIL_WORKFLOW_ON_ALERT", True)

    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    now = now.astimezone(dt.timezone.utc)
    since = now - dt.timedelta(hours=lookback_hours)
    runtime_targets = load_runtime_targets(os.environ)
    try:
        runtime_targets = _hydrate_runtime_target_schedules(
            runtime_targets,
            project=project,
        )
    except RuntimeError as exc:
        message = format_operational_alert(
            locale=_notification_locale(),
            alert_type="execution_report_heartbeat",
            name=name,
            context={"lookback_hours": lookback_hours},
            issues=[_notice("heartbeat_scheduler_policy_error")],
            technical_details=[str(exc)],
        )
        print(message)
        _send_telegram(message)
        return 1 if fail_workflow else 0
    publication_grace_minutes = float(
        os.environ.get("RUNTIME_HEARTBEAT_PUBLICATION_GRACE_MINUTES") or "30"
    )
    if publication_grace_minutes < 0:
        raise ValueError("RUNTIME_HEARTBEAT_PUBLICATION_GRACE_MINUTES must be non-negative")
    due_targets, target_schedule_evaluated = filter_due_targets(
        runtime_targets,
        since=since,
        now=now,
        market_aware=_env_bool("RUNTIME_HEARTBEAT_MARKET_AWARE", True),
        publication_grace=dt.timedelta(minutes=publication_grace_minutes),
    )
    if runtime_targets and target_schedule_evaluated and not due_targets:
        target_names = ", ".join(target_label(target) for target in runtime_targets)
        schedule_reason = _runtime_target_scheduler_skip_reason(since, now)
        print(
            f"Execution report heartbeat skipped for {name}: "
            + (
                schedule_reason
                or "no runtime target main schedule was due on an open market session "
                f"({target_names})"
            )
        )
        return 0
    if not runtime_targets:
        runtime_target_skip_reason = _runtime_target_scheduler_skip_reason(since, now)
        if runtime_target_skip_reason:
            print(f"Execution report heartbeat skipped for {name}: {runtime_target_skip_reason}")
            return 0

    required_services, scheduler_skip_reason, _scheduler_checked = _resolve_required_services(
        project=project,
        since=since,
        now=now,
    )
    if scheduler_skip_reason:
        print(f"Execution report heartbeat skipped for {name}: {scheduler_skip_reason}")
        return 0
    if due_targets:
        required_services = filter_services_for_targets(
            required_services,
            due_targets,
            all_targets=runtime_targets,
        )
    required_targets = [
        target
        for target in due_targets
        if not required_services or str(target.get("service") or "").strip() in required_services
    ]
    required_keys = [target_key(target) for target in required_targets]
    required_labels = {
        target_key(target): target_label(target)
        for target in required_targets
    }
    required_due_at = {
        target_key(target): due_at
        for target in required_targets
        if (due_at := target_latest_due_at(target)) is not None
    }
    target_services = {
        str(target.get("service") or "").strip()
        for target in required_targets
    }
    for service in required_services:
        if service not in target_services:
            required_keys.append(service)
            required_labels[service] = service
    if required_keys:
        max_reports = max(max_reports, min(200, len(required_keys) * 4))
    globs = _report_globs(since, now)
    if not globs:
        raise SystemExit("No heartbeat GCS report URI configured")

    objects: dict[str, tuple[str, dt.datetime]] = {}
    list_errors = []
    for gcs_glob in globs:
        try:
            for entry in _list_gcs_objects(gcs_glob, project=project):
                uri = _object_uri(entry)
                updated = _object_updated_at(entry)
                if uri and updated and updated >= since:
                    objects[uri] = (uri, updated)
        except RuntimeError as exc:
            list_errors.append(f"{gcs_glob}: {exc}")

    sorted_objects = sorted(objects.values(), key=lambda item: item[1], reverse=True)
    accepted: list[tuple[str, dt.datetime, str, str]] = []
    accepted_by_service: dict[str, tuple[str, dt.datetime, str, str]] = {}
    inspected = []
    for uri, updated in sorted_objects[:max_reports]:
        payload = _cat_gcs_json(uri, project=project)
        if payload is None:
            inspected.append(f"- {updated.isoformat()} {uri} unreadable")
            continue
        matches, service_name, filter_reason = _payload_matches(
            payload,
            required_services,
            required_targets=required_targets,
        )
        if not matches:
            inspected.append(f"- {updated.isoformat()} {uri} skipped {filter_reason}")
            continue
        latest_due_at = required_due_at.get(service_name)
        if latest_due_at is not None and updated < latest_due_at:
            inspected.append(
                f"- {updated.isoformat()} {uri} skipped "
                f"predates latest due schedule {latest_due_at.isoformat()}"
            )
            continue
        ok, reason = _is_accepted_report(payload)
        inspected.append(f"- {updated.isoformat()} {uri} {reason}")
        if ok:
            execution_backend = _report_execution_backend(payload)
            if required_keys:
                accepted_by_service.setdefault(
                    service_name,
                    (uri, updated, reason, execution_backend),
                )
            else:
                accepted.append((uri, updated, reason, execution_backend))

    if required_keys:
        missing = [key for key in required_keys if key not in accepted_by_service]
        if not missing:
            details = ", ".join(
                f"{required_labels[key]}@{accepted_by_service[key][1].isoformat()} "
                f"backend={accepted_by_service[key][3]}"
                for key in required_keys
            )
            print(f"Execution report heartbeat OK for {name}: {details}")
            return 0
    if accepted:
        uri, updated, reason, execution_backend = accepted[0]
        print(
            f"Execution report heartbeat OK for {name}: {reason}, updated={updated.isoformat()}, "
            f"backend={execution_backend}, uri={uri}"
        )
        return 0

    issues = []
    technical_details = []
    if list_errors:
        issues.append(_notice("heartbeat_list_failed"))
        technical_details.extend(list_errors[:3])
    if not sorted_objects:
        issues.append(_notice("heartbeat_no_recent_report", lookback_hours=f"{lookback_hours:g}"))
    elif required_keys:
        missing = [key for key in required_keys if key not in accepted_by_service]
        issues.append(
            _notice(
                "heartbeat_missing_acceptable_report",
                targets=", ".join(required_labels[key] for key in missing),
            )
        )
    else:
        issues.append(
            _notice(
                "heartbeat_no_acceptable_report",
                count=min(len(sorted_objects), max_reports),
            )
        )

    run_url = ""
    if os.environ.get("GITHUB_SERVER_URL") and os.environ.get("GITHUB_REPOSITORY") and os.environ.get("GITHUB_RUN_ID"):
        run_url = (
            f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
            f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        )
    message = format_operational_alert(
        locale=_notification_locale(),
        alert_type="execution_report_heartbeat",
        name=name,
        context={"lookback_hours": f"{lookback_hours:g}"},
        issues=issues,
        recent_reports=inspected[:max_reports],
        technical_details=technical_details,
        workflow_url=run_url,
    )
    print(message)
    _send_telegram(message[:3900])
    return 1 if fail_workflow else 0


if __name__ == "__main__":
    raise SystemExit(main())
