from __future__ import annotations

from datetime import datetime

import pytz

from entrypoints.cloud_run import is_market_open_now, is_market_open_today


def test_is_market_open_today_fails_closed_when_calendar_import_fails(monkeypatch):
    monkeypatch.setattr(
        "entrypoints.cloud_run.import_module",
        lambda _name: (_ for _ in ()).throw(TypeError("broken calendar")),
    )
    monkeypatch.setattr(
        "entrypoints.cloud_run.datetime",
        type(
            "FakeDatetime",
            (),
            {
                "now": staticmethod(
                    lambda _tz: datetime(2026, 4, 6, 12, 0, 0, tzinfo=pytz.timezone("America/New_York"))
                )
            },
        ),
    )

    assert is_market_open_today() is False


def test_is_market_open_now_fails_closed_when_calendar_import_fails(monkeypatch):
    monkeypatch.setattr(
        "entrypoints.cloud_run.import_module",
        lambda _name: (_ for _ in ()).throw(TypeError("broken calendar")),
    )
    monkeypatch.setattr(
        "entrypoints.cloud_run.datetime",
        type(
            "FakeDatetime",
            (),
            {
                "now": staticmethod(
                    lambda _tz: datetime(2026, 4, 6, 12, 0, 0, tzinfo=pytz.timezone("America/New_York"))
                )
            },
        ),
    )

    assert is_market_open_now() is False


def test_is_market_open_now_returns_false_before_regular_session(monkeypatch):
    market_timezone = pytz.timezone("America/New_York")
    premarket_time = market_timezone.localize(datetime(2026, 4, 6, 2, 0, 0))
    monkeypatch.setattr(
        "entrypoints.cloud_run.datetime",
        type(
            "FakeDatetime",
            (),
            {"now": staticmethod(lambda _tz: premarket_time)},
        ),
    )

    assert is_market_open_now() is False
