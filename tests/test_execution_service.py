from types import SimpleNamespace

from application.execution_service import check_order_submitted, execute_rebalance, get_available_buying_power
from notifications.telegram import build_translator
from quant_platform_kit.common.models import OrderIntent


def _weight_allocation(targets, *, risk_symbols=(), income_symbols=(), safe_haven_symbols=()):
    ordered_symbols = tuple(targets.keys())
    return {
        "target_mode": "weight",
        "strategy_symbols": ordered_symbols,
        "risk_symbols": tuple(risk_symbols),
        "income_symbols": tuple(income_symbols),
        "safe_haven_symbols": tuple(safe_haven_symbols),
        "targets": dict(targets),
    }


def _signal_metadata(
    targets,
    *,
    risk_symbols=(),
    income_symbols=(),
    safe_haven_symbols=(),
    **extra,
):
    payload = dict(extra)
    payload["allocation"] = _weight_allocation(
        targets,
        risk_symbols=risk_symbols,
        income_symbols=income_symbols,
        safe_haven_symbols=safe_haven_symbols,
    )
    return payload


def translate(key, **kwargs):
    templates = {
        "submitted": "submitted {order_id}",
        "failed": "failed {reason}",
        "market_sell": "sell {symbol} {qty}",
        "limit_buy": "buy {symbol} {qty} @{price}",
        "target_diff": "target_diff {symbol}: current={current} target={target} delta={delta}",
        "execution_profile_detail": "profile={profile}",
        "regime_detail": "regime={value}",
        "breadth_detail": "breadth={value}",
        "target_stock_detail": "target_stock={value}",
        "realized_stock_detail": "realized_stock={value}",
        "snapshot_as_of_detail": "snapshot_as_of={value}",
        "trade_date_detail": "trade_date={value}",
        "pending_orders_detected": "pending_orders_detected profile={profile} symbols={symbols}",
        "same_day_fills_detected": "same_day_fills_detected profile={profile} mode={mode} symbols={symbols} trade_date={trade_date}",
        "same_day_execution_locked": "same_day_execution_locked profile={profile} mode={mode} trade_date={trade_date} snapshot_date={snapshot_date} target_hash={target_hash} lock_path={lock_path}",
        "execution_lock_acquired": "execution_lock_acquired mode={mode} trade_date={trade_date} snapshot_date={snapshot_date} lock_path={lock_path}",
        "dry_run_snapshot_prices": "dry_run_snapshot_prices count={count} symbols={symbols}",
        "price_fallback_prices": "price_fallback_prices count={count} symbols={symbols}",
        "no_equity": "❌ No equity",
        "cash_label": "现金",
        "buy_deferred": "ℹ️ [买入说明] {detail}",
        "buy_deferred_small_account_cash_substitution": (
            "{symbol} 目标金额 ${diff} 低于 1 股价格 ${price}；"
            "为避免超过目标仓位，小账户本轮保留现金，不回补 {cash_symbols}"
        ),
        "small_account_allocation_drift": "📏 整数股偏离：若本轮订单全部成交，{details}",
        "small_account_allocation_drift_detail": (
            "{symbol} 预计 {projected_weight} vs 目标 {target_weight}（{drift_weight}）"
        ),
    }
    template = templates[key]
    return template.format(**kwargs) if kwargs else template


def test_check_order_submitted_accepts_submitted_like_status():
    report = SimpleNamespace(broker_order_id="123", status="Submitted")
    ok, message = check_order_submitted(report, translator=translate)
    assert ok is True
    assert "submitted 123" in message


def test_check_order_submitted_warns_submitted_order_is_not_fill_confirmation():
    report = SimpleNamespace(broker_order_id="123", status="Submitted")
    ok, message = check_order_submitted(report, translator=build_translator("zh"))
    assert ok is True
    assert "状态: Submitted" in message
    assert "仍可能被拒单或取消" in message
    assert "尚未确认券商接受或成交" in message
    assert "自动取消" in message


def test_check_order_submitted_accepts_pending_submit_status():
    report = SimpleNamespace(broker_order_id="123", status="PendingSubmit")
    ok, message = check_order_submitted(report, translator=translate)
    assert ok is True
    assert "submitted 123" in message


def test_get_available_buying_power_prefers_usd_cash_balance_over_aggregate_available_funds():
    class FakeIB:
        def accountValues(self):
            return [
                SimpleNamespace(tag="AvailableFunds", currency="USD", value="885.99"),
                SimpleNamespace(tag="CashBalance", currency="HKD", value="408.98"),
                SimpleNamespace(tag="CashBalance", currency="USD", value="477.10"),
            ]

    assert get_available_buying_power(FakeIB(), 885.99, currency="USD") == 477.10


def test_get_available_buying_power_supports_ibkr_ledger_cash_balance():
    class FakeIB:
        def accountValues(self):
            return [
                SimpleNamespace(tag="AvailableFunds", currency="USD", value="885.99"),
                SimpleNamespace(tag="$LEDGER-CashBalance", currency="USD", value="477.10"),
            ]

    assert get_available_buying_power(FakeIB(), 885.99, currency="USD") == 477.10


def test_get_available_buying_power_does_not_treat_total_cash_value_as_currency_cash():
    class FakeIB:
        def accountValues(self):
            return [
                SimpleNamespace(tag="TotalCashValue", currency="USD", value="500.00"),
            ]

    assert get_available_buying_power(FakeIB(), 0.0, currency="USD") == 0.0


def test_execute_rebalance_submits_limit_buy_for_underweight_position(monkeypatch, tmp_path):
    class FakeIB:
        def openTrades(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="5000")]

    submitted = []

    def fake_submit_order_intent(_ib, intent):
        submitted.append(intent)
        return SimpleNamespace(broker_order_id="1", status="Submitted")

    def fake_fetch_quote_snapshots(_ib, symbols):
        return {symbol: SimpleNamespace(last_price=100.0) for symbol in symbols}

    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    trade_logs = execute_rebalance(
        FakeIB(),
        {"VOO": 1.0},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        fetch_quote_snapshots=fake_fetch_quote_snapshots,
        submit_order_intent=fake_submit_order_intent,
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO", "BIL"],
        strategy_profile="tech_communication_pullback_enhancement",
        signal_metadata=_signal_metadata(
            {"VOO": 1.0},
            risk_symbols=("VOO",),
            safe_haven_symbols=("BIL",),
            regime="risk_on",
            breadth_ratio=0.6,
            target_stock_weight=0.8,
            realized_stock_weight=0.8,
            trade_date="2026-04-01",
            snapshot_as_of="2026-03-31",
        ),
        dry_run_only=False,
        cash_reserve_ratio=0.03,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
    )

    assert len(submitted) == 1
    assert submitted[0].side == "buy"
    assert submitted[0].symbol == "VOO"
    assert submitted[0].order_type == "limit"
    assert any(log.startswith("buy VOO") for log in trade_logs)


