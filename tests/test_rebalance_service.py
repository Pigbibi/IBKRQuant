import json

from application.rebalance_service import (
    _resolve_reconciliation_mode,
    _should_record_execution_marker,
    _strategy_dashboard_text,
    run_strategy_core,
)
from application.runtime_dependencies import IBKRRebalanceConfig
from notifications.renderers import (
    _build_notification_trade_lines,
    build_dashboard,
    render_trade_notification,
)
from notifications.telegram import build_translator


def _weight_allocation(targets, *, risk_symbols=(), income_symbols=(), safe_haven_symbols=()):
    return {
        "target_mode": "weight",
        "strategy_symbols": tuple(targets.keys()),
        "risk_symbols": tuple(risk_symbols),
        "income_symbols": tuple(income_symbols),
        "safe_haven_symbols": tuple(safe_haven_symbols),
        "targets": dict(targets),
    }


def _build_test_translator():
    templates = {
        "heartbeat_title": "heartbeat",
        "rebalance_title": "rebalance",
        "no_trades": "no trades",
        "equity": "equity",
        "buying_power": "buying_power",
        "empty_positions": "(empty positions)",
        "empty_target_weights": "(empty target positions)",
        "account_summary_title": "Account Summary",
        "positions_title": "Current Positions",
        "execution_summary_title": "Execution Summary",
        "target_weights_title": "Target Weights",
        "strategy_label": "strategy={name}",
        "strategy_profile_detail": "strategy_profile={profile}",
        "regime_detail": "regime={value}",
        "breadth_detail": "breadth={value}",
        "target_stock_detail": "target_stock={value}",
        "realized_stock_detail": "realized_stock={value}",
        "safe_haven_target_detail": "safe_haven_target={value}",
        "snapshot_decision_detail": "snapshot_decision={value}",
        "snapshot_as_of_detail": "snapshot_as_of={value}",
        "snapshot_age_days_detail": "snapshot_age_days={value}",
        "snapshot_file_ts_detail": "snapshot_file_ts={value}",
        "snapshot_path_detail": "snapshot_path={value}",
        "config_source_detail": "config_source={value}",
        "target_diff_summary": "target_changes {details}",
        "same_day_execution_locked_notice": "same_day_execution_locked_notice {mode} {trade_date} {snapshot_date}",
        "dry_run_snapshot_prices": "dry_run_snapshot_prices count={count} symbols={symbols}",
        "price_fallback_prices": "price_fallback_prices count={count} symbols={symbols}",
        "no_order_plan_reason": "no_order_plan_reason {reason}",
        "dry_run_buy_batch": "dry_run_buy_batch count={count} details={details}",
        "dry_run_sell_batch": "dry_run_sell_batch count={count} details={details}",
        "submitted_buy_batch": "submitted_buy_batch count={count} details={details}",
        "submitted_sell_batch": "submitted_sell_batch count={count} details={details}",
        "filled_buy_batch": "filled_buy_batch count={count} details={details}",
        "filled_sell_batch": "filled_sell_batch count={count} details={details}",
        "partial_buy_batch": "partial_buy_batch count={count} details={details}",
        "partial_sell_batch": "partial_sell_batch count={count} details={details}",
        "deferred_buy_batch": "deferred_buy_batch details={details}",
        "deferred_sell_batch": "deferred_sell_batch details={details}",
        "failed_buy_batch": "failed_buy_batch details={details}",
        "failed_sell_batch": "failed_sell_batch details={details}",
        "skip_reason_quantity_zero": "quantity_zero",
        "skip_reason_cancelled": "order cancelled",
        "skip_reason_submit_failed": "submit_failed",
        "skip_order_detail": "{symbol} ({reason})",
        "skip_order_detail_with_qty": "{symbol} {quantity} ({reason})",
    }

    def translate(key, **kwargs):
        template = templates.get(key, key)
        return template.format(**kwargs) if kwargs else template

    return translate


def test_execution_marker_is_not_recorded_for_blocked_execution_with_trade_logs():
    config = IBKRRebalanceConfig(
        translator=_build_test_translator(),
        separator="---",
        execution_dedup_enabled=True,
    )

    assert not _should_record_execution_marker(
        trade_logs=("broker submission failed",),
        execution_summary={
            "execution_status": "blocked",
            "orders_skipped": [{"reason": "submit_failed"}],
        },
        config=config,
    )


