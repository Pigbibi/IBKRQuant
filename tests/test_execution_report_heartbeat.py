from __future__ import annotations

import subprocess
import datetime as dt
import json
import os

import pytest

from scripts import execution_report_heartbeat as heartbeat


def _clear_runtime_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith("RUNTIME_HEARTBEAT_") or name in {
            "CLOUD_RUN_SERVICE",
            "CLOUD_RUN_SERVICES",
            "CLOUD_RUN_SERVICE_TARGETS_JSON",
            "EXECUTION_REPORT_GCS_URI",
            "FIRSTRADE_GCS_STATE_BUCKET",
            "FIRSTRADE_STATE_PREFIX",
            "GCP_PROJECT_ID",
            "GOOGLE_CLOUD_PROJECT",
            "RUNTIME_TARGET_ENABLED",
            "RUNTIME_TARGET_JSON",
        }:
            monkeypatch.delenv(name, raising=False)


def test_explicit_required_services_override_target_derived_services(monkeypatch):
    monkeypatch.setenv("RUNTIME_HEARTBEAT_REQUIRED_SERVICES", "svc-daily-a,svc-daily-b")
    monkeypatch.setenv(
        "CLOUD_RUN_SERVICE_TARGETS_JSON",
        json.dumps(
            {
                "targets": [
                    {"service": "svc-daily-a"},
                    {"service": "svc-monthly"},
                ]
            }
        ),
    )

    assert heartbeat._load_required_services() == ["svc-daily-a", "svc-daily-b"]


def test_required_services_fall_back_to_cloud_run_targets(monkeypatch):
    monkeypatch.delenv("RUNTIME_HEARTBEAT_REQUIRED_SERVICES", raising=False)
    monkeypatch.setenv(
        "CLOUD_RUN_SERVICE_TARGETS_JSON",
        json.dumps(
            {
                "targets": [
                    {"service": "svc-a"},
                    {"runtime_target": {"service_name": "svc-b"}},
                    {"service": "svc-a"},
                ]
            }
        ),
    )

    assert heartbeat._load_required_services() == ["svc-a", "svc-b"]


def test_target_derived_required_services_skip_disabled_targets(monkeypatch):
    monkeypatch.delenv("RUNTIME_HEARTBEAT_REQUIRED_SERVICES", raising=False)
    monkeypatch.delenv("CLOUD_RUN_SERVICE", raising=False)
    monkeypatch.delenv("CLOUD_RUN_SERVICES", raising=False)
    monkeypatch.delenv("RUNTIME_HEARTBEAT_ACCOUNT_SCOPE", raising=False)
    monkeypatch.setenv(
        "CLOUD_RUN_SERVICE_TARGETS_JSON",
        json.dumps(
            {
                "targets": [
                    {
                        "service": "interactive-brokers-quant-disabled-service",
                        "RUNTIME_TARGET_ENABLED": "false",
                    },
                    {
                        "service": "interactive-brokers-quant-enabled-service",
                        "runtime_target": {
                            "service_name": "interactive-brokers-quant-enabled-service"
                        },
                    },
                    {
                        "service": "interactive-brokers-quant-disabled-nested-service",
                        "runtime_target": {
                            "runtime_target_enabled": "false",
                        },
                    },
                ]
            }
        ),
    )

    assert heartbeat._load_required_services() == [
        "interactive-brokers-quant-enabled-service"
    ]


def test_explicit_required_services_skip_disabled_targets(monkeypatch):
    monkeypatch.setenv(
        "RUNTIME_HEARTBEAT_REQUIRED_SERVICES",
        "interactive-brokers-enabled-service,interactive-brokers-disabled-service",
    )
    monkeypatch.setenv(
        "CLOUD_RUN_SERVICE_TARGETS_JSON",
        json.dumps(
            {
                "targets": [
                    {
                        "service": "interactive-brokers-enabled-service",
                        "RUNTIME_TARGET_ENABLED": "true",
                    },
                    {
                        "service": "interactive-brokers-disabled-service",
                        "RUNTIME_TARGET_ENABLED": "false",
                    },
                ]
            }
        ),
    )

    assert heartbeat._load_required_services() == [
        "interactive-brokers-enabled-service"
    ]