def test_execute_rebalance_blocks_when_all_order_submissions_are_rejected(tmp_path):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="CashBalance", currency="USD", value="1000")]

    trade_logs, summary = execute_rebalance(
        FakeIB(),
        {"VOO": 1.0},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        fetch_quote_snapshots=lambda _ib, symbols: {
            symbol: SimpleNamespace(last_price=100.0) for symbol in symbols
        },
        submit_order_intent=lambda *_args, **_kwargs: SimpleNamespace(
            broker_order_id=None,
            status="Rejected",
        ),
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO"],
        strategy_profile="tech_communication_pullback_enhancement",
        signal_metadata=_signal_metadata(
            {"VOO": 1.0},
            risk_symbols=("VOO",),
            trade_date="2026-04-01",
            snapshot_as_of="2026-03-31",
        ),
        dry_run_only=False,
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert summary["execution_status"] == "blocked"
    assert summary["no_op_reason"] == "submit_failed:VOO:Rejected"
    assert summary["skipped_reasons"] == ["submit_failed:VOO:Rejected"]
    assert summary["orders_submitted"] == []
    assert "failed submit_failed:VOO:Rejected" in trade_logs


def test_execute_rebalance_uses_symbol_specific_limit_buy_premium(monkeypatch, tmp_path):
    class FakeIB:
        def openTrades(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="5000")]

    submitted = []

    def fake_submit_order_intent(_ib, intent):
        submitted.append(intent)
        return SimpleNamespace(broker_order_id="1", status="Submitted")

    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    execute_rebalance(
        FakeIB(),
        {"SOXL": 1.0},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        fetch_quote_snapshots=lambda _ib, symbols: {
            symbol: SimpleNamespace(last_price=100.0) for symbol in symbols
        },
        submit_order_intent=fake_submit_order_intent,
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["SOXL"],
        strategy_profile="soxl_soxx_trend_income",
        signal_metadata=_signal_metadata({"SOXL": 1.0}, risk_symbols=("SOXL",)),
        dry_run_only=False,
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        limit_buy_premium_by_symbol={"SOXL": 1.015},
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
    )

    assert submitted[0].symbol == "SOXL"
    assert submitted[0].side == "buy"
    assert submitted[0].limit_price == 101.5


