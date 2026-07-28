from types import SimpleNamespace

import pytest

from application.runtime_broker_adapters import (
    IBKRGatewayUnavailableError,
    IBKRTradingPermissionError,
    build_runtime_broker_adapters,
)


def _build_adapters(*, account_ids=("U1234567",), execution_mode="paper"):
    return build_runtime_broker_adapters(
        host_resolver=lambda: "127.0.0.1",
        ib_port=4001,
        ib_client_id=11,
        connect_timeout_seconds=60,
        connect_attempts=1,
        connect_retry_delay_seconds=0,
        client_id_retry_offset=100,
        ensure_event_loop_fn=lambda: None,
        connect_ib_fn=lambda *_args, **_kwargs: SimpleNamespace(managedAccounts=lambda: ["U1234567"]),
        fetch_portfolio_snapshot_fn=lambda *_args, **_kwargs: None,
        fetch_quote_snapshots_fn=lambda *_args, **_kwargs: None,
        submit_order_intent_fn=lambda *_args, **_kwargs: None,
        application_get_market_prices_fn=lambda *_args, **_kwargs: None,
        application_check_order_submitted_fn=lambda *_args, **_kwargs: None,
        application_execute_rebalance_fn=lambda *_args, **_kwargs: None,
        execute_paper_liquidation_fn=lambda *_args, **_kwargs: None,
        translator=lambda key, **_kwargs: key,
        strategy_profile="global_etf_rotation",
        account_group="live-slot-a",
        service_name="interactive-brokers-live-slot-a-service",
        account_ids=account_ids,
        execution_mode=execution_mode,
        dry_run_only=False,
        cash_reserve_ratio=0.0,
        cash_reserve_floor_usd=0.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        quantity_step=1.0,
        min_order_notional=50.0,
        safe_haven_cash_substitute_threshold_usd=750.0,
        sell_settle_delay_sec=0.0,
        separator="---",
        strategy_display_name="Test Strategy",
        sleep_fn=lambda _seconds: None,
        printer=lambda *_args, **_kwargs: None,
    )


def test_connect_ib_accepts_configured_managed_account():
    adapters = _build_adapters(account_ids=("U1234567",))

    ib = adapters.connect_ib()

    assert ib.managedAccounts() == ["U1234567"]


def test_connect_ib_resolves_gateway_host_again_for_each_retry():
    observed = {"hosts": [], "refreshes": 0}
    current_host = {"value": "10.0.0.8"}

    def resolve_host():
        return current_host["value"]

    def refresh_host():
        observed["refreshes"] += 1
        current_host["value"] = "10.0.0.9"
        return current_host["value"]

    def connect(host, *_args, **_kwargs):
        observed["hosts"].append(host)
        if len(observed["hosts"]) == 1:
            raise ConnectionRefusedError("gateway restarting")
        return SimpleNamespace(managedAccounts=lambda: ["U1234567"])

    adapters = _build_adapters()
    adapters = adapters.__class__(
        **{
            **adapters.__dict__,
            "host_resolver": resolve_host,
            "refresh_host_fn": refresh_host,
            "connect_ib_fn": connect,
            "connect_attempts": 2,
        }
    )

    adapters.connect_ib()

    assert observed == {
        "hosts": ["10.0.0.8", "10.0.0.9"],
        "refreshes": 1,
    }


def test_connect_ib_raises_dedicated_error_after_retries_are_exhausted():
    adapters = _build_adapters()
    adapters = adapters.__class__(
        **{
            **adapters.__dict__,
            "connect_ib_fn": lambda *_args, **_kwargs: (_ for _ in ()).throw(
                TimeoutError("handshake timeout")
            ),
            "connect_attempts": 2,
        }
    )

    with pytest.raises(IBKRGatewayUnavailableError, match="unavailable after 2 attempt") as exc_info:
        adapters.connect_ib()

    assert isinstance(exc_info.value.__cause__, TimeoutError)


def test_connect_ib_rejects_configured_account_not_visible_to_gateway_username():
    observed = {"disconnects": 0}

    class FakeIB:
        def managedAccounts(self):
            return ["U7654321"]

        def disconnect(self):
            observed["disconnects"] += 1

    adapters = _build_adapters(account_ids=("U1234567",))
    adapters = adapters.__class__(
        **{
            **adapters.__dict__,
            "connect_ib_fn": lambda *_args, **_kwargs: FakeIB(),
        }
    )

    with pytest.raises(RuntimeError, match="Configured IBKR account_ids are not available"):
        adapters.connect_ib()

    assert observed["disconnects"] == 1