def test_execution_marker_is_recorded_after_accepted_order():
    config = IBKRRebalanceConfig(
        translator=_build_test_translator(),
        separator="---",
        execution_dedup_enabled=True,
    )

    assert _should_record_execution_marker(
        trade_logs=("order submitted",),
        execution_summary={
            "execution_status": "executed",
            "orders_submitted": [{"symbol": "AAA", "status": "Submitted"}],
        },
        config=config,
    )


def test_execution_marker_is_recorded_while_broker_order_is_pending_reconciliation():
    config = IBKRRebalanceConfig(
        translator=_build_test_translator(),
        separator="---",
        execution_dedup_enabled=True,
    )

    assert _should_record_execution_marker(
        trade_logs=("order awaiting broker confirmation",),
        execution_summary={
            "execution_status": "pending_reconciliation",
            "orders_pending": [{"symbol": "AAA", "status": "Submitted"}],
        },
        config=config,
    )


def test_execution_marker_is_recorded_after_partial_submission_success():
    config = IBKRRebalanceConfig(
        translator=_build_test_translator(),
        separator="---",
        execution_dedup_enabled=True,
    )

    assert _should_record_execution_marker(
        trade_logs=("one order submitted", "one order rejected"),
        execution_summary={
            "execution_status": "executed",
            "orders_submitted": [{"symbol": "AAA", "status": "Submitted"}],
            "skipped_reasons": ["submit_failed:BBB:Rejected"],
        },
        config=config,
    )


def test_execution_marker_preserves_legacy_trade_log_fallback():
    config = IBKRRebalanceConfig(
        translator=_build_test_translator(),
        separator="---",
        execution_dedup_enabled=True,
    )

    assert _should_record_execution_marker(
        trade_logs=("legacy order submitted",),
        execution_summary=None,
        config=config,
    )


def test_execution_marker_preserves_legacy_trade_log_fallback_with_minimal_summary():
    config = IBKRRebalanceConfig(
        translator=_build_test_translator(),
        separator="---",
        execution_dedup_enabled=True,
    )

    assert _should_record_execution_marker(
        trade_logs=("legacy order submitted",),
        execution_summary={"adapter_metadata": {"source": "legacy"}},
        config=config,
    )


def test_build_dashboard_localizes_strategy_details():
    dashboard = build_dashboard(
        positions={},
        account_values={"equity": 1000.0, "buying_power": 500.0},
        signal_desc="保持观望",
        status_desc="breadth=0.0%",
        strategy_profile="global_etf_rotation",
        target_weights={},
        signal_metadata={
            "regime": "risk_off",
            "breadth_ratio": 0.0,
            "target_stock_weight": 0.0,
            "realized_stock_weight": 0.0,
            "snapshot_as_of": None,
            "snapshot_guard_decision": "proceed",
            "snapshot_age_days": 8,
            "feature_snapshot_path": "gs://bucket/snapshot.csv",
            "strategy_config_source": "external_config",
            "allocation": _weight_allocation({}, safe_haven_symbols=("BOXX",)),
        },
        translator=build_translator("zh"),
        separator="---",
        strategy_display_name="全球 ETF 轮动",
    )

    assert "🧭 策略: 全球 ETF 轮动" in dashboard
    assert "📊 账户摘要" in dashboard
    assert "💼 当前持仓" in dashboard
    assert "🧾 执行摘要" in dashboard
    assert "目标持仓" in dashboard
    assert "市场阶段=risk_off" in dashboard
    assert "快照路径" not in dashboard
    assert "配置来源" not in dashboard
    assert "快照账龄" not in dashboard


def test_reconciliation_mode_uses_execution_summary_live_mode():
    config = IBKRRebalanceConfig(
        translator=_build_test_translator(),
        separator="---",
        execution_mode="paper",
    )

    assert _resolve_reconciliation_mode(
        config,
        signal_metadata={"dry_run_only": False},
        execution_summary={"mode": "live"},
    ) == "live"


def test_build_dashboard_localizes_snapshot_guard_text_for_zh():
    dashboard = build_dashboard(
        positions={},
        account_values={"equity": 1000.0, "buying_power": 500.0},
        signal_desc="feature snapshot guard blocked execution",
        status_desc="fail_closed | reason=feature_snapshot_path_missing",
        strategy_profile="tech_communication_pullback_enhancement",
        target_weights={},
        signal_metadata={
            "allocation": _weight_allocation({}, safe_haven_symbols=("BOXX",)),
        },
        translator=build_translator("zh"),
        separator="---",
        strategy_display_name="科技通信回调增强",
        status_icon="🛑",
    )

    assert "🛑 关闭执行\n  - 原因=缺少特征快照路径" in dashboard
    assert "🎯 特征快照校验阻止执行" in dashboard