def test_execute_rebalance_executes_option_intent_when_stock_targets_are_unchanged(tmp_path):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="300000")]

    def fake_fetch_quote_snapshots(_ib, symbols):
        return {symbol: SimpleNamespace(last_price=100.0) for symbol in symbols}

    option_intent = {
        "intent_type": "single_leg_option",
        "asset_class": "option",
        "action": "buy_to_open",
        "underlier": "TQQQ",
        "right": "C",
        "expiration": "2028-01-21",
        "strike": 70.0,
        "quantity": 2,
        "order_type": "limit",
        "limit_price": 32.5,
        "time_in_force": "DAY",
        "contract_multiplier": 100,
        "max_notional_usd": 6500.0,
    }

    trade_logs, summary = execute_rebalance(
        FakeIB(),
        {"VOO": 0.0},
        {},
        {"equity": 300000.0, "buying_power": 300000.0},
        fetch_quote_snapshots=fake_fetch_quote_snapshots,
        submit_order_intent=lambda *_args, **_kwargs: None,
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO"],
        strategy_profile="tqqq_growth_income",
        signal_metadata=_signal_metadata(
            {"VOO": 0.0},
            risk_symbols=("VOO",),
            trade_date="2026-04-01",
            snapshot_as_of="2026-03-31",
            option_order_intents={"schema_version": "option_order_intents.v1", "intents": (option_intent,)},
        ),
        dry_run_only=True,
        cash_reserve_ratio=0.03,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert summary["execution_status"] == "executed"
    assert summary["option_order_intent_count"] == 1
    assert summary["option_orders_submitted"][0]["symbol"] == "TQQQ 2028-01-21 70C"
    assert any("DRY_RUN option buy_to_open TQQQ" in log for log in trade_logs)


def test_execute_rebalance_dry_runs_multi_leg_option_intent_as_combo(tmp_path):
    class FakeIB:
        def openTrades(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="1000000")]

    trade_logs, summary = execute_rebalance(
        FakeIB(),
        {"VOO": 0.0},
        {},
        {"equity": 1000000.0, "buying_power": 1000000.0},
        fetch_quote_snapshots=lambda _ib, symbols: {symbol: SimpleNamespace(last_price=100.0) for symbol in symbols},
        submit_order_intent=lambda *_args, **_kwargs: None,
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO"],
        strategy_profile="soxl_soxx_trend_income",
        signal_metadata=_signal_metadata(
            {"VOO": 0.0},
            risk_symbols=("VOO",),
            option_order_intents={
                "schema_version": "option_order_intents.v1",
                "intents": (
                    {
                        "intent_type": "multi_leg_option",
                        "asset_class": "option",
                        "action": "sell_to_open_put_credit_spread",
                        "underlier": "SOXX",
                        "expiration": "2026-07-17",
                        "quantity": 1,
                        "max_loss_usd": 750.0,
                    },
                ),
            },
        ),
        dry_run_only=True,
        cash_reserve_ratio=0.03,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert trade_logs
    assert summary["execution_status"] == "executed"
    assert summary["option_orders_submitted"][0]["symbol"] == "SOXX 2026-07-17 PCS"


def test_execute_rebalance_hk_profile_dry_run_keeps_whole_share_orders_off_broker(tmp_path):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="HKD", value="200000")]

    submitted = []
    prices = {"02834": 12.0, "03110": 18.0}

    trade_logs, summary = execute_rebalance(
        FakeIB(),
        {"02834": 0.45, "03110": 0.35},
        {},
        {"equity": 200000.0, "buying_power": 200000.0},
        fetch_quote_snapshots=lambda _ib, symbols: {
            symbol: SimpleNamespace(last_price=prices[symbol]) for symbol in symbols
        },
        submit_order_intent=lambda _ib, intent: submitted.append(intent),
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["02834", "03110"],
        strategy_profile="hk_global_etf_tactical_rotation",
        account_group="paper-hk",
        service_name="ibkr-hk-paper",
        signal_metadata=_signal_metadata(
            {"02834": 0.45, "03110": 0.35},
            risk_symbols=("02834", "03110"),
            trade_date="2026-06-01",
            snapshot_as_of="2026-05-29",
        ),
        dry_run_only=True,
        cash_reserve_ratio=0.02,
        rebalance_threshold_ratio=0.01,
        limit_buy_premium=1.0,
        quantity_step=1.0,
        min_order_notional=50.0,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert submitted == []
    assert summary["mode"] == "dry_run"
    assert summary["execution_status"] == "executed"
    assert {order["symbol"] for order in summary["orders_submitted"]} == {"02834", "03110"}
    assert all(order["status"] == "dry_run" for order in summary["orders_submitted"])
    assert all(float(order["quantity"]).is_integer() for order in summary["orders_submitted"])
    assert {quote["symbol"] for quote in summary["quote_snapshot"]["quotes"]} == {"02834", "03110"}
    assert any(log.startswith("DRY_RUN buy 02834") for log in trade_logs)
    assert any(log.startswith("DRY_RUN buy 03110") for log in trade_logs)


def test_execute_rebalance_uses_reserved_cash_floor_when_higher(tmp_path):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="1000")]

    trade_logs, summary = execute_rebalance(
        FakeIB(),
        {},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        fetch_quote_snapshots=lambda _ib, _symbols: {},
        submit_order_intent=lambda _ib, _intent: None,
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=[],
        signal_metadata=_signal_metadata({}),
        dry_run_only=True,
        cash_reserve_ratio=0.03,
        cash_reserve_floor_usd=250.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert summary["cash_reserve_dollars"] == 250.0


def test_execute_rebalance_caps_buy_budget_by_reserved_cash_after_sell(tmp_path, monkeypatch):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="1412.12")]

    prices = {"SOXL": 225.19, "SOXX": 600.71}
    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    _trade_logs, summary = execute_rebalance(
        FakeIB(),
        {},
        {
            "SOXL": {"quantity": 3},
            "SOXX": {"quantity": 2},
        },
        {"equity": 3289.13, "buying_power": 1412.12},
        fetch_quote_snapshots=lambda _ib, symbols: {
            symbol: SimpleNamespace(last_price=prices[symbol]) for symbol in symbols
        },
        submit_order_intent=lambda *_args, **_kwargs: SimpleNamespace(
            broker_order_id="dry-run",
            status="Submitted",
        ),
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["SOXL", "SOXX"],
        strategy_profile="soxl_soxx_trend_income",
        signal_metadata=_signal_metadata(
            {"SOXL": 0.0, "SOXX": 0.90},
            risk_symbols=("SOXL", "SOXX"),
            trade_date="2026-06-26",
        ),
        dry_run_only=True,
        cash_reserve_ratio=0.0,
        cash_reserve_floor_usd=150.0,
        rebalance_threshold_ratio=0.01,
        limit_buy_premium=1.005,
        quantity_step=1.0,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    buy_orders = [order for order in summary["orders_submitted"] if order["side"] == "buy"]
    assert buy_orders
    buy_notional = sum(float(order["quantity"]) * prices[order["symbol"]] for order in buy_orders)
    # 1412.12 + 3*225.19 sale proceeds, minus 150 reserved, times 0.95 buffer
    max_investable = max(0.0, (1412.12 + (3 * 225.19)) - 150.0) * 0.95
    assert buy_notional <= max_investable + 1.0


def test_execute_rebalance_live_cash_only_reuses_projected_sell_proceeds_when_cash_snapshot_is_stale(
    tmp_path, monkeypatch
):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="CashBalance", currency="USD", value="112.38")]

    prices = {"SOXL": 192.50, "SOXX": 582.25}
    submitted = []
    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    def fake_submit_order_intent(_ib, intent):
        submitted.append(intent)
        if intent.side == "sell":
            return SimpleNamespace(
                broker_order_id=f"order-{len(submitted)}",
                symbol=intent.symbol,
                side=intent.side,
                status="Filled",
                quantity=intent.quantity,
                filled_quantity=intent.quantity,
                average_fill_price=prices[intent.symbol],
            )
        return SimpleNamespace(broker_order_id=f"order-{len(submitted)}", status="Submitted")

    _trade_logs, summary = execute_rebalance(
        FakeIB(),
        {},
        {
            "SOXL": {"quantity": 3},
            "SOXX": {"quantity": 2},
        },
        {"equity": 1920.36, "buying_power": 112.38},
        fetch_quote_snapshots=lambda _ib, symbols: {
            symbol: SimpleNamespace(last_price=prices[symbol]) for symbol in symbols
        },
        submit_order_intent=fake_submit_order_intent,
        order_intent_cls=OrderIntent,
        translator=build_translator("zh"),
        strategy_symbols=["SOXL", "SOXX"],
        strategy_profile="soxl_soxx_trend_income",
        signal_metadata=_signal_metadata(
            {"SOXL": 0.0, "SOXX": 0.90},
            risk_symbols=("SOXL", "SOXX"),
            trade_date="2026-07-09",
        ),
        dry_run_only=False,
        cash_reserve_ratio=0.03,
        rebalance_threshold_ratio=0.01,
        limit_buy_premium=1.005,
        quantity_step=1.0,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert [(intent.symbol, intent.side, intent.quantity) for intent in submitted] == [
        ("SOXL", "sell", 3),
        ("SOXX", "buy", 1),
    ]
    assert summary["orders_filled"][0]["symbol"] == "SOXL"
    assert summary["orders_submitted"][0]["symbol"] == "SOXX"
    assert summary["projected_sell_release_value"] == 577.5
    assert summary["orders_skipped"] == []


def test_execute_rebalance_live_cash_only_does_not_bridge_unfilled_sell_submissions(tmp_path, monkeypatch):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="CashBalance", currency="USD", value="112.38")]

    prices = {"SOXL": 192.50, "SOXX": 582.25}
    submitted = []
    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    def fake_submit_order_intent(_ib, intent):
        submitted.append(intent)
        return SimpleNamespace(broker_order_id=f"order-{len(submitted)}", status="Submitted")

    _trade_logs, summary = execute_rebalance(
        FakeIB(),
        {},
        {"SOXL": {"quantity": 3}, "SOXX": {"quantity": 0}},
        {"equity": 700.0, "buying_power": 112.38},
        fetch_quote_snapshots=lambda _ib, symbols: {
            symbol: SimpleNamespace(last_price=prices[symbol]) for symbol in symbols
        },
        submit_order_intent=fake_submit_order_intent,
        order_intent_cls=OrderIntent,
        translator=build_translator("zh"),
        strategy_symbols=["SOXL", "SOXX"],
        strategy_profile="soxl_soxx_trend_income",
        signal_metadata=_signal_metadata(
            {"SOXL": 0.0, "SOXX": 0.90},
            risk_symbols=("SOXL", "SOXX"),
            trade_date="2026-07-09",
        ),
        dry_run_only=False,
        cash_reserve_ratio=0.03,
        rebalance_threshold_ratio=0.01,
        limit_buy_premium=1.005,
        quantity_step=1.0,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert [(intent.symbol, intent.side, intent.quantity) for intent in submitted] == [("SOXL", "sell", 3)]
    assert summary["projected_sell_release_value"] == 0.0
    assert {"symbol": "SOXX", "side": "buy", "reason": "quantity_zero"} in summary["orders_skipped"]


def test_execute_rebalance_live_cash_only_uses_partial_fill_proceeds_and_fill_price(tmp_path, monkeypatch):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="CashBalance", currency="USD", value="100.00")]

    prices = {"SOXL": 200.0, "SOXX": 300.0}
    submitted = []
    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    def fake_submit_order_intent(_ib, intent):
        submitted.append(intent)
        if intent.side == "sell":
            return SimpleNamespace(
                broker_order_id="order-1",
                symbol=intent.symbol,
                side=intent.side,
                status="PartiallyFilled",
                quantity=intent.quantity,
                filled_quantity=1,
                average_fill_price=190.0,
            )
        return SimpleNamespace(broker_order_id=f"order-{len(submitted)}", status="Submitted")

    _trade_logs, summary = execute_rebalance(
        FakeIB(),
        {},
        {"SOXL": {"quantity": 3}, "SOXX": {"quantity": 0}},
        {"equity": 700.0, "buying_power": 100.0},
        fetch_quote_snapshots=lambda _ib, symbols: {
            symbol: SimpleNamespace(last_price=prices[symbol]) for symbol in symbols
        },
        submit_order_intent=fake_submit_order_intent,
        order_intent_cls=OrderIntent,
        translator=build_translator("zh"),
        strategy_symbols=["SOXL", "SOXX"],
        strategy_profile="soxl_soxx_trend_income",
        signal_metadata=_signal_metadata(
            {"SOXL": 0.0, "SOXX": 0.90},
            risk_symbols=("SOXL", "SOXX"),
            trade_date="2026-07-09",
        ),
        dry_run_only=False,
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.01,
        limit_buy_premium=1.005,
        quantity_step=1.0,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert [(intent.symbol, intent.side, intent.quantity) for intent in submitted] == [("SOXL", "sell", 3)]
    assert summary["projected_sell_release_value"] == 190.0
    assert summary["orders_partially_filled"][0]["symbol"] == "SOXL"
    assert {"symbol": "SOXX", "side": "buy", "reason": "quantity_zero"} in summary["orders_skipped"]


def test_execute_rebalance_projects_unbuyable_weight_target_to_zero(tmp_path, monkeypatch):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="236.81")]

    prices = {"SOXL": 191.15, "SOXX": 536.88, "BOXX": 100.0}
    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    trade_logs, summary = execute_rebalance(
        FakeIB(),
        {},
        {"SOXX": {"quantity": 1}},
        {"equity": 773.69, "buying_power": 236.81},
        fetch_quote_snapshots=lambda _ib, symbols: {
            symbol: SimpleNamespace(last_price=prices[symbol]) for symbol in symbols
        },
        submit_order_intent=lambda *_args, **_kwargs: SimpleNamespace(
            broker_order_id="dry-run",
            status="Submitted",
        ),
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["SOXL", "SOXX", "BOXX"],
        strategy_profile="soxl_soxx_trend_income",
        signal_metadata=_signal_metadata(
            {"SOXL": 0.70, "SOXX": 0.20, "BOXX": 0.10},
            risk_symbols=("SOXL", "SOXX"),
            safe_haven_symbols=("BOXX",),
            trade_date="2026-05-22",
        ),
        dry_run_only=True,
        cash_reserve_ratio=0.03,
        rebalance_threshold_ratio=0.01,
        limit_buy_premium=1.0,
        quantity_step=1.0,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert summary["small_account_whole_share_substituted_symbols"] == ["SOXX"]
    assert summary["orders_submitted"][0] == {
        "symbol": "SOXX",
        "side": "sell",
        "quantity": 1,
        "status": "dry_run",
    }
    assert summary["orders_submitted"][1]["symbol"] == "SOXL"
    assert summary["orders_submitted"][1]["side"] == "buy"
    assert summary["orders_submitted"][1]["quantity"] == 2


def test_execute_rebalance_keeps_safe_haven_cash_when_only_risk_target_is_unbuyable(tmp_path, monkeypatch):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="1294.00")]

    prices = {"SOXL": 175.0, "SOXX": 525.0, "BOXX": 116.83}
    submitted = []
    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    trade_logs, summary = execute_rebalance(
        FakeIB(),
        {},
        {},
        {"equity": 1294.0, "buying_power": 1294.0},
        fetch_quote_snapshots=lambda _ib, symbols: {
            symbol: SimpleNamespace(last_price=prices[symbol]) for symbol in symbols
        },
        submit_order_intent=lambda _ib, intent: submitted.append(intent) or SimpleNamespace(
            broker_order_id="dry-run",
            status="Submitted",
        ),
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["SOXL", "SOXX", "BOXX"],
        strategy_profile="soxl_soxx_trend_income",
        signal_metadata=_signal_metadata(
            {"SOXL": 0.0, "SOXX": 0.15, "BOXX": 0.85},
            risk_symbols=("SOXL", "SOXX"),
            safe_haven_symbols=("BOXX",),
            trade_date="2026-05-26",
        ),
        dry_run_only=True,
        cash_reserve_ratio=0.03,
        rebalance_threshold_ratio=0.01,
        limit_buy_premium=1.0,
        quantity_step=1.0,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert submitted == []
    assert summary["small_account_whole_share_substituted_symbols"] == ["SOXX"]
    assert summary["small_account_safe_haven_cash_substituted_symbols"] == ["BOXX"]
    assert len(summary["small_account_whole_share_cash_notes"]) == 1
    cash_note = summary["small_account_whole_share_cash_notes"][0]
    assert cash_note["symbol"] == "SOXX"
    assert round(cash_note["target_value"], 3) == 188.277
    assert cash_note["price"] == 525.0
    assert cash_note["cash_symbols"] == ("BOXX",)
    assert any("SOXX.US 目标金额 $188.28 低于 1 股价格 $525.00" in log for log in trade_logs)
    assert summary["realized_safe_haven_weight"] == 0.0
    boxx_row = next(row for row in summary["target_vs_current"] if row["symbol"] == "BOXX")
    assert boxx_row["target_weight"] == 0.0


def test_execute_rebalance_routes_order_to_single_account_id(monkeypatch, tmp_path):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [
                SimpleNamespace(account="U1234567", tag="AvailableFunds", currency="USD", value="5000"),
                SimpleNamespace(account="U7654321", tag="AvailableFunds", currency="USD", value="100"),
            ]

    submitted = []

    def fake_submit_order_intent(_ib, intent):
        submitted.append(intent)
        return SimpleNamespace(broker_order_id="1", status="Submitted")

    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    trade_logs, summary = execute_rebalance(
        FakeIB(),
        {"TQQQ": 1.0},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        fetch_quote_snapshots=lambda _ib, symbols: {
            symbol: SimpleNamespace(last_price=100.0) for symbol in symbols
        },
        submit_order_intent=fake_submit_order_intent,
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["TQQQ"],
        strategy_profile="tqqq_growth_income",
        account_group="live-tqqq",
        service_name="ibkr-tqqq-live",
        account_ids=("U1234567",),
        signal_metadata=_signal_metadata({"TQQQ": 1.0}, risk_symbols=("TQQQ",), trade_date="2026-04-01"),
        dry_run_only=False,
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert len(submitted) == 1
    assert submitted[0].account_id == "U1234567"
    assert summary["order_account_id"] == "U1234567"


def test_execute_rebalance_rejects_multiple_order_account_ids():
    class FakeIB:
        def accountValues(self):
            return []

    try:
        execute_rebalance(
            FakeIB(),
            {"TQQQ": 1.0},
            {},
            {"equity": 1000.0, "buying_power": 1000.0},
            fetch_quote_snapshots=lambda _ib, _symbols: {},
            submit_order_intent=lambda *_args, **_kwargs: None,
            order_intent_cls=OrderIntent,
            translator=translate,
            strategy_symbols=["TQQQ"],
            signal_metadata=_signal_metadata({"TQQQ": 1.0}, risk_symbols=("TQQQ",)),
            account_ids=("U1234567", "U7654321"),
            dry_run_only=False,
            cash_reserve_ratio=0.0,
            rebalance_threshold_ratio=0.02,
            limit_buy_premium=1.005,
            sell_settle_delay_sec=0,
        )
    except ValueError as exc:
        assert "single account_id" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_execute_rebalance_zero_target_sell_uses_position_quantity(monkeypatch, tmp_path):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="1000")]

    submitted = []

    def fake_submit_order_intent(_ib, intent):
        submitted.append(intent)
        return SimpleNamespace(broker_order_id="1", status="Submitted")

    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    _trade_logs, summary = execute_rebalance(
        FakeIB(),
        {"VOO": 0.0},
        {"VOO": {"quantity": 2}},
        {"equity": 327.88, "buying_power": 1000.0},
        fetch_quote_snapshots=lambda *_args, **_kwargs: {"VOO": SimpleNamespace(last_price=165.85)},
        submit_order_intent=fake_submit_order_intent,
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO"],
        strategy_profile="global_etf_rotation",
        signal_metadata=_signal_metadata({"VOO": 0.0}, risk_symbols=("VOO",), trade_date="2026-04-01"),
        dry_run_only=False,
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        quantity_step=1.0,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert summary["execution_status"] == "executed"
    assert len(submitted) == 1
    assert submitted[0].side == "sell"
    assert submitted[0].quantity == 2


def test_execute_rebalance_keeps_existing_whole_share_when_positive_target_is_unbuyable(monkeypatch, tmp_path):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="500")]

    submitted = []

    def fake_submit_order_intent(_ib, intent):
        submitted.append(intent)
        return SimpleNamespace(broker_order_id="1", status="Submitted")

    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    _trade_logs, summary = execute_rebalance(
        FakeIB(),
        {"TQQQ": 0.1, "QQQM": 0.6},
        {"TQQQ": {"quantity": 7}, "QQQM": {"quantity": 0}},
        {"equity": 540.0, "buying_power": 540.0},
        fetch_quote_snapshots=lambda *_args, **_kwargs: {
            "TQQQ": SimpleNamespace(last_price=77.33),
            "QQQM": SimpleNamespace(last_price=297.19),
        },
        submit_order_intent=fake_submit_order_intent,
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["TQQQ", "QQQM"],
        strategy_profile="tqqq_growth_income",
        signal_metadata=_signal_metadata(
            {"TQQQ": 60.94 / 540.0, "QQQM": 320.0 / 540.0},
            risk_symbols=("TQQQ", "QQQM"),
            trade_date="2026-06-17",
        ),
        dry_run_only=False,
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        quantity_step=1.0,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert summary["small_account_existing_whole_share_retained_symbols"] == ["TQQQ"]
    sell_orders = [intent for intent in submitted if intent.side == "sell"]
    assert len(sell_orders) == 1
    assert sell_orders[0].symbol == "TQQQ"
    assert sell_orders[0].quantity == 6


def test_execute_rebalance_retains_existing_soxx_when_delever_target_nearly_one_share(monkeypatch, tmp_path):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="519.54")]

    prices = {"SOXL": 232.99, "SOXX": 605.17, "BOXX": 117.06}
    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    _trade_logs, summary = execute_rebalance(
        FakeIB(),
        {},
        {"SOXL": {"quantity": 0}, "SOXX": {"quantity": 1}, "BOXX": {"quantity": 0}},
        {"equity": 1170.60, "buying_power": 519.54},
        fetch_quote_snapshots=lambda _ib, symbols: {
            symbol: SimpleNamespace(last_price=prices[symbol]) for symbol in symbols
        },
        submit_order_intent=lambda *_args, **_kwargs: SimpleNamespace(
            broker_order_id="dry-run",
            status="Submitted",
        ),
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["SOXL", "SOXX", "BOXX"],
        strategy_profile="soxl_soxx_trend_income",
        signal_metadata=_signal_metadata(
            {"SOXL": 0.35, "SOXX": 0.55, "BOXX": 0.10},
            risk_symbols=("SOXL", "SOXX"),
            safe_haven_symbols=("BOXX",),
            trade_date="2026-06-24",
        ),
        dry_run_only=True,
        cash_reserve_ratio=0.0,
        cash_reserve_floor_usd=150.0,
        rebalance_threshold_ratio=0.01,
        limit_buy_premium=1.005,
        quantity_step=1.0,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert summary["small_account_existing_whole_share_retained_symbols"] == ["SOXX"]
    assert "SOXX" not in summary["small_account_whole_share_substituted_symbols"]
    assert not [
        order
        for order in summary["orders_submitted"]
        if order["side"] == "sell" and order["symbol"] == "SOXX"
    ]
    assert {
        "symbol": "SOXL",
        "side": "buy",
        "quantity": 1,
        "limit_price": 234.15,
        "status": "dry_run",
    } in summary["orders_submitted"]


def test_execute_rebalance_caps_buy_to_cash_when_overweight_sell_rounds_to_zero(
    monkeypatch, tmp_path
):
    """Reproduce SOXL/SOXX rotation: SOXX trim rounds to 0 shares; buys must not exceed cash."""
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [
                SimpleNamespace(tag="AvailableFunds", currency="USD", value="2000.00"),
                SimpleNamespace(tag="CashBalance", currency="USD", value="400.00"),
            ]

    prices = {"SOXL": 219.80, "SOXX": 611.00, "BOXX": 100.0}
    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    _trade_logs, summary = execute_rebalance(
        FakeIB(),
        {},
        {"SOXL": {"quantity": 0}, "SOXX": {"quantity": 4}, "BOXX": {"quantity": 0}},
        {"equity": 4032.0, "buying_power": 400.0},
        fetch_quote_snapshots=lambda _ib, symbols: {
            symbol: SimpleNamespace(last_price=prices[symbol]) for symbol in symbols
        },
        submit_order_intent=lambda *_args, **_kwargs: SimpleNamespace(
            broker_order_id="dry-run",
            status="Submitted",
        ),
        order_intent_cls=OrderIntent,
        translator=build_translator("zh"),
        strategy_symbols=["SOXL", "SOXX", "BOXX"],
        strategy_profile="soxl_soxx_trend_income",
        signal_metadata=_signal_metadata(
            {"SOXL": 0.35, "SOXX": 0.55, "BOXX": 0.10},
            risk_symbols=("SOXL", "SOXX"),
            safe_haven_symbols=("BOXX",),
            trade_date="2026-06-29",
        ),
        dry_run_only=True,
        cash_reserve_ratio=0.0,
        cash_reserve_floor_usd=0.0,
        rebalance_threshold_ratio=0.01,
        limit_buy_premium=1.005,
        quantity_step=1.0,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    buy_orders = [order for order in summary["orders_submitted"] if order["side"] == "buy"]
    assert len(buy_orders) == 1
    assert buy_orders[0]["symbol"] == "SOXL"
    assert buy_orders[0]["quantity"] == 1
    assert buy_orders[0]["quantity"] * buy_orders[0]["limit_price"] <= 400.0
    assert {"symbol": "SOXX", "side": "sell", "reason": "quantity_zero"} in summary["orders_skipped"]
    assert not [order for order in summary["orders_submitted"] if order["side"] == "sell"]


def test_execute_rebalance_bootstraps_close_to_one_share_core_target(monkeypatch, tmp_path):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="623.39")]

    prices = {"SOXL": 229.73, "SOXX": 603.00, "BOXX": 100.0}
    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    trade_logs, summary = execute_rebalance(
        FakeIB(),
        {},
        {"SOXL": {"quantity": 0}, "SOXX": {"quantity": 0}, "BOXX": {"quantity": 0}},
        {"equity": 623.39, "buying_power": 623.39},
        fetch_quote_snapshots=lambda _ib, symbols: {
            symbol: SimpleNamespace(last_price=prices[symbol]) for symbol in symbols
        },
        submit_order_intent=lambda *_args, **_kwargs: SimpleNamespace(
            broker_order_id="dry-run",
            status="Submitted",
        ),
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["SOXL", "SOXX", "BOXX"],
        strategy_profile="soxl_soxx_trend_income",
        signal_metadata=_signal_metadata(
            {"SOXL": 0.35, "SOXX": 0.55, "BOXX": 0.10},
            risk_symbols=("SOXL", "SOXX"),
            safe_haven_symbols=("BOXX",),
            trade_date="2026-06-24",
        ),
        dry_run_only=True,
        cash_reserve_ratio=0.0,
        cash_reserve_floor_usd=0.0,
        rebalance_threshold_ratio=0.01,
        limit_buy_premium=1.005,
        limit_buy_premium_by_symbol={"SOXL": 1.015},
        quantity_step=1.0,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert summary["small_account_whole_share_bootstrap_symbols"] == ["SOXL"]
    assert summary["small_account_whole_share_substituted_symbols"] == ["SOXX"]
    assert summary["small_account_allocation_drift_notes"][0]["symbol"] == "SOXX"
    assert any("整数股偏离" in log and "SOXX.US 预计 0.0%" in log for log in trade_logs)
    assert any("SOXL.US" in log and "1 股" in log for log in trade_logs)
    assert {
        "symbol": "SOXL",
        "side": "buy",
        "quantity": 1,
        "limit_price": 233.18,
        "status": "dry_run",
    } in summary["orders_submitted"]
    assert not [
        order
        for order in summary["orders_submitted"]
        if order["side"] == "buy" and order["symbol"] == "SOXX"
    ]


def test_execute_rebalance_buy_only_top_up_existing_whole_share_is_not_filtered_as_no_op(monkeypatch, tmp_path):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="700.00")]

    prices = {"SOXX": 605.0}
    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    _trade_logs, summary = execute_rebalance(
        FakeIB(),
        {},
        {"SOXX": {"quantity": 1}},
        {"equity": 1305.0, "buying_power": 700.0},
        fetch_quote_snapshots=lambda _ib, symbols: {
            symbol: SimpleNamespace(last_price=prices[symbol]) for symbol in symbols
        },
        submit_order_intent=lambda *_args, **_kwargs: SimpleNamespace(
            broker_order_id="dry-run",
            status="Submitted",
        ),
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["SOXX"],
        strategy_profile="soxl_soxx_trend_income",
        signal_metadata=_signal_metadata(
            {"SOXX": 0.883},
            risk_symbols=("SOXX",),
            trade_date="2026-07-09",
        ),
        dry_run_only=True,
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.01,
        limit_buy_premium=1.005,
        quantity_step=1.0,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert summary["execution_status"] == "executed"
    assert summary["small_account_whole_share_bootstrap_symbols"] == ["SOXX"]
    assert {
        "symbol": "SOXX",
        "side": "buy",
        "quantity": 1,
        "limit_price": 608.02,
        "status": "dry_run",
    } in summary["orders_submitted"]


def test_execute_rebalance_skips_when_pending_orders_exist():
    class FakeIB:
        def openTrades(self):
            return [
                SimpleNamespace(
                    contract=SimpleNamespace(symbol="VOO"),
                    orderStatus=SimpleNamespace(status="Submitted"),
                )
            ]

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="5000")]

    trade_logs = execute_rebalance(
        FakeIB(),
        {"VOO": 1.0},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        fetch_quote_snapshots=lambda *_args, **_kwargs: {"VOO": SimpleNamespace(last_price=100.0)},
        submit_order_intent=lambda *_args, **_kwargs: None,
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO"],
        strategy_profile="tech_communication_pullback_enhancement",
        signal_metadata=_signal_metadata({"VOO": 1.0}, risk_symbols=("VOO",)),
        dry_run_only=False,
        cash_reserve_ratio=0.03,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
    )

    assert trade_logs == ["pending_orders_detected profile=tech_communication_pullback_enhancement symbols=VOO"]


