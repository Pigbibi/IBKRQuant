#!/usr/bin/env python3
"""Check Cloud Scheduler and Cloud Run logs, then notify Telegram on failures."""

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


ERROR_SEVERITIES = {"ERROR", "CRITICAL", "ALERT", "EMERGENCY"}
FAILURE_WORDS = (
    "DEADLINE_EXCEEDED",
    "INTERNAL_ERROR",
    "PERMISSION_DENIED",
    "UNAUTHENTICATED",
    "URL_ERROR",
    "URL_UNREACHABLE",
)
SCHEDULER_CLOUD_RUN_DEDUP_SECONDS = 120


def _split_values(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in re.split(r"[,;\n]+", raw) if part.strip()]


def _env_bool(name: str, default: bool = False) -> bool:
    value = (os.environ.get(name) or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on"}


def _load_services() -> list[str]:
    services = []
    for name in (
        "RUNTIME_GUARD_CLOUD_RUN_SERVICES",
        "CLOUD_RUN_SERVICES",
        "CLOUD_RUN_SERVICE",
    ):
        services.extend(_split_values(os.environ.get(name)))

    raw_targets = (os.environ.get("CLOUD_RUN_SERVICE_TARGETS_JSON") or "").strip()
    if raw_targets:
        try:
            payload = json.loads(raw_targets)
            targets = payload.get("targets") if isinstance(payload, dict) else payload
            if isinstance(targets, list):
                for target in targets:
                    if not isinstance(target, dict):
                        continue
                    if not _target_enabled(target):
                        continue
                    runtime_target = target.get("runtime_target") or target.get(
                        "runtime_target_json"
                    )
                    if isinstance(runtime_target, str):
                        try:
                            runtime_target = json.loads(runtime_target)
                        except json.JSONDecodeError:
                            runtime_target = {}
                    for key in ("service", "service_name", "cloud_run_service"):
                        value = target.get(key) or (
                            runtime_target.get(key)
                            if isinstance(runtime_target, dict)
                            else None
                        )
                        if value:
                            services.extend(_split_values(str(value)))
                            break
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"CLOUD_RUN_SERVICE_TARGETS_JSON is invalid: {exc}") from exc

    seen = set()
    unique = []
    for service in services:
        if service not in seen:
            seen.add(service)
            unique.append(service)
    return unique


def _cloud_run_log_filter(service: str, since_text: str, region: str = "") -> str:
    parts = [
        'resource.type="cloud_run_revision"',
        f'resource.labels.service_name="{service}"',
        f'timestamp >= "{since_text}"',
    ]
    if region:
        parts.append(f'resource.labels.location="{region}"')
    return " AND ".join(parts)


def _service_job_aliases(service: str) -> list[str]:
    service_name = str(service or "").strip()
    if not service_name:
        return []
    aliases = [service_name]
    if service_name.endswith("-service"):
        aliases.append(service_name.removesuffix("-service"))
    return list(dict.fromkeys(aliases))


def _scheduler_job_pattern_for_services(services: list[str]) -> str:
    candidates: list[str] = []
    for service in services:
        candidates.extend(_service_job_aliases(service))
    unique = list(dict.fromkeys(candidates))
    return "|".join(re.escape(candidate) for candidate in unique)


def _entry_job_name(entry: dict[str, Any]) -> str:
    labels = _labels(entry)
    return str(labels.get("job_id") or labels.get("job_name") or "")


def _scheduler_entry_since(
    entry: dict[str, Any],
    service_since_by_name: dict[str, dt.datetime],
    fallback: dt.datetime,
) -> dt.datetime:
    job_name = _entry_job_name(entry)
    matches = [
        service_since
        for service, service_since in service_since_by_name.items()
        if any(alias and alias in job_name for alias in _service_job_aliases(service))
    ]
    return max(matches) if matches else fallback


def _is_duplicate_scheduler_failure(
    entry: dict[str, Any],
    cloud_run_failures_by_service: dict[str, list[dict[str, Any]]],
) -> bool:
    scheduler_timestamp = _parse_timestamp(entry.get("timestamp"))
    job_name = _entry_job_name(entry)
    if scheduler_timestamp is None or not job_name:
        return False

    tolerance = dt.timedelta(seconds=SCHEDULER_CLOUD_RUN_DEDUP_SECONDS)
    for service, failures in cloud_run_failures_by_service.items():
        if not any(alias and alias in job_name for alias in _service_job_aliases(service)):
            continue
        for failure in failures:
            cloud_run_timestamp = _parse_timestamp(failure.get("timestamp"))
            if (
                cloud_run_timestamp is not None
                and abs(scheduler_timestamp - cloud_run_timestamp) <= tolerance
            ):
                return True
    return False


