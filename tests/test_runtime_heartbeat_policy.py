from __future__ import annotations

import datetime as dt
import json

from scripts.runtime_heartbeat_policy import (
    filter_due_targets,
    load_runtime_targets,
    match_payload_target,
    target_key,
    target_latest_due_at,
)
from scripts import execution_report_heartbeat as heartbeat


def _target(
    *,
    service: str,
    strategy: str,
    scope: str,
    timezone: str,
    calendar: str,
) -> dict[str, object]:
    return {
        "service": service,
        "runtime_target": {
            "service_name": service,
            "strategy_profile": strategy,
            "account_scope": scope,
            "scheduler": {
                "timezone": timezone,
                "main_time": "45 15 * * *",
            },
            "market_calendar": calendar,
            "market_timezone": timezone,
        },
    }


def test_due_targets_use_each_strategy_market_calendar() -> None:
    targets = load_runtime_targets(
        {
            "CLOUD_RUN_SERVICE_TARGETS_JSON": json.dumps(
                {
                    "targets": [
                        _target(
                            service="svc-us",
                            strategy="us-strategy",
                            scope="US",
                            timezone="America/New_York",
                            calendar="NYSE",
                        ),
                        _target(
                            service="svc-hk",
                            strategy="hk-strategy",
                            scope="HK",
                            timezone="Asia/Hong_Kong",
                            calendar="XHKG",
                        ),
                    ]
                }
            )
        }
    )

    due, evaluated = filter_due_targets(
        targets,
        since=dt.datetime(2026, 7, 3, 0, 0, tzinfo=dt.timezone.utc),
        now=dt.datetime(2026, 7, 3, 22, 0, tzinfo=dt.timezone.utc),
        session_dates_loader=lambda calendar, **_kwargs: (
            {dt.date(2026, 7, 3)} if calendar == "XHKG" else set()
        ),
    )

    assert evaluated is True
    assert [target["strategy_profile"] for target in due] == ["hk-strategy"]


def test_real_exchange_calendars_distinguish_us_holiday_from_hk_session() -> None:
    targets = load_runtime_targets(
        {
            "CLOUD_RUN_SERVICE_TARGETS_JSON": json.dumps(
                {
                    "targets": [
                        _target(
                            service="svc-us",
                            strategy="us-strategy",
                            scope="US",
                            timezone="America/New_York",
                            calendar="NYSE",
                        ),
                        _target(
                            service="svc-hk",
                            strategy="hk-strategy",
                            scope="HK",
                            timezone="Asia/Hong_Kong",
                            calendar="XHKG",
                        ),
                    ]
                }
            )
        }
    )

    due, evaluated = filter_due_targets(
        targets,
        since=dt.datetime(2026, 7, 3, 0, 0, tzinfo=dt.timezone.utc),
        now=dt.datetime(2026, 7, 3, 22, 0, tzinfo=dt.timezone.utc),
    )

    assert evaluated is True
    assert [target["strategy_profile"] for target in due] == ["hk-strategy"]


def test_july_29_us_month_end_target_is_due_at_1545_eastern() -> None:
    raw_target = _target(
        service="svc-us-monthly",
        strategy="us-monthly",
        scope="US",
        timezone="America/New_York",
        calendar="NYSE",
    )
    raw_target["runtime_target"]["scheduler"]["main_time"] = "45 15 25-29 * *"
    targets = load_runtime_targets(
        {
            "CLOUD_RUN_SERVICE_TARGETS_JSON": json.dumps(
                {"targets": [raw_target]}
            )
        }
    )

    due, evaluated = filter_due_targets(
        targets,
        since=dt.datetime(2026, 7, 29, 19, 40, tzinfo=dt.timezone.utc),
        now=dt.datetime(2026, 7, 29, 20, 10, tzinfo=dt.timezone.utc),
    )

    assert evaluated is True
    assert [target["strategy_profile"] for target in due] == ["us-monthly"]
    assert target_latest_due_at(due[0]) == dt.datetime(
        2026,
        7,
        29,
        19,
        45,
        tzinfo=dt.timezone.utc,
    )


def test_neutral_daily_heartbeat_tracks_latest_due_time_per_market() -> None:
    targets = load_runtime_targets(
        {
            "CLOUD_RUN_SERVICE_TARGETS_JSON": json.dumps(
                {
                    "targets": [
                        _target(
                            service="svc-us",
                            strategy="us-strategy",
                            scope="US",
                            timezone="America/New_York",
                            calendar="NYSE",
                        ),
                        _target(
                            service="svc-hk",
                            strategy="hk-strategy",
                            scope="HK",
                            timezone="Asia/Hong_Kong",
                            calendar="XHKG",
                        ),
                    ]
                }
            )
        }
    )

    due, evaluated = filter_due_targets(
        targets,
        since=dt.datetime(2026, 7, 28, 10, 20, tzinfo=dt.timezone.utc),
        now=dt.datetime(2026, 7, 29, 22, 20, tzinfo=dt.timezone.utc),
        session_dates_loader=lambda _calendar, **_kwargs: {
            dt.date(2026, 7, 28),
            dt.date(2026, 7, 29),
        },
    )

    assert evaluated is True
    assert {
        target["strategy_profile"]: target_latest_due_at(target)
        for target in due
    } == {
        "us-strategy": dt.datetime(
            2026,
            7,
            29,
            19,
            45,
            tzinfo=dt.timezone.utc,
        ),
        "hk-strategy": dt.datetime(
            2026,
            7,
            29,
            7,
            45,
            tzinfo=dt.timezone.utc,
        ),
    }