def test_execute_rebalance_blocks_same_day_repeat_via_execution_lock(tmp_path, monkeypatch):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="5000")]

    def fake_fetch_quote_snapshots(_ib, symbols):
        return {symbol: SimpleNamespace(last_price=100.0) for symbol in symbols}

    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    kwargs = dict(
        fetch_quote_snapshots=fake_fetch_quote_snapshots,
        submit_order_intent=lambda *_args, **_kwargs: SimpleNamespace(broker_order_id="1", status="Submitted"),
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO", "BOXX"],
        strategy_profile="tech_communication_pullback_enhancement",
        account_group="default",
        service_name="ibkr-paper",
        account_ids=("DU123",),
        signal_metadata=_signal_metadata(
            {"VOO": 0.8, "BOXX": 0.2},
            risk_symbols=("VOO",),
            safe_haven_symbols=("BOXX",),
            regime="risk_on",
            breadth_ratio=0.6,
            target_stock_weight=0.8,
            realized_stock_weight=0.8,
            trade_date="2026-04-01",
            snapshot_as_of="2026-03-31",
        ),
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
    )

    first_logs = execute_rebalance(
        FakeIB(),
        {"VOO": 0.8, "BOXX": 0.2},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        dry_run_only=False,
        **kwargs,
    )
    second_logs = execute_rebalance(
        FakeIB(),
        {"VOO": 0.8, "BOXX": 0.2},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        dry_run_only=False,
        **kwargs,
    )
    paper_logs = execute_rebalance(
        FakeIB(),
        {"VOO": 0.8, "BOXX": 0.2},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        dry_run_only=False,
        **kwargs,
    )

    assert any("execution_lock_acquired" in log for log in first_logs)
    assert any("same_day_execution_locked" in log for log in second_logs)
    assert any("same_day_execution_locked" in log for log in paper_logs)