def test_build_dashboard_localizes_qqq_tech_diagnostics_for_zh():
    dashboard = build_dashboard(
        positions={},
        account_values={"equity": 1000.0, "buying_power": 500.0},
        signal_desc=(
            "regime=soft_defense breadth=41.2% benchmark_trend=down "
            "target_stock=60.0% realized_stock=60.0% selected=8 top=CIEN(0.92)"
        ),
        status_desc="regime=soft_defense | breadth=41.2% | target_stock=60.0% | realized_stock=60.0%",
        strategy_profile="tech_communication_pullback_enhancement",
        target_weights={},
        signal_metadata={
            "allocation": _weight_allocation({}, safe_haven_symbols=("BOXX",)),
        },
        translator=build_translator("zh"),
        separator="---",
        strategy_display_name="科技通信回调增强",
    )

    assert "🐤 市场阶段=软防御\n  - 市场宽度=41.2%" in dashboard
    assert "  - 目标股票仓位=60.0%" in dashboard
    assert "  - 实际股票仓位=60.0%" in dashboard
    assert "基准趋势=向下" in dashboard
    assert "入选标的数=8 前排标的=CIEN(0.92)" in dashboard


def test_notification_trade_lines_suppress_runtime_diagnostic_tail_for_zh():
    lines = _build_notification_trade_lines(
        [
            (
                "执行配置=soxl_soxx_trend_income | 市场阶段=<none> | 宽度=0.0% | "
                "目标股票仓位=0.0% | 实际股票仓位=0.0% | 快照日期=<none> | 交易日=<none>"
            )
        ],
        execution_summary={},
        translator=build_translator("zh"),
    )

    assert lines == []


def test_notification_trade_lines_include_no_order_reason_for_small_account_zh():
    lines = _build_notification_trade_lines(
        [],
        execution_summary={"no_op_reason": "min_notional:BOXX,QQQ,TQQQ"},
        translator=build_translator("zh"),
    )

    assert lines == ["未下单: 原因=低于最小订单金额:BOXX,QQQ,TQQQ"]


def test_notification_trade_lines_include_no_order_reason_for_small_account_en():
    lines = _build_notification_trade_lines(
        [],
        execution_summary={"no_op_reason": "min_notional:BOXX,QQQ,TQQQ"},
        translator=build_translator("en"),
    )

    assert lines == ["No order submitted: reason=min_notional:BOXX,QQQ,TQQQ"]


def test_trade_notification_localizes_compact_signal_state_for_zh_and_en():
    common_kwargs = dict(
        dashboard="",
        strategy_dashboard="",
        trade_logs=[],
        execution_summary={},
        signal_desc="entry",
        status_desc="entry",
        status_icon="🐤",
        separator="---",
        strategy_display_name="TQQQ Growth Income",
        extra_notification_lines=(),
    )

    zh_notification = render_trade_notification(
        **common_kwargs,
        translator=build_translator("zh"),
    )
    en_notification = render_trade_notification(
        **common_kwargs,
        translator=build_translator("en"),
    )

    assert "🎯 入场信号" not in zh_notification.compact_text
    assert "🎯 Entry Signal" not in en_notification.compact_text


def test_run_strategy_core_passes_signal_metadata_to_execution():
    observed = {"messages": [], "strategy_symbols": None}

    class FakeIB:
        def isConnected(self):
            return True

        def disconnect(self):
            observed["disconnected"] = True

    def fake_execute_rebalance(
        _ib,
        _weights,
        _positions,
        _account_values,
        *,
        strategy_symbols=None,
        signal_metadata=None,
    ):
        observed["strategy_symbols"] = strategy_symbols
        observed["signal_metadata"] = signal_metadata
        return []

    result = run_strategy_core(
        connect_ib=lambda: FakeIB(),
        get_current_portfolio=lambda _ib: ({}, {"equity": 1000.0, "buying_power": 500.0}),
        compute_signals=lambda _ib, _holdings: (
            {"AAA": 0.9, "BOXX": 0.1},
            "signal",
            False,
            "breadth=60.0%",
            {
                "managed_symbols": ("AAA", "BOXX"),
                "status_icon": "📏",
                "signal_date": "2026-04-01",
                "effective_date": "2026-04-02",
                "execution_timing_contract": "next_trading_day",
                "execution_annotations": {
                    "dashboard_text": (
                        "📌 Strategy portfolio\n"
                        "  - Total assets (strategy symbols + cash): $1,000.00\n"
                        "  - Buying power: $500.00\n"
                        "💼 Strategy holdings\n"
                        "  - AAA: $0.00 / 0 shares\n"
                        "  - BOXX: $0.00 / 0 shares"
                    )
                },
                "allocation": _weight_allocation(
                    {"AAA": 0.9, "BOXX": 0.1},
                    risk_symbols=("AAA",),
                    safe_haven_symbols=("BOXX",),
                ),
            },
        ),
        execute_rebalance=fake_execute_rebalance,
        send_tg_message=lambda message: observed["messages"].append(message),
        config=IBKRRebalanceConfig(
            translator=_build_test_translator(),
            separator="---",
            strategy_display_name="Global ETF Rotation",
            notify_no_trade_cycles=False,
        ),
    )

    assert result.result == "OK - executed"
    assert observed["strategy_symbols"] == ("AAA", "BOXX")
    assert observed["signal_metadata"]["managed_symbols"] == ("AAA", "BOXX")
    assert observed["messages"] == []