def test_same_service_strategies_require_distinct_reports() -> None:
    targets = load_runtime_targets(
        {
            "CLOUD_RUN_SERVICE_TARGETS_JSON": json.dumps(
                {
                    "targets": [
                        _target(
                            service="shared-service",
                            strategy="strategy-a",
                            scope="US",
                            timezone="America/New_York",
                            calendar="NYSE",
                        ),
                        _target(
                            service="shared-service",
                            strategy="strategy-b",
                            scope="US",
                            timezone="America/New_York",
                            calendar="NYSE",
                        ),
                    ]
                }
            )
        }
    )

    matched, reason = match_payload_target(
        {
            "service_name": "shared-service",
            "strategy_profile": "strategy-a",
            "account_scope": "US",
        },
        targets,
    )

    assert matched == target_key(targets[0])
    assert reason == "matched runtime target"
    missing_strategy, _ = match_payload_target(
        {"service_name": "shared-service", "account_scope": "US"},
        targets,
    )
    assert missing_strategy is None

    matched_by_heartbeat, matched_key, _ = heartbeat._payload_matches(
        {
            "service_name": "shared-service",
            "strategy_profile": "strategy-a",
            "account_scope": "US",
        },
        ["shared-service"],
        required_targets=targets,
    )
    assert matched_by_heartbeat is True
    assert matched_key == target_key(targets[0])
    missing_by_heartbeat, _, _ = heartbeat._payload_matches(
        {"service_name": "shared-service", "account_scope": "US"},
        ["shared-service"],
        required_targets=targets,
    )
    assert missing_by_heartbeat is False


def test_scheduler_timezone_beats_account_region_when_market_is_not_explicit() -> None:
    targets = load_runtime_targets(
        {
            "CLOUD_RUN_SERVICE_TARGETS_JSON": json.dumps(
                {
                    "targets": [
                        {
                            "service": "longbridge-sg-us-service",
                            "account_scope": "SG",
                            "runtime_target": {
                                "service_name": "longbridge-sg-us-service",
                                "strategy_profile": "us-strategy",
                                "account_scope": "SG",
                                "scheduler": {
                                    "timezone": "America/New_York",
                                    "main_time": "45 15 * * *",
                                },
                            },
                        }
                    ]
                }
            )
        }
    )

    assert targets[0]["market"] == "US"
    assert targets[0]["market_calendar"] == "NYSE"
    assert targets[0]["market_timezone"] == "America/New_York"


def test_ambiguous_sg_account_does_not_guess_a_stock_exchange() -> None:
    targets = load_runtime_targets(
        {
            "CLOUD_RUN_SERVICE_TARGETS_JSON": json.dumps(
                {
                    "targets": [
                        {
                            "service": "longbridge-sg-service",
                            "account_scope": "SG",
                            "runtime_target": {
                                "service_name": "longbridge-sg-service",
                                "strategy_profile": "unknown-strategy",
                                "account_scope": "SG",
                                "scheduler": {
                                    "timezone": "UTC",
                                    "main_time": "0 12 * * *",
                                },
                            },
                        }
                    ]
                }
            )
        }
    )

    assert targets[0]["market"] == ""
    assert targets[0]["market_calendar"] == ""


def test_calendar_failure_keeps_target_due_fail_closed() -> None:
    targets = load_runtime_targets(
        {
            "RUNTIME_TARGET_JSON": json.dumps(
                _target(
                    service="svc-us",
                    strategy="us-strategy",
                    scope="US",
                    timezone="America/New_York",
                    calendar="INVALID",
                )["runtime_target"]
            )
        }
    )

    def fail_calendar(_calendar: str, **_kwargs: object) -> set[dt.date]:
        raise RuntimeError("calendar unavailable")

    due, evaluated = filter_due_targets(
        targets,
        since=dt.datetime(2026, 7, 3, 0, 0, tzinfo=dt.timezone.utc),
        now=dt.datetime(2026, 7, 3, 22, 0, tzinfo=dt.timezone.utc),
        session_dates_loader=fail_calendar,
        warning_logger=lambda _message: None,
    )

    assert due == targets
    assert evaluated is False