def test_execute_rebalance_skips_when_same_day_fills_detected():
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return [
                SimpleNamespace(
                    contract=SimpleNamespace(symbol="VOO"),
                    execution=SimpleNamespace(time="2026-04-01 10:30:00", acctNumber="DU123"),
                )
            ]

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="5000")]

    trade_logs = execute_rebalance(
        FakeIB(),
        {"VOO": 1.0},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        fetch_quote_snapshots=lambda *_args, **_kwargs: {"VOO": SimpleNamespace(last_price=100.0)},
        submit_order_intent=lambda *_args, **_kwargs: None,
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO"],
        strategy_profile="tech_communication_pullback_enhancement",
        account_group="default",
        service_name="ibkr-paper",
        account_ids=("DU123",),
        signal_metadata=_signal_metadata({"VOO": 1.0}, risk_symbols=("VOO",), trade_date="2026-04-01"),
        dry_run_only=False,
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
    )

    assert any("same_day_fills_detected" in log for log in trade_logs)


def test_execute_rebalance_returns_structured_summary_when_requested(monkeypatch, tmp_path):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="5000")]

    def fake_fetch_quote_snapshots(_ib, symbols):
        return {symbol: SimpleNamespace(last_price=100.0) for symbol in symbols}

    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    trade_logs, summary = execute_rebalance(
        FakeIB(),
        {"VOO": 0.8, "BOXX": 0.2},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        fetch_quote_snapshots=fake_fetch_quote_snapshots,
        submit_order_intent=lambda *_args, **_kwargs: SimpleNamespace(broker_order_id="1", status="Submitted"),
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO", "BOXX"],
        strategy_profile="tech_communication_pullback_enhancement",
        account_group="default",
        service_name="ibkr-paper",
        account_ids=("DU123",),
        signal_metadata=_signal_metadata(
            {"VOO": 0.8, "BOXX": 0.2},
            risk_symbols=("VOO",),
            safe_haven_symbols=("BOXX",),
            regime="risk_on",
            breadth_ratio=0.6,
            target_stock_weight=0.8,
            realized_stock_weight=0.8,
            safe_haven_weight=0.2,
            safe_haven_symbol="BOXX",
            trade_date="2026-04-01",
            snapshot_as_of="2026-03-31",
        ),
        dry_run_only=False,
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert any("execution_lock_acquired" in log for log in trade_logs)
    assert summary["execution_status"] == "executed"
    assert summary["mode"] == "paper"
    assert summary["safe_haven_symbol"] == "BOXX"
    assert summary["orders_submitted"]
    assert summary["target_vs_current"]


def test_execute_rebalance_keeps_small_safe_haven_target_as_cash(monkeypatch, tmp_path):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="1500")]

    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    trade_logs, summary = execute_rebalance(
        FakeIB(),
        {"VOO": 0.5, "BOXX": 0.5},
        {},
        {"equity": 1500.0, "buying_power": 1500.0},
        fetch_quote_snapshots=lambda _ib, symbols: {
            symbol: SimpleNamespace(last_price=100.0) for symbol in symbols
        },
        submit_order_intent=lambda *_args, **_kwargs: SimpleNamespace(broker_order_id="1", status="Submitted"),
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO", "BOXX"],
        strategy_profile="tech_communication_pullback_enhancement",
        signal_metadata=_signal_metadata(
            {"VOO": 0.5, "BOXX": 0.5},
            risk_symbols=("VOO",),
            safe_haven_symbols=("BOXX",),
            trade_date="2026-04-01",
            snapshot_as_of="2026-03-31",
        ),
        dry_run_only=True,
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.01,
        limit_buy_premium=1.0,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
        safe_haven_cash_substitute_threshold_usd=1000.0,
    )

    assert any("DRY_RUN buy VOO" in log for log in trade_logs)
    assert not any(order["symbol"] == "BOXX" for order in summary["orders_submitted"])
    assert summary["safe_haven_cash_substituted_symbols"] == ["BOXX"]
    assert summary["realized_safe_haven_weight"] == 0.0
    boxx_row = next(row for row in summary["target_vs_current"] if row["symbol"] == "BOXX")
    assert boxx_row["target_weight"] == 0.0