def test_run_strategy_core_writes_reconciliation_record(tmp_path):
    observed = {"messages": []}

    class FakeIB:
        def isConnected(self):
            return True

        def disconnect(self):
            return None

    output_path = tmp_path / "reconciliation.json"

    result = run_strategy_core(
        connect_ib=lambda: FakeIB(),
        get_current_portfolio=lambda _ib: ({}, {"equity": 1000.0, "buying_power": 500.0}),
        compute_signals=lambda _ib, _holdings: (
            {"AAA": 0.6, "BOXX": 0.4},
            "signal",
            False,
            "breadth=41.0%",
            {
                "strategy_profile": "tech_communication_pullback_enhancement",
                "managed_symbols": ("AAA", "BOXX"),
                "status_icon": "🧲",
                "trade_date": "2026-04-01",
                "snapshot_as_of": "2026-03-31",
                "snapshot_guard_decision": "proceed",
                "regime": "soft_defense",
                "breadth_ratio": 0.41,
                "target_stock_weight": 0.6,
                "realized_stock_weight": 0.6,
                "safe_haven_weight": 0.4,
                "safe_haven_symbol": "BOXX",
                "dry_run_only": True,
                "allocation": _weight_allocation(
                    {"AAA": 0.6, "BOXX": 0.4},
                    risk_symbols=("AAA",),
                    safe_haven_symbols=("BOXX",),
                ),
            },
        ),
        execute_rebalance=lambda *_args, **_kwargs: (
            ["DRY_RUN buy AAA 1 @100.00"],
            {
                "mode": "dry_run",
                "execution_status": "executed",
                "orders_submitted": [{"symbol": "AAA", "side": "buy", "quantity": 1, "status": "dry_run"}],
                "orders_filled": [],
                "orders_partially_filled": [],
                "orders_skipped": [],
                "skipped_reasons": [],
                "residual_cash_estimate": 400.0,
                "realized_safe_haven_weight": 0.4,
                "price_source_mode": "mixed_market_quote_snapshot_close",
                "snapshot_price_fallback_used": True,
                "snapshot_price_fallback_count": 1,
                "snapshot_price_fallback_symbols": ["AAA"],
                "target_vs_current": [{"symbol": "AAA", "current_weight": 0.0, "target_weight": 0.6, "delta_weight": 0.6}],
            },
        ),
        send_tg_message=lambda message: observed["messages"].append(message),
        translator=_build_test_translator(),
        separator="---",
        strategy_display_name="Tech/Communication Pullback Enhancement",
        reconciliation_output_path=output_path,
    )

    assert result.result == "OK - executed"
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["strategy_profile"] == "tech_communication_pullback_enhancement"
    assert payload["snapshot_as_of"] == "2026-03-31"
    assert payload["orders_submitted"][0]["symbol"] == "AAA"
    assert payload["snapshot_price_fallback_used"] is True
    assert payload["snapshot_price_fallback_symbols"] == ["AAA"]
    assert "dry_run_buy_batch count=1 details=AAA 1" in observed["messages"][0]
    assert "target_changes AAA +60.0%" in observed["messages"][0]
    assert "DRY_RUN buy AAA 1 @100.00" not in observed["messages"][0]
    assert "目标差异" not in observed["messages"][0]