def test_all_explicit_required_services_disabled_skips(monkeypatch):
    monkeypatch.setenv(
        "RUNTIME_HEARTBEAT_REQUIRED_SERVICES",
        "interactive-brokers-disabled-service",
    )
    monkeypatch.setenv(
        "CLOUD_RUN_SERVICE_TARGETS_JSON",
        json.dumps(
            {
                "targets": [
                    {
                        "service": "interactive-brokers-disabled-service",
                        "RUNTIME_TARGET_ENABLED": "false",
                    }
                ]
            }
        ),
    )

    required, skip_reason, scheduler_checked = heartbeat._resolve_required_services(
        project="project-1",
        since=dt.datetime(2026, 6, 20, 0, 0, tzinfo=dt.timezone.utc),
        now=dt.datetime(2026, 6, 20, 1, 0, tzinfo=dt.timezone.utc),
    )

    assert required == []
    assert skip_reason == "all explicitly required heartbeat services are disabled"
    assert scheduler_checked is False


def test_scheduler_aware_required_services_only_include_due_main_schedulers(monkeypatch):
    monkeypatch.delenv("RUNTIME_HEARTBEAT_REQUIRED_SERVICES", raising=False)
    monkeypatch.setenv(
        "CLOUD_RUN_SERVICE_TARGETS_JSON",
        json.dumps(
            {
                "targets": [
                    {"service": "svc-daily"},
                    {"service": "svc-monthly"},
                ]
            }
        ),
    )
    monkeypatch.setattr(
        heartbeat,
        "_list_scheduler_jobs",
        lambda **_kwargs: [
            {
                "state": "ENABLED",
                "schedule": "45 15 * * 1-5",
                "timeZone": "America/New_York",
                "httpTarget": {"uri": "https://svc-daily.example.run.app/"},
            },
            {
                "state": "ENABLED",
                "schedule": "45 15 26 * *",
                "timeZone": "America/New_York",
                "httpTarget": {"uri": "https://svc-monthly.example.run.app/"},
            },
            {
                "state": "ENABLED",
                "schedule": "35 9,15 25-30 * *",
                "timeZone": "America/New_York",
                "httpTarget": {"uri": "https://svc-monthly.example.run.app/probe"},
            },
        ],
    )

    required = heartbeat._load_required_services(
        project="project-1",
        since=dt.datetime(2026, 6, 5, 0, 0, tzinfo=dt.timezone.utc),
        now=dt.datetime(2026, 6, 6, 2, 0, tzinfo=dt.timezone.utc),
    )

    assert required == ["svc-daily"]


def test_scheduler_aware_required_services_include_monthly_service_when_due(monkeypatch):
    monkeypatch.delenv("RUNTIME_HEARTBEAT_REQUIRED_SERVICES", raising=False)
    monkeypatch.setenv(
        "CLOUD_RUN_SERVICE_TARGETS_JSON",
        json.dumps({"targets": [{"service": "svc-monthly"}]}),
    )
    monkeypatch.setattr(
        heartbeat,
        "_list_scheduler_jobs",
        lambda **_kwargs: [
            {
                "state": "ENABLED",
                "schedule": "45 15 26 * *",
                "timeZone": "America/New_York",
                "httpTarget": {"uri": "https://svc-monthly.example.run.app/"},
            },
        ],
    )

    required = heartbeat._load_required_services(
        project="project-1",
        since=dt.datetime(2026, 6, 26, 19, 0, tzinfo=dt.timezone.utc),
        now=dt.datetime(2026, 6, 26, 20, 0, tzinfo=dt.timezone.utc),
    )

    assert required == ["svc-monthly"]


