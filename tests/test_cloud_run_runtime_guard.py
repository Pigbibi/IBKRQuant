from __future__ import annotations

import datetime as dt
import json
import re
import subprocess

from scripts import cloud_run_runtime_guard as guard


def test_runtime_guard_notification_language_is_configurable(monkeypatch):
    monkeypatch.setenv("NOTIFY_LANG", "zh-CN")

    assert guard._notice("runtime_guard_scheduler_log_query_failed") == "Cloud Scheduler 日志查询失败"


def test_cloud_run_log_filter_includes_region_when_available():
    log_filter = guard._cloud_run_log_filter(
        "interactive-brokers-live-u1599-tqqq-service",
        "2026-07-01T12:00:00Z",
        "asia-east1",
    )

    assert 'resource.labels.service_name="interactive-brokers-live-u1599-tqqq-service"' in log_filter
    assert 'resource.labels.location="asia-east1"' in log_filter
    assert 'timestamp >= "2026-07-01T12:00:00Z"' in log_filter


def test_scheduler_job_pattern_includes_service_alias():
    pattern = guard._scheduler_job_pattern_for_services(
        ["interactive-brokers-live-u1599-tqqq-service"]
    )

    assert re.search(pattern, "interactive-brokers-live-u1599-tqqq-service-scheduler")
    assert re.search(pattern, "interactive-brokers-live-u1599-tqqq-scheduler")
    assert not re.search(pattern, "interactive-brokers-live-u1660-soxl-scheduler")