def _services_without_success(
    services: list[str],
    success_count_by_service: dict[str, int],
    queried_services: set[str],
) -> list[str]:
    return [
        service
        for service in services
        if service in queried_services and success_count_by_service.get(service, 0) == 0
    ]


def _run_gcloud(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False)


def _run_gcloud_json(args: list[str], context: str) -> Any:
    result = _run_gcloud(args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or f"gcloud {context} failed")
    if not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gcloud {context} returned invalid JSON: {exc}") from exc


def _run_gcloud_logging(project: str, log_filter: str, limit: int) -> list[dict[str, Any]]:
    command = [
        "gcloud",
        "logging",
        "read",
        log_filter,
        "--project",
        project,
        "--format=json",
        f"--limit={limit}",
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or "gcloud logging read failed")
    if not result.stdout.strip():
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gcloud returned invalid JSON: {exc}") from exc
    return payload if isinstance(payload, list) else []


def _parse_timestamp(value: Any) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _format_timestamp(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _target_payloads() -> list[dict[str, Any]]:
    raw_targets = (os.environ.get("CLOUD_RUN_SERVICE_TARGETS_JSON") or "").strip()
    if not raw_targets:
        return []
    try:
        payload = json.loads(raw_targets)
    except json.JSONDecodeError:
        return []
    targets = payload.get("targets") if isinstance(payload, dict) else payload
    if not isinstance(targets, list):
        return []
    return [target for target in targets if isinstance(target, dict)]


def _runtime_target(target: dict[str, Any]) -> dict[str, Any]:
    runtime_target = target.get("runtime_target") or target.get("runtime_target_json")
    if isinstance(runtime_target, str):
        try:
            runtime_target = json.loads(runtime_target)
        except json.JSONDecodeError:
            runtime_target = {}
    return runtime_target if isinstance(runtime_target, dict) else {}


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on"}


def _target_enabled(target: dict[str, Any]) -> bool:
    runtime_target = _runtime_target(target)
    for key in ("runtime_target_enabled", "RUNTIME_TARGET_ENABLED"):
        if key in target:
            return _coerce_bool(target.get(key), True)
        if key in runtime_target:
            return _coerce_bool(runtime_target.get(key), True)
    return True


def _target_service_names(target: dict[str, Any]) -> list[str]:
    runtime_target = _runtime_target(target)
    for key in ("service", "service_name", "cloud_run_service"):
        value = target.get(key) or runtime_target.get(key)
        if value:
            return _split_values(str(value))
    return []


def _region_for_service(service: str) -> str:
    for target in _target_payloads():
        if service not in _target_service_names(target):
            continue
        runtime_target = _runtime_target(target)
        for key in ("region", "cloud_run_region", "location"):
            value = target.get(key) or runtime_target.get(key)
            if value:
                return str(value).strip()
    return (
        os.environ.get("RUNTIME_GUARD_CLOUD_RUN_REGION")
        or os.environ.get("CLOUD_RUN_REGION")
        or os.environ.get("CLOUD_RUN_LOCATION")
        or os.environ.get("GOOGLE_CLOUD_REGION")
        or ""
    ).strip()


def _latest_ready_revision_started_at(project: str, service: str) -> dt.datetime | None:
    region = _region_for_service(service)
    if not region:
        return None

    service_payload = _run_gcloud_json(
        [
            "gcloud",
            "run",
            "services",
            "describe",
            service,
            "--project",
            project,
            "--region",
            region,
            "--format=json",
        ],
        f"run services describe {service}",
    )
    if not isinstance(service_payload, dict):
        return None
    status = service_payload.get("status") or {}
    if not isinstance(status, dict):
        return None
    revision = str(status.get("latestReadyRevisionName") or "").strip()
    if not revision:
        return None

    revision_payload = _run_gcloud_json(
        [
            "gcloud",
            "run",
            "revisions",
            "describe",
            revision,
            "--project",
            project,
            "--region",
            region,
            "--format=json",
        ],
        f"run revisions describe {revision}",
    )
    if not isinstance(revision_payload, dict):
        return None
    metadata = revision_payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        return None
    return _parse_timestamp(metadata.get("creationTimestamp"))


def _cloud_run_log_since(project: str, service: str, fallback: dt.datetime) -> dt.datetime:
    try:
        revision_start = _latest_ready_revision_started_at(project, service)
    except RuntimeError as exc:
        print(
            f"Unable to resolve latest ready revision for {service}; using lookback window: {exc}",
            file=sys.stderr,
        )
        return fallback
    if revision_start and revision_start > fallback:
        return revision_start
    return fallback


def _status(entry: dict[str, Any]) -> int | None:
    value = (entry.get("httpRequest") or {}).get("status")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _entry_text(entry: dict[str, Any]) -> str:
    chunks = []
    for key in ("textPayload", "message"):
        value = entry.get(key)
        if value:
            chunks.append(str(value))
    for key in ("jsonPayload", "protoPayload"):
        value = entry.get(key)
        if value:
            chunks.append(json.dumps(value, sort_keys=True))
    return " ".join(chunks)


def _request_path(entry: dict[str, Any]) -> str:
    request_url = str((entry.get("httpRequest") or {}).get("requestUrl") or "").strip()
    if not request_url:
        return ""
    return urllib.parse.urlparse(request_url).path


def _is_ignorable_monitor_dispatch_capacity_warning(entry: dict[str, Any]) -> bool:
    if not _env_bool("RUNTIME_GUARD_IGNORE_MONITOR_DISPATCH_CAPACITY_WARNINGS", True):
        return False
    return (
        _status(entry) == 429
        and _request_path(entry) == "/monitor-dispatch"
        and "NO AVAILABLE INSTANCE" in _entry_text(entry).upper()
    )


def _is_failure(entry: dict[str, Any]) -> bool:
    if _is_ignorable_monitor_dispatch_capacity_warning(entry):
        return False
    severity = str(entry.get("severity") or "").upper()
    status = _status(entry)
    text = _entry_text(entry).upper()
    return (
        severity in ERROR_SEVERITIES
        or (status is not None and status >= 400)
        or any(word in text for word in FAILURE_WORDS)
    )


def _is_success(entry: dict[str, Any]) -> bool:
    status = _status(entry)
    return status is not None and 200 <= status < 400


def _labels(entry: dict[str, Any]) -> dict[str, Any]:
    resource = entry.get("resource") or {}
    labels = resource.get("labels") or {}
    return labels if isinstance(labels, dict) else {}


def _summarize(entry: dict[str, Any]) -> str:
    labels = _labels(entry)
    target = labels.get("service_name") or labels.get("job_id") or labels.get("job_name")
    timestamp = str(entry.get("timestamp") or "-")
    severity = str(entry.get("severity") or "-")
    status = _status(entry)
    status_text = f" status={status}" if status is not None else ""
    text = re.sub(r"\s+", " ", _entry_text(entry)).strip()
    if len(text) > 180:
        text = text[:177] + "..."
    suffix = f" {text}" if text else ""
    return f"- {timestamp} {target or '<unknown>'} severity={severity}{status_text}{suffix}"




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
        print("No Telegram token/chat configured; unable to send runtime guard alert.", file=sys.stderr)
        return False

    ok = True
    base_url = "https://api.telegram.org"
    for token_value, chat_id in unique_targets:
        body = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/bot{token_value}/sendMessage",
            data=body,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status >= 400:
                    ok = False
                    print(f"Telegram returned HTTP {response.status}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"Telegram send failed: {type(exc).__name__}", file=sys.stderr)
    return ok


def main() -> int:
    project = (
        os.environ.get("RUNTIME_GUARD_GCP_PROJECT_ID")
        or os.environ.get("GCP_PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
    )
    if not project:
        raise SystemExit("GCP_PROJECT_ID or GOOGLE_CLOUD_PROJECT is required")

    name = os.environ.get("RUNTIME_GUARD_NAME") or os.environ.get("GITHUB_REPOSITORY") or "Cloud Run"
    lookback_minutes = int(os.environ.get("RUNTIME_GUARD_LOOKBACK_MINUTES") or "180")
    limit = int(os.environ.get("RUNTIME_GUARD_LOG_LIMIT") or "200")
    require_success = _env_bool("RUNTIME_GUARD_REQUIRE_SUCCESS", False)
    fail_workflow = _env_bool("RUNTIME_GUARD_FAIL_WORKFLOW_ON_ALERT", True)
    check_scheduler = _env_bool("RUNTIME_GUARD_CHECK_SCHEDULER", True)
    ignore_pre_ready_logs = _env_bool("RUNTIME_GUARD_IGNORE_PRE_READY_REVISION_LOGS", True)

    since = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=lookback_minutes)
    ).replace(microsecond=0)
    since_text = since.isoformat().replace("+00:00", "Z")

    issues: list[str] = []
    details: list[str] = []
    success_count = 0
    success_count_by_service: dict[str, int] = {}
    queried_services: set[str] = set()
    cloud_run_failures_by_service: dict[str, list[dict[str, Any]]] = {}
    service_since_by_name: dict[str, dt.datetime] = {}

    try:
        services = _load_services()
    except RuntimeError as exc:
        services = []
        issues.append(f"service configuration error: {exc}")
    scheduler_pattern = (
        os.environ.get("RUNTIME_GUARD_SCHEDULER_JOB_PATTERN")
        or _scheduler_job_pattern_for_services(services)
    )

    for service in services:
        service_since = _cloud_run_log_since(project, service, since) if ignore_pre_ready_logs else since
        service_since_by_name[service] = service_since
        service_since_text = _format_timestamp(service_since)
        log_filter = _cloud_run_log_filter(service, service_since_text, _region_for_service(service))
        try:
            entries = _run_gcloud_logging(project, log_filter, limit)
        except RuntimeError as exc:
            issues.append(f"Cloud Run log query failed for {service}: {exc}")
            continue
        queried_services.add(service)
        failures = [entry for entry in entries if _is_failure(entry)]
        cloud_run_failures_by_service[service] = failures
        service_success_count = sum(1 for entry in entries if _is_success(entry))
        success_count_by_service[service] = service_success_count
        success_count += service_success_count
        if failures:
            issues.append(f"{len(failures)} Cloud Run failure log(s) for {service}")
            details.extend(_summarize(entry) for entry in failures[:5])

    if services and require_success:
        for service in _services_without_success(
            services,
            success_count_by_service,
            queried_services,
        ):
            issues.append(
                f"no successful Cloud Run request found for {service} "
                f"in the last {lookback_minutes} minutes"
            )

    if check_scheduler and scheduler_pattern:
        log_filter = f'resource.type="cloud_scheduler_job" AND timestamp >= "{since_text}"'
        try:
            entries = _run_gcloud_logging(project, log_filter, limit)
            if scheduler_pattern:
                regex = re.compile(scheduler_pattern)
                entries = [
                    entry
                    for entry in entries
                    if regex.search(str(_labels(entry).get("job_id") or _labels(entry).get("job_name") or ""))
                ]
            failures = []
            for entry in entries:
                if not _is_failure(entry):
                    continue
                entry_timestamp = _parse_timestamp(entry.get("timestamp"))
                entry_since = _scheduler_entry_since(entry, service_since_by_name, since)
                if entry_timestamp and entry_timestamp < entry_since:
                    continue
                if _is_duplicate_scheduler_failure(
                    entry,
                    cloud_run_failures_by_service,
                ):
                    continue
                failures.append(entry)
            if failures:
                issues.append(f"{len(failures)} Cloud Scheduler failure log(s)")
                details.extend(_summarize(entry) for entry in failures[:5])
        except RuntimeError as exc:
            issues.append(f"Cloud Scheduler log query failed: {exc}")
    elif check_scheduler:
        print("Skipping Cloud Scheduler check because no scheduler job pattern could be derived.", file=sys.stderr)

    if not issues:
        service_text = ", ".join(services) if services else "<none configured>"
        print(
            f"Runtime guard OK for {name}: services={service_text}, lookback={lookback_minutes}m, successes={success_count}"
        )
        return 0

    run_url = ""
    if os.environ.get("GITHUB_SERVER_URL") and os.environ.get("GITHUB_REPOSITORY") and os.environ.get("GITHUB_RUN_ID"):
        run_url = (
            f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
            f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        )
    message_lines = [
        f"[Runtime Guard] {name}",
        f"Project: {project}",
        f"Lookback: {lookback_minutes} minutes",
        "Issues:",
        *[f"- {issue}" for issue in issues],
    ]
    if details:
        message_lines.extend(["Details:", *details[:10]])
    if run_url:
        message_lines.append(f"Workflow: {run_url}")
    message = "\n".join(message_lines)
    print(message)
    _send_telegram(message[:3900])
    return 1 if fail_workflow else 0


if __name__ == "__main__":
    raise SystemExit(main())