def test_scheduler_aware_required_services_fall_back_to_named_scheduler_describe(monkeypatch):
    monkeypatch.delenv("RUNTIME_HEARTBEAT_REQUIRED_SERVICES", raising=False)
    monkeypatch.setenv("CLOUD_RUN_SERVICE", "svc-monthly")
    monkeypatch.setattr(
        heartbeat,
        "_list_scheduler_jobs",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("cloudscheduler.jobs.list denied")),
    )
    monkeypatch.setattr(
        heartbeat,
        "_describe_scheduler_job",
        lambda job_name, **_kwargs: {
            "state": "ENABLED",
            "schedule": "45 15 26 * *",
            "timeZone": "America/New_York",
            "httpTarget": {"uri": "https://svc-monthly.example.run.app/"},
        }
        if job_name == "svc-monthly-scheduler"
        else None,
    )

    required, skip_reason, scheduler_checked = heartbeat._resolve_required_services(
        project="project-1",
        since=dt.datetime(2026, 6, 10, 0, 0, tzinfo=dt.timezone.utc),
        now=dt.datetime(2026, 6, 10, 2, 0, tzinfo=dt.timezone.utc),
    )

    assert required == []
    assert skip_reason and "no configured Cloud Scheduler main job was due" in skip_reason
    assert scheduler_checked is True


def test_scheduler_aware_named_fallback_uses_service_alias(monkeypatch):
    monkeypatch.delenv("RUNTIME_HEARTBEAT_REQUIRED_SERVICES", raising=False)
    monkeypatch.setenv("CLOUD_RUN_SERVICE", "interactive-brokers-live-u1599-tqqq-service")
    monkeypatch.setattr(
        heartbeat,
        "_list_scheduler_jobs",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("cloudscheduler.jobs.list denied")),
    )
    requested_job_names = []

    def fake_describe_scheduler_job(job_name, **_kwargs):
        requested_job_names.append(job_name)
        if job_name != "interactive-brokers-live-u1599-tqqq-scheduler":
            return None
        return {
            "state": "ENABLED",
            "schedule": "45 15 26 * *",
            "timeZone": "America/New_York",
            "httpTarget": {
                "uri": "https://interactive-brokers-live-u1599-tqqq-service.example.run.app/"
            },
        }

    monkeypatch.setattr(heartbeat, "_describe_scheduler_job", fake_describe_scheduler_job)

    required, skip_reason, scheduler_checked = heartbeat._resolve_required_services(
        project="project-1",
        since=dt.datetime(2026, 6, 10, 0, 0, tzinfo=dt.timezone.utc),
        now=dt.datetime(2026, 6, 10, 2, 0, tzinfo=dt.timezone.utc),
    )

    assert requested_job_names == [
        "interactive-brokers-live-u1599-tqqq-service-scheduler",
        "interactive-brokers-live-u1599-tqqq-scheduler",
    ]
    assert required == []
    assert skip_reason and "no configured Cloud Scheduler main job was due" in skip_reason
    assert scheduler_checked is True


def test_main_skips_when_no_scheduler_main_job_is_due(monkeypatch, capsys):
    monkeypatch.delenv("RUNTIME_HEARTBEAT_REQUIRED_SERVICES", raising=False)
    monkeypatch.setenv("GCP_PROJECT_ID", "interactivebrokersquant")
    monkeypatch.setenv("RUNTIME_HEARTBEAT_NAME", "Monthly runtime")
    monkeypatch.setenv("RUNTIME_HEARTBEAT_REPORT_PLATFORM", "interactive_brokers")
    monkeypatch.setenv("CLOUD_RUN_SERVICE", "ibkr-monthly-service")
    monkeypatch.setenv("RUNTIME_HEARTBEAT_GCS_URIS", "gs://bucket/execution-reports")
    monkeypatch.setattr(
        heartbeat,
        "_list_scheduler_jobs",
        lambda **_kwargs: [
            {
                "state": "ENABLED",
                "schedule": "45 15 26 * *",
                "timeZone": "America/New_York",
                "httpTarget": {"uri": "https://ibkr-monthly-service.example.run.app/"},
            },
        ],
    )
    monkeypatch.setattr(
        heartbeat,
        "_list_gcs_objects",
        lambda *_args, **_kwargs: pytest.fail("GCS should not be queried when no scheduler job is due"),
    )

    result = heartbeat.main(now=dt.datetime(2026, 6, 10, 1, 35, tzinfo=dt.timezone.utc))

    assert result == 0
    output = capsys.readouterr().out
    assert "Execution report heartbeat skipped for Monthly runtime" in output
    assert "no configured Cloud Scheduler main job was due" in output