def test_telegram_token_falls_back_to_secret_manager(monkeypatch):
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("TG_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_TOKEN_SECRET_NAME", "platform-telegram-token")
    monkeypatch.setenv("GCP_PROJECT_ID", "interactivebrokersquant")
    observed = {}

    def fake_run_gcloud(command):
        observed["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout="secret-token\n", stderr="")

    monkeypatch.setattr(guard, "_run_gcloud", fake_run_gcloud)

    assert guard._telegram_token() == "secret-token"
    assert observed["command"] == [
        "gcloud",
        "secrets",
        "versions",
        "access",
        "latest",
        "--secret",
        "platform-telegram-token",
        "--project",
        "interactivebrokersquant",
    ]


def test_cloud_run_log_query_retries_transient_google_error(monkeypatch):
    attempts = []
    sleeps = []

    def fake_run_gcloud(command):
        attempts.append(command)
        if len(attempts) == 1:
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr='HttpError: {"error": {"code": 500, "status": "INTERNAL"}}',
            )
        return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

    monkeypatch.setenv("RUNTIME_GUARD_LOG_QUERY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("RUNTIME_GUARD_LOG_QUERY_RETRY_SECONDS", "0")
    monkeypatch.setattr(guard, "_run_gcloud", fake_run_gcloud)
    monkeypatch.setattr(guard.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert guard._run_gcloud_logging("project-1", 'resource.type="cloud_run_revision"', 10) == []
    assert len(attempts) == 2
    assert sleeps == [0.0]


def test_cloud_run_log_query_does_not_retry_permission_error(monkeypatch):
    attempts = []

    def fake_run_gcloud(command):
        attempts.append(command)
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="ERROR: permission_denied (403)",
        )

    monkeypatch.setattr(guard, "_run_gcloud", fake_run_gcloud)

    try:
        guard._run_gcloud_logging("project-1", 'resource.type="cloud_run_revision"', 10)
    except RuntimeError as exc:
        assert "permission_denied" in str(exc)
    else:
        raise AssertionError("permission errors must fail without retry")
    assert len(attempts) == 1


def test_cloud_run_log_since_uses_latest_ready_revision(monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_REGION", "us-central1")
    observed = []

    def fake_run_gcloud(command):
        observed.append(command)
        if command[1:4] == ["run", "services", "describe"]:
            payload = {
                "status": {
                    "latestReadyRevisionName": "interactive-brokers-live-u1599-tqqq-service-00002"
                }
            }
        else:
            payload = {"metadata": {"creationTimestamp": "2026-07-01T06:50:04.123Z"}}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(guard, "_run_gcloud", fake_run_gcloud)

    fallback = dt.datetime(2026, 7, 1, 6, 0, tzinfo=dt.timezone.utc)
    result = guard._cloud_run_log_since(
        "interactivebrokersquant",
        "interactive-brokers-live-u1599-tqqq-service",
        fallback,
    )

    assert result == dt.datetime(2026, 7, 1, 6, 50, 4, 123000, tzinfo=dt.timezone.utc)
    assert observed[0] == [
        "gcloud",
        "run",
        "services",
        "describe",
        "interactive-brokers-live-u1599-tqqq-service",
        "--project",
        "interactivebrokersquant",
        "--region",
        "us-central1",
        "--format=json",
    ]
    assert observed[1][1:5] == [
        "run",
        "revisions",
        "describe",
        "interactive-brokers-live-u1599-tqqq-service-00002",
    ]


def test_region_for_service_prefers_target_region(monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_REGION", "us-central1")
    monkeypatch.setenv(
        "CLOUD_RUN_SERVICE_TARGETS_JSON",
        json.dumps(
            {
                "targets": [
                    {
                        "service": "interactive-brokers-live-u1599-tqqq-service",
                        "region": "asia-east1",
                    },
                ]
            }
        ),
    )

    assert guard._region_for_service("interactive-brokers-live-u1599-tqqq-service") == "asia-east1"


def test_load_services_ignores_disabled_runtime_targets(monkeypatch):
    monkeypatch.delenv("RUNTIME_GUARD_CLOUD_RUN_SERVICES", raising=False)
    monkeypatch.delenv("CLOUD_RUN_SERVICES", raising=False)
    monkeypatch.delenv("CLOUD_RUN_SERVICE", raising=False)
    monkeypatch.setenv(
        "CLOUD_RUN_SERVICE_TARGETS_JSON",
        json.dumps(
            {
                "targets": [
                    {"service": "enabled-service", "RUNTIME_TARGET_ENABLED": "true"},
                    {"service": "disabled-service", "RUNTIME_TARGET_ENABLED": "false"},
                    {"service": "disabled-lower-service", "runtime_target_enabled": "false"},
                ]
            }
        ),
    )

    assert guard._load_services() == ["enabled-service"]


def test_scheduler_entry_since_uses_matching_service_revision_window():
    fallback = dt.datetime(2026, 7, 1, 1, 0, tzinfo=dt.timezone.utc)
    service_since = dt.datetime(2026, 7, 1, 2, 0, tzinfo=dt.timezone.utc)
    entry = {"resource": {"labels": {"job_id": "enabled-service-scheduler"}}}

    assert (
        guard._scheduler_entry_since(entry, {"enabled-service": service_since}, fallback)
        == service_since
    )
    assert (
        guard._scheduler_entry_since(entry, {"other-service": service_since}, fallback)
        == fallback
    )


def test_scheduler_failure_matching_cloud_run_failure_is_duplicate():
    scheduler_entry = {
        "timestamp": "2026-07-29T19:45:03Z",
        "resource": {"labels": {"job_id": "test-scheduler"}},
    }
    cloud_run_failures = {
        "test-service": [
            {
                "timestamp": "2026-07-29T19:45:01Z",
                "resource": {"labels": {"service_name": "test-service"}},
            }
        ]
    }

    assert guard._is_duplicate_scheduler_failure(
        scheduler_entry,
        cloud_run_failures,
    )


def test_scheduler_failure_for_other_service_is_not_duplicate():
    scheduler_entry = {
        "timestamp": "2026-07-29T19:45:03Z",
        "resource": {"labels": {"job_id": "other-platform-scheduler"}},
    }
    cloud_run_failures = {
        "test-service": [
            {
                "timestamp": "2026-07-29T19:45:01Z",
                "resource": {"labels": {"service_name": "test-service"}},
            }
        ]
    }

    assert not guard._is_duplicate_scheduler_failure(
        scheduler_entry,
        cloud_run_failures,
    )


def test_services_without_success_are_reported_individually():
    assert guard._services_without_success(
        ["healthy-service", "silent-service"],
        {"healthy-service": 1, "silent-service": 0},
        {"healthy-service", "silent-service"},
    ) == ["silent-service"]


def test_monitor_dispatch_capacity_warning_is_not_failure_by_default(monkeypatch):
    monkeypatch.delenv("RUNTIME_GUARD_IGNORE_MONITOR_DISPATCH_CAPACITY_WARNINGS", raising=False)
    entry = {
        "severity": "WARNING",
        "httpRequest": {
            "status": 429,
            "requestUrl": "https://example.run.app/monitor-dispatch",
        },
        "textPayload": "The request was aborted because there was no available instance.",
    }

    assert guard._is_failure(entry) is False


def test_monitor_dispatch_capacity_warning_can_be_counted(monkeypatch):
    monkeypatch.setenv("RUNTIME_GUARD_IGNORE_MONITOR_DISPATCH_CAPACITY_WARNINGS", "false")
    entry = {
        "severity": "WARNING",
        "httpRequest": {
            "status": 429,
            "requestUrl": "https://example.run.app/monitor-dispatch",
        },
        "textPayload": "The request was aborted because there was no available instance.",
    }

    assert guard._is_failure(entry) is True


def test_strategy_request_capacity_warning_still_fails(monkeypatch):
    monkeypatch.delenv("RUNTIME_GUARD_IGNORE_MONITOR_DISPATCH_CAPACITY_WARNINGS", raising=False)
    entry = {
        "severity": "WARNING",
        "httpRequest": {
            "status": 429,
            "requestUrl": "https://example.run.app/dry-run",
        },
        "textPayload": "The request was aborted because there was no available instance.",
    }

    assert guard._is_failure(entry) is True


def test_scheduler_job_matching_rejects_prefixed_service_names():
    pattern = guard._scheduler_job_pattern_for_services(["test-service"])
    entry = {
        "timestamp": "2026-07-29T19:45:03Z",
        "resource": {
            "labels": {"job_id": "test-secondary-service-scheduler"}
        },
    }
    failures = {
        "test-service": [{"timestamp": "2026-07-29T19:45:01Z"}],
    }
    fallback = dt.datetime(2026, 7, 29, 19, 0, tzinfo=dt.timezone.utc)
    service_since = dt.datetime(2026, 7, 29, 19, 30, tzinfo=dt.timezone.utc)

    assert not re.search(pattern, "test-secondary-service-scheduler")
    assert guard._scheduler_entry_since(
        entry,
        {"test-service": service_since},
        fallback,
    ) == fallback
    assert guard._is_duplicate_scheduler_failure(entry, failures) is False


def test_explicit_disabled_service_is_removed_using_target_defaults(monkeypatch):
    monkeypatch.delenv("RUNTIME_GUARD_CLOUD_RUN_SERVICES", raising=False)
    monkeypatch.delenv("CLOUD_RUN_SERVICE", raising=False)
    monkeypatch.setenv("CLOUD_RUN_SERVICES", "enabled-service,disabled-service")
    monkeypatch.setenv(
        "CLOUD_RUN_SERVICE_TARGETS_JSON",
        json.dumps(
            {
                "defaults": {"runtime_target_enabled": False},
                "targets": [
                    {
                        "service": "enabled-service",
                        "runtime_target_enabled": True,
                    },
                    {"service": "disabled-service"},
                ],
            }
        ),
    )

    assert guard._load_services() == ["enabled-service"]