def test_run_strategy_core_propagates_blocked_execution_status(tmp_path):
    class FakeIB:
        def isConnected(self):
            return True

        def disconnect(self):
            return None

    reason = "submit_failed:AAA:Rejected"
    result = run_strategy_core(
        connect_ib=lambda: FakeIB(),
        get_current_portfolio=lambda _ib: ({}, {"equity": 1000.0, "buying_power": 1000.0}),
        compute_signals=lambda _ib, _holdings: (
            {"AAA": 1.0},
            "signal",
            False,
            "breadth=60.0%",
            {
                "strategy_profile": "tech_communication_pullback_enhancement",
                "managed_symbols": ("AAA",),
                "trade_date": "2026-04-01",
                "snapshot_as_of": "2026-03-31",
                "allocation": _weight_allocation({"AAA": 1.0}, risk_symbols=("AAA",)),
            },
        ),
        execute_rebalance=lambda *_args, **_kwargs: (
            [f"failed {reason}"],
            {
                "execution_status": "blocked",
                "no_op_reason": reason,
                "orders_submitted": [],
                "orders_filled": [],
                "orders_partially_filled": [],
                "orders_skipped": [{"symbol": "AAA", "reason": "Rejected"}],
                "skipped_reasons": [reason],
            },
        ),
        send_tg_message=lambda _message: None,
        config=IBKRRebalanceConfig(
            translator=_build_test_translator(),
            separator="---",
            notify_no_trade_cycles=False,
            reconciliation_output_path=tmp_path / "reconciliation.json",
        ),
    )

    assert result.result == f"Blocked - {reason}"
    assert result.execution_summary["execution_status"] == "blocked"


def test_trade_notification_keeps_detailed_logs_out_of_compact_message():
    notification = render_trade_notification(
        dashboard="📌 Strategy portfolio\n  - Total assets: $1,000.00",
        strategy_dashboard=(
            "📌 Strategy portfolio\n"
            "  - Total assets: $1,000.00\n"
            "⏱ Timing: 2026-04-01 -> 2026-04-02 (next trading day)"
        ),
        trade_logs=[
            (
                "execution_profile=tqqq_growth_income | regime=<none> | breadth=0.0% | "
                "target_stock=0.0% | realized_stock=0.0% | snapshot_as_of=<none> | trade_date=2026-04-02"
            )
        ],
        execution_summary={"no_op_reason": "min_notional:QQQ,TQQQ"},
        signal_desc="entry | small account warning",
        status_desc="entry",
        status_icon="🐤",
        translator=_build_test_translator(),
        separator="---",
        strategy_display_name="TQQQ Growth Income",
        extra_notification_lines=(
            "🆔 Account: U1234567",
            "🧩 Plugin: Crisis Watch Notice | enabled: yes | status: no crisis detected | notice: no action",
        ),
    )

    assert "execution_profile=tqqq_growth_income" in notification.detailed_text
    assert "execution_profile=tqqq_growth_income" not in notification.compact_text
    assert "📌 Strategy portfolio" in notification.compact_text
    assert "⏱ Timing:" not in notification.compact_text
    assert "no_order_plan_reason reason=min_notional:QQQ,TQQQ" in notification.compact_text
    assert notification.compact_text.index("🆔 Account: U1234567") < notification.compact_text.index("🧩 Plugin:")


def test_strategy_dashboard_relabels_total_assets_when_margin_is_enabled():
    dashboard = _strategy_dashboard_text(
        {
            "cash_only_execution": False,
            "dashboard_text": (
                "📌 策略账户概览\n"
                "  - 总资产（策略标的+现金，不含融资额度）: $50,000.00\n"
                "  - 可用现金: $75,000.00"
            ),
        },
        translator=build_translator("zh"),
    )

    assert "总资产（策略净值）: $50,000.00" in dashboard
    assert "购买力: $75,000.00" in dashboard
    assert "不含融资额度" not in dashboard


def test_trade_notification_includes_abnormal_order_batch():
    notification = render_trade_notification(
        dashboard="dashboard",
        strategy_dashboard="dashboard",
        trade_logs=(),
        execution_summary={
            "mode": "paper",
            "execution_status": "executed",
            "orders_submitted": [],
            "orders_filled": [],
            "orders_partially_filled": [],
            "orders_skipped": [
                {
                    "symbol": "SOXL",
                    "side": "buy",
                    "quantity": 1,
                    "status": "Cancelled",
                    "reason": "Cancelled",
                },
            ],
            "target_vs_current": [],
        },
        signal_desc="signal",
        status_desc="status",
        status_icon="🐤",
        translator=_build_test_translator(),
        separator="---",
        strategy_display_name="SOXL/SOXX Semiconductor Trend Income",
        extra_notification_lines=(),
    )

    assert "failed_buy_batch details=SOXL 1 (order cancelled)" in notification.compact_text