def test_main_skips_when_runtime_target_is_disabled(monkeypatch, capsys):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("RUNTIME_HEARTBEAT_NAME", "Disabled runtime")
    monkeypatch.setenv("RUNTIME_TARGET_ENABLED", "false")
    monkeypatch.setattr(
        heartbeat,
        "_list_gcs_objects",
        lambda *_args, **_kwargs: pytest.fail("GCS should not be queried for disabled targets"),
    )

    result = heartbeat.main(now=dt.datetime(2026, 6, 20, 1, 35, tzinfo=dt.timezone.utc))

    assert result == 0
    output = capsys.readouterr().out
    assert "Execution report heartbeat skipped for Disabled runtime" in output
    assert "runtime target is disabled" in output


def test_main_skips_when_runtime_target_json_is_disabled(monkeypatch, capsys):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("RUNTIME_HEARTBEAT_NAME", "Disabled runtime")
    monkeypatch.setenv(
        "RUNTIME_TARGET_JSON",
        json.dumps({"runtime_target_enabled": False}),
    )
    monkeypatch.setattr(
        heartbeat,
        "_list_gcs_objects",
        lambda *_args, **_kwargs: pytest.fail("GCS should not be queried for disabled targets"),
    )

    result = heartbeat.main(now=dt.datetime(2026, 6, 20, 23, 10, tzinfo=dt.timezone.utc))

    assert result == 0
    output = capsys.readouterr().out
    assert "Execution report heartbeat skipped for Disabled runtime" in output
    assert "runtime target is disabled" in output


def test_main_skips_outside_runtime_target_scheduler_day_for_scoped_target(
    monkeypatch,
    capsys,
):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("RUNTIME_HEARTBEAT_NAME", "IBKR monthly runtime")
    monkeypatch.setenv("RUNTIME_HEARTBEAT_ACCOUNT_SCOPE", "live-monthly")
    monkeypatch.setenv(
        "CLOUD_RUN_SERVICE_TARGETS_JSON",
        json.dumps(
            {
                "targets": [
                    {
                        "service": "interactive-brokers-quant-live-daily-service",
                        "account_scope": "live-daily",
                        "runtime_target": {
                            "scheduler": {
                                "timezone": "America/New_York",
                                "main_time": "45 15 * * *",
                            }
                        },
                    },
                    {
                        "service": "interactive-brokers-quant-live-monthly-service",
                        "account_scope": "live-monthly",
                        "runtime_target": {
                            "scheduler": {
                                "timezone": "America/New_York",
                                "main_time": "45 15 1-7 * *",
                            }
                        },
                    },
                ]
            }
        ),
    )
    monkeypatch.setattr(
        heartbeat,
        "_list_gcs_objects",
        lambda *_args, **_kwargs: pytest.fail("GCS should not be queried outside scheduler window"),
    )

    result = heartbeat.main(now=dt.datetime(2026, 6, 20, 23, 10, tzinfo=dt.timezone.utc))

    assert result == 0
    output = capsys.readouterr().out
    assert "Execution report heartbeat skipped for IBKR monthly runtime" in output
    assert "interactive-brokers-quant-live-monthly-service" in output
    assert "expected day(s)=1,2,3,4,5,6,7" in output


def test_runtime_target_scheduler_does_not_skip_when_any_active_target_runs_daily(
    monkeypatch,
):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv(
        "CLOUD_RUN_SERVICE_TARGETS_JSON",
        json.dumps(
            {
                "targets": [
                    {
                        "service": "interactive-brokers-quant-live-daily-service",
                        "runtime_target": {
                            "scheduler": {
                                "timezone": "America/New_York",
                                "main_time": "45 15 * * *",
                            }
                        },
                    },
                    {
                        "service": "interactive-brokers-quant-live-monthly-service",
                        "runtime_target": {
                            "scheduler": {
                                "timezone": "America/New_York",
                                "main_time": "45 15 1-7 * *",
                            }
                        },
                    },
                ]
            }
        ),
    )

    now = dt.datetime(2026, 6, 20, 23, 10, tzinfo=dt.timezone.utc)
    reason = heartbeat._runtime_target_scheduler_skip_reason(
        now - dt.timedelta(hours=36),
        now,
    )

    assert reason is None