def test_execute_rebalance_sells_cash_sweep_symbol_when_buying_power_is_short(monkeypatch, tmp_path):
    class FakeIB:
        def __init__(self):
            self._account_values_calls = 0

        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            self._account_values_calls += 1
            buying_power = "24" if self._account_values_calls == 1 else "224"
            return [
                SimpleNamespace(
                    account="DU123",
                    tag="CashBalance",
                    currency="USD",
                    value=buying_power,
                )
            ]

    submitted = []

    def fake_submit_order_intent(_ib, intent):
        submitted.append(intent)
        return SimpleNamespace(broker_order_id=f"order-{len(submitted)}", status="Submitted")

    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    trade_logs, summary = execute_rebalance(
        FakeIB(),
        {"VOO": 0.8, "BOXX": 0.2},
        {"VOO": {"quantity": 0}, "BOXX": {"quantity": 2}},
        {"equity": 1000.0, "buying_power": 24.0},
        fetch_quote_snapshots=lambda *_args, **_kwargs: {
            "VOO": SimpleNamespace(last_price=100.0),
            "BOXX": SimpleNamespace(last_price=100.0),
        },
        submit_order_intent=fake_submit_order_intent,
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO", "BOXX"],
        strategy_profile="tech_communication_pullback_enhancement",
        account_group="default",
        service_name="ibkr-paper",
        account_ids=("DU123",),
        signal_metadata=_signal_metadata(
            {"VOO": 0.8, "BOXX": 0.2},
            risk_symbols=("VOO",),
            safe_haven_symbols=("BOXX",),
            regime="risk_on",
            breadth_ratio=0.6,
            target_stock_weight=0.8,
            realized_stock_weight=0.8,
            safe_haven_weight=0.2,
            safe_haven_symbol="BOXX",
            trade_date="2026-04-01",
            snapshot_as_of="2026-03-31",
        ),
        dry_run_only=False,
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert any(intent.side == "sell" and intent.symbol == "BOXX" for intent in submitted)
    assert any(intent.side == "buy" and intent.symbol == "VOO" for intent in submitted)
    assert summary["execution_status"] == "executed"
    assert any(log.startswith("sell BOXX") for log in trade_logs)


def test_execute_rebalance_blocks_when_material_target_has_missing_prices():
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="5000")]

    trade_logs, summary = execute_rebalance(
        FakeIB(),
        {"VOO": 1.0},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        fetch_quote_snapshots=lambda *_args, **_kwargs: {},
        submit_order_intent=lambda *_args, **_kwargs: None,
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO"],
        strategy_profile="tech_communication_pullback_enhancement",
        signal_metadata=_signal_metadata(
            {"VOO": 1.0},
            risk_symbols=("VOO",),
            trade_date="2026-04-01",
            snapshot_as_of="2026-03-31",
        ),
        dry_run_only=True,
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
        return_summary=True,
    )

    assert summary["execution_status"] == "blocked"
    assert summary["no_op_reason"] == "missing_price:VOO"
    assert summary["orders_skipped"] == [{"symbol": "VOO", "reason": "missing_price"}]
    assert "failed missing_price:VOO" in trade_logs[-1]