def test_trade_notification_uses_deferred_batch_for_quantity_zero_skip():
    notification = render_trade_notification(
        dashboard="dashboard",
        strategy_dashboard="dashboard",
        trade_logs=(),
        execution_summary={
            "mode": "paper",
            "execution_status": "executed",
            "orders_submitted": [
                {"symbol": "SOXL", "side": "buy", "quantity": 6, "status": "Submitted"},
            ],
            "orders_filled": [],
            "orders_partially_filled": [],
            "orders_skipped": [
                {"symbol": "SOXX", "side": "sell", "reason": "quantity_zero"},
            ],
            "target_vs_current": [],
        },
        signal_desc="signal",
        status_desc="status",
        status_icon="🐤",
        translator=_build_test_translator(),
        separator="---",
        strategy_display_name="SOXL/SOXX Semiconductor Trend Income",
        extra_notification_lines=(),
    )

    assert "deferred_sell_batch details=SOXX (quantity_zero)" in notification.compact_text
    assert "failed_sell_batch" not in notification.compact_text


def test_run_strategy_core_writes_reconciliation_record_under_strategy_dir(tmp_path):
    class FakeIB:
        def isConnected(self):
            return True

        def disconnect(self):
            return None

    output_root = tmp_path / "tech_communication_pullback_enhancement" / "reconciliation"

    result = run_strategy_core(
        connect_ib=lambda: FakeIB(),
        get_current_portfolio=lambda _ib: ({}, {"equity": 1000.0, "buying_power": 500.0}),
        compute_signals=lambda _ib, _holdings: (
            None,
            "signal",
            False,
            "outside execution window",
            {
                "strategy_profile": "tech_communication_pullback_enhancement",
                "trade_date": "2026-04-01",
                "snapshot_as_of": "2026-03-31",
                "snapshot_guard_decision": "no_op",
                "no_op_reason": "outside_execution_window",
                "dry_run_only": True,
            },
        ),
        execute_rebalance=lambda *_args, **_kwargs: [],
        send_tg_message=lambda _message: None,
        translator=_build_test_translator(),
        separator="---",
        strategy_display_name="Tech/Communication Pullback Enhancement",
        reconciliation_output_path=output_root,
    )

    assert result.result == "OK - no-op"
    candidate_paths = [
        output_root,
        output_root / "2026-04-01" / "reconciliation.json",
    ]
    payload_path = next((path for path in candidate_paths if path.is_file()), None)
    assert payload_path is not None
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["no_op_reason"] == "outside_execution_window"


def test_run_strategy_core_prefers_structured_noop_status_in_zh():
    observed = {"messages": []}

    class FakeIB:
        def isConnected(self):
            return True

        def disconnect(self):
            return None

    result = run_strategy_core(
        connect_ib=lambda: FakeIB(),
        get_current_portfolio=lambda _ib: ({}, {"equity": 1000.0, "buying_power": 500.0}),
        compute_signals=lambda _ib, _holdings: (
            None,
            "monthly snapshot cadence | waiting inside execution window",
            False,
            "no-op | reason=outside_monthly_execution_window",
            {
                "strategy_profile": "russell_top50_leader_rotation",
                "trade_date": "2026-04-22",
                "snapshot_as_of": "2026-04-16",
                "snapshot_guard_decision": "proceed",
                "no_op_reason": "outside_monthly_execution_window snapshot=2026-04-16 allowed=2026-04-17,2026-04-20,2026-04-21",
                "managed_symbols": ("AAPL", "MSFT", "BOXX"),
                "dry_run_only": True,
                "notification_context": {
                    "signal": {"code": "signal_monthly_snapshot_waiting", "params": {}},
                    "status": {
                        "code": "status_monthly_snapshot_waiting_window",
                        "params": {
                            "snapshot_as_of": "2026-04-16",
                            "allowed_dates": "2026-04-17, 2026-04-20, 2026-04-21",
                        },
                    },
                },
            },
        ),
        execute_rebalance=lambda *_args, **_kwargs: [],
        send_tg_message=lambda message: observed["messages"].append(message),
        translator=build_translator("zh"),
        separator="---",
        strategy_display_name="罗素 Top50 领涨轮动",
    )

    assert result.result == "OK - no-op"
    assert observed["messages"] == []