def test_runtime_target_scheduler_does_not_skip_when_lookback_includes_scheduler_day(
    monkeypatch,
):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv(
        "RUNTIME_TARGET_JSON",
        json.dumps(
            {
                "service_name": "interactive-brokers-quant-live-monthly-service",
                "scheduler": {
                    "timezone": "America/New_York",
                    "main_time": "45 15 1-7 * *",
                },
            }
        ),
    )

    reason = heartbeat._runtime_target_scheduler_skip_reason(
        dt.datetime(2026, 6, 7, 20, 0, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 6, 8, 20, 0, tzinfo=dt.timezone.utc),
    )

    assert reason is None


def test_main_rejects_previous_session_report_after_a_new_session_is_due(
    monkeypatch,
    capsys,
):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("RUNTIME_HEARTBEAT_NAME", "IBKR US runtime")
    monkeypatch.setenv("RUNTIME_HEARTBEAT_REQUIRED_SERVICES", "svc-us")
    monkeypatch.setenv("RUNTIME_HEARTBEAT_GCS_URIS", "gs://bucket/reports")
    monkeypatch.setenv(
        "RUNTIME_TARGET_JSON",
        json.dumps(
            {
                "service_name": "svc-us",
                "strategy_profile": "us-strategy",
                "account_scope": "US",
                "scheduler": {
                    "timezone": "America/New_York",
                    "main_time": "45 15 * * *",
                },
                "market": "US",
                "market_calendar": "NYSE",
                "market_timezone": "America/New_York",
            }
        ),
    )
    monkeypatch.setattr(
        heartbeat,
        "_list_gcs_objects",
        lambda *_args, **_kwargs: [
            {
                "url": "gs://bucket/reports/previous.json",
                "metadata": {"updated": "2026-07-28T19:46:00Z"},
            }
        ],
    )
    monkeypatch.setattr(
        heartbeat,
        "_cat_gcs_json",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "service_name": "svc-us",
            "strategy_profile": "us-strategy",
            "account_scope": "US",
        },
    )
    monkeypatch.setattr(heartbeat, "_send_telegram", lambda _message: True)

    result = heartbeat.main(
        now=dt.datetime(2026, 7, 29, 22, 20, tzinfo=dt.timezone.utc)
    )

    assert result == 1
    output = capsys.readouterr().out
    assert "missing acceptable report for runtime target(s)" in output
    assert "predates latest due schedule" in output


def test_report_with_blocked_execution_status_is_rejected_even_when_top_level_is_ok():
    accepted, reason = heartbeat._is_accepted_report(
        {
            "status": "ok",
            "summary": {
                "execution_status": "blocked",
                "no_op_reason": "no_equity",
            },
        }
    )

    assert accepted is False
    assert reason == "rejected execution_status=blocked"


@pytest.mark.parametrize(
    "no_op_reason",
    [
        "pending_orders_detected:AAA",
        "same_day_fills_detected:AAA",
        "same_day_execution_locked:mode=live",
    ],
)
def test_report_with_expected_execution_guard_is_accepted_when_top_level_is_ok(no_op_reason):
    accepted, reason = heartbeat._is_accepted_report(
        {
            "status": "ok",
            "summary": {
                "execution_status": "blocked",
                "no_op_reason": no_op_reason,
            },
        }
    )

    assert accepted is True
    assert reason == "status=ok"


def test_report_with_failed_notification_delivery_is_rejected():
    accepted, reason = heartbeat._is_accepted_report(
        {
            "status": "ok",
            "summary": {
                "notification_delivery_summary": {
                    "event_count": 1,
                    "sent_count": 0,
                    "failed_count": 1,
                    "all_acknowledged": False,
                }
            },
        }
    )

    assert accepted is False
    assert "notification delivery not acknowledged" in reason


def test_telegram_token_falls_back_to_secret_manager(monkeypatch):
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("TG_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_TOKEN_SECRET_NAME", "platform-telegram-token")
    monkeypatch.setenv("GCP_PROJECT_ID", "interactivebrokersquant")
    observed = {}

    def fake_run_gcloud(command):
        observed["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout="secret-token\n", stderr="")

    monkeypatch.setattr(heartbeat, "_run_gcloud", fake_run_gcloud)

    assert heartbeat._telegram_token() == "secret-token"
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
