import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

from application.ibkr_market_data import fetch_quote_snapshots_with_expected_error_summary


class FakeErrorEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self

    def emit(self, *args):
        for handler in tuple(self.handlers):
            handler(*args)


class FakeIB:
    def __init__(self):
        self.errorEvent = FakeErrorEvent()
        self.wrapper = type(
            "FakeWrapper",
            (),
            {
                "_logger": logging.getLogger("ib_insync.wrapper"),
                "_reqId2Contract": {},
            },
        )()


class FakeContract:
    def __init__(self, symbol):
        self.symbol = symbol


def emit_broker_error(ib, request_id, code, message, *, symbol=None):
    contract = FakeContract(symbol) if symbol else None
    if contract is not None:
        ib.wrapper._reqId2Contract[request_id] = contract
    ib.wrapper._logger.error("Error %s, reqId %s: %s", code, request_id, message)
    ib.errorEvent.emit(request_id, code, message, contract)


def test_expected_market_data_errors_are_collapsed_without_hiding_unrelated_errors(caplog):
    ib = FakeIB()
    summaries = []
    def fake_fetch(_ib, symbols, **_kwargs):
        for code in (10168, 10089):
            emit_broker_error(
                ib,
                1,
                code,
                "expected market data error",
                symbol="AAA",
            )
        emit_broker_error(ib, 1, 300, "Can't find EId with tickerId:1")
        emit_broker_error(ib, -1, 502, "unrelated gateway failure")
        return {symbol: object() for symbol in symbols}

    with caplog.at_level(logging.ERROR, logger="ib_insync.wrapper"):
        result = fetch_quote_snapshots_with_expected_error_summary(
            ib,
            ("AAA",),
            fetch_quote_snapshots=fake_fetch,
            printer=summaries.append,
        )

    assert set(result) == {"AAA"}
    assert "Error 10168" not in caplog.text
    assert "Error 10089" not in caplog.text
    assert "Error 300" not in caplog.text
    assert "Error 502" in caplog.text
    assert len(summaries) == 1
    assert '"10089": 1' in summaries[0]
    assert '"10168": 1' in summaries[0]
    assert '"300": 1' in summaries[0]


def test_standalone_error_300_is_not_reported_as_entitlement_fallback(caplog):
    ib = FakeIB()
    summaries = []
    def fake_fetch(_ib, symbols, **_kwargs):
        emit_broker_error(
            ib,
            1,
            300,
            "Can't find EId with tickerId:1",
            symbol="AAA",
        )
        return {symbol: object() for symbol in symbols}

    with caplog.at_level(logging.ERROR, logger="ib_insync.wrapper"):
        result = fetch_quote_snapshots_with_expected_error_summary(
            ib,
            ("AAA",),
            fetch_quote_snapshots=fake_fetch,
            printer=summaries.append,
        )

    assert set(result) == {"AAA"}
    assert "Error 300" in caplog.text
    assert summaries == []


def test_error_300_for_different_request_is_not_hidden(caplog):
    ib = FakeIB()
    summaries = []
    def fake_fetch(_ib, symbols, **_kwargs):
        emit_broker_error(
            ib,
            1,
            10168,
            "expected market data error",
            symbol="AAA",
        )
        emit_broker_error(
            ib,
            2,
            300,
            "unrelated request cancellation",
            symbol="BBB",
        )
        return {symbol: object() for symbol in symbols}

    with caplog.at_level(logging.ERROR, logger="ib_insync.wrapper"):
        result = fetch_quote_snapshots_with_expected_error_summary(
            ib,
            ("AAA", "BBB"),
            fetch_quote_snapshots=fake_fetch,
            printer=summaries.append,
        )

    assert set(result) == {"AAA", "BBB"}
    assert "Error 10168" not in caplog.text
    assert "Error 300, reqId 2" in caplog.text
    assert len(summaries) == 1
    assert '"10168": 1' in summaries[0]
    assert '"300"' not in summaries[0]


def test_other_ib_instance_logs_are_not_filtered(caplog):
    ib = FakeIB()
    other_ib = FakeIB()

    def fake_fetch(_ib, symbols, **_kwargs):
        emit_broker_error(
            other_ib,
            9,
            10168,
            "unrelated instance entitlement error",
            symbol="OTHER",
        )
        return {symbol: object() for symbol in symbols}

    with caplog.at_level(logging.ERROR, logger="ib_insync.wrapper"):
        fetch_quote_snapshots_with_expected_error_summary(
            ib,
            ("AAA",),
            fetch_quote_snapshots=fake_fetch,
            printer=lambda _message: None,
        )

    assert "unrelated instance entitlement error" in caplog.text


def test_different_ib_instances_fetch_concurrently():
    entered = set()
    entered_lock = Lock()
    both_entered = Event()

    def run_fetch(label):
        ib = FakeIB()

        def fake_fetch(_ib, symbols, **_kwargs):
            with entered_lock:
                entered.add(label)
                if len(entered) == 2:
                    both_entered.set()
            assert both_entered.wait(timeout=1.0)
            return {symbol: object() for symbol in symbols}

        return fetch_quote_snapshots_with_expected_error_summary(
            ib,
            (label,),
            fetch_quote_snapshots=fake_fetch,
            printer=lambda _message: None,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run_fetch, ("AAA", "BBB")))

    assert [set(result) for result in results] == [{"AAA"}, {"BBB"}]