def test_execute_rebalance_blocks_when_material_target_has_no_buying_power():
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="0")]

    trade_logs, summary = execute_rebalance(
        FakeIB(),
        {"VOO": 1.0},
        {},
        {"equity": 1000.0, "buying_power": 0.0},
        fetch_quote_snapshots=lambda *_args, **_kwargs: {"VOO": SimpleNamespace(last_price=100.0)},
        submit_order_intent=lambda *_args, **_kwargs: None,
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO"],
        strategy_profile="tech_communication_pullback_enhancement",
        signal_metadata=_signal_metadata(
            {"VOO": 1.0},
            risk_symbols=("VOO",),
            trade_date="2026-04-01",
            snapshot_as_of="2026-03-31",
        ),
        dry_run_only=True,
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
        return_summary=True,
    )

    assert summary["execution_status"] == "blocked"
    assert summary["no_op_reason"] == "insufficient_buying_power:VOO"
    assert summary["skipped_reasons"] == ["insufficient_buying_power:VOO"]
    assert "failed insufficient_buying_power:VOO" in trade_logs[-1]


def test_execute_rebalance_uses_snapshot_prices_for_dry_run_when_quotes_missing(tmp_path):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="5000")]

    trade_logs, summary = execute_rebalance(
        FakeIB(),
        {"VOO": 0.6, "BOXX": 0.4},
        {},
        {"equity": 3000.0, "buying_power": 3000.0},
        fetch_quote_snapshots=lambda *_args, **_kwargs: {},
        submit_order_intent=lambda *_args, **_kwargs: None,
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO", "BOXX"],
        strategy_profile="tech_communication_pullback_enhancement",
        signal_metadata=_signal_metadata(
            {"VOO": 0.6, "BOXX": 0.4},
            risk_symbols=("VOO",),
            safe_haven_symbols=("BOXX",),
            trade_date="2026-04-01",
            snapshot_as_of="2026-03-31",
            dry_run_price_fallbacks={"VOO": 100.0, "BOXX": 100.0},
        ),
        dry_run_only=True,
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert summary["execution_status"] == "executed"
    assert len(summary["orders_submitted"]) == 2
    assert summary["snapshot_price_fallback_used"] is True
    assert summary["snapshot_price_fallback_count"] == 2
    assert set(summary["snapshot_price_fallback_symbols"]) == {"VOO", "BOXX"}
    assert summary["price_source_mode"] == "mixed_market_quote_snapshot_close"
    assert any(log.startswith("dry_run_snapshot_prices count=2") for log in trade_logs)
    assert any(log.startswith("DRY_RUN buy VOO") for log in trade_logs)
    assert any(log.startswith("DRY_RUN buy BOXX") for log in trade_logs)


def test_execute_rebalance_uses_price_fallbacks_for_live_when_quotes_missing(monkeypatch, tmp_path):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="5000")]

    submitted = []

    def fake_submit_order_intent(_ib, intent):
        submitted.append(intent)
        return SimpleNamespace(broker_order_id="1", status="Submitted")

    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    trade_logs, summary = execute_rebalance(
        FakeIB(),
        {"VOO": 1.0},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        fetch_quote_snapshots=lambda *_args, **_kwargs: {},
        submit_order_intent=fake_submit_order_intent,
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO"],
        strategy_profile="tech_communication_pullback_enhancement",
        signal_metadata=_signal_metadata(
            {"VOO": 1.0},
            risk_symbols=("VOO",),
            trade_date="2026-04-01",
            price_fallbacks={"VOO": 100.0},
            price_fallback_source="historical_close",
        ),
        dry_run_only=False,
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert summary["execution_status"] == "executed"
    assert len(submitted) == 1
    assert submitted[0].symbol == "VOO"
    assert summary["snapshot_price_fallback_used"] is True
    assert summary["price_fallback_source"] == "historical_close"
    assert summary["price_source_mode"] == "mixed_market_quote_historical_close"
    assert any(log.startswith("price_fallback_prices count=1") for log in trade_logs)