def test_connect_ib_rejects_live_mode_without_configured_account_ids():
    observed = {"disconnects": 0}

    class FakeIB:
        def managedAccounts(self):
            return ["U1234567"]

        def disconnect(self):
            observed["disconnects"] += 1

    adapters = _build_adapters(account_ids=(), execution_mode="live")
    adapters = adapters.__class__(
        **{
            **adapters.__dict__,
            "connect_ib_fn": lambda *_args, **_kwargs: FakeIB(),
        }
    )

    with pytest.raises(RuntimeError, match="IBKR live execution requires configured account_ids"):
        adapters.connect_ib()

    assert observed["disconnects"] == 1


def test_connect_ib_rejects_live_gateway_read_only_mode_without_retry():
    observed = {"disconnects": 0, "open_order_requests": 0}

    class FakeEvent:
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
        RaiseRequestErrors = False
        RequestTimeout = 0

        def __init__(self):
            self.errorEvent = FakeEvent()

        def managedAccounts(self):
            return ["U1234567"]

        def reqOpenOrders(self):
            observed["open_order_requests"] += 1
            self.errorEvent.emit(-1, 321, "API is in Read-Only mode", None)
            raise TimeoutError("open orders timed out")

        def disconnect(self):
            observed["disconnects"] += 1

    adapters = _build_adapters(account_ids=("U1234567",), execution_mode="live")
    adapters = adapters.__class__(
        **{
            **adapters.__dict__,
            "connect_ib_fn": lambda *_args, **_kwargs: FakeIB(),
            "connect_attempts": 3,
        }
    )

    with pytest.raises(IBKRTradingPermissionError, match="Read-Only"):
        adapters.connect_ib()

    assert observed == {
        "disconnects": 1,
        "open_order_requests": 1,
    }


def test_connect_ib_retries_when_trading_permission_probe_loses_connection():
    observed = {
        "connects": 0,
        "disconnects": 0,
        "open_order_requests": 0,
        "refreshes": 0,
    }

    class FakeEvent:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

        def __isub__(self, handler):
            self.handlers.remove(handler)
            return self

    class FakeIB:
        RaiseRequestErrors = False
        RequestTimeout = 0

        def __init__(self, attempt):
            self.attempt = attempt
            self.errorEvent = FakeEvent()

        def managedAccounts(self):
            return ["U1234567"]

        def reqOpenOrders(self):
            observed["open_order_requests"] += 1
            if self.attempt == 1:
                raise ConnectionError("gateway connection dropped")
            return []

        def disconnect(self):
            observed["disconnects"] += 1

    def connect(*_args, **_kwargs):
        observed["connects"] += 1
        return FakeIB(observed["connects"])

    def refresh_host():
        observed["refreshes"] += 1
        return "127.0.0.1"

    adapters = _build_adapters(account_ids=("U1234567",), execution_mode="live")
    adapters = adapters.__class__(
        **{
            **adapters.__dict__,
            "connect_ib_fn": connect,
            "connect_attempts": 2,
            "refresh_host_fn": refresh_host,
        }
    )

    assert adapters.connect_ib().managedAccounts() == ["U1234567"]
    assert observed == {
        "connects": 2,
        "disconnects": 1,
        "open_order_requests": 2,
        "refreshes": 1,
    }


def test_connect_ib_does_not_misclassify_unrelated_321_as_read_only():
    class FakeEvent:
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
        RaiseRequestErrors = False
        RequestTimeout = 0

        def __init__(self):
            self.errorEvent = FakeEvent()

        def managedAccounts(self):
            return ["U1234567"]

        def reqOpenOrders(self):
            self.errorEvent.emit(-1, 321, "Generic validation error", None)
            return []

    adapters = _build_adapters(account_ids=("U1234567",), execution_mode="live")
    adapters = adapters.__class__(
        **{
            **adapters.__dict__,
            "connect_ib_fn": lambda *_args, **_kwargs: FakeIB(),
        }
    )

    assert adapters.connect_ib().managedAccounts() == ["U1234567"]


def test_connect_ib_skips_trading_permission_probe_for_dry_run():
    class FakeIB:
        def managedAccounts(self):
            return ["U1234567"]

        def reqOpenOrders(self):
            pytest.fail("dry-run connection must not probe trading permissions")

    adapters = _build_adapters(account_ids=("U1234567",), execution_mode="live")
    adapters = adapters.__class__(
        **{
            **adapters.__dict__,
            "connect_ib_fn": lambda *_args, **_kwargs: FakeIB(),
            "dry_run_only": True,
        }
    )

    assert adapters.connect_ib().managedAccounts() == ["U1234567"]
