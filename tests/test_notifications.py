from notifications.renderers import _summarize_skipped_orders, build_dashboard
from notifications.telegram import build_strategy_display_name, build_translator, send_telegram_message
from strategy_registry import SUPPORTED_STRATEGY_PROFILES


def test_build_translator_supports_chinese():
    translate = build_translator("zh")
    assert translate("equity") == "净值"
    assert translate("account_summary_title") == "📊 账户摘要"
    assert translate("positions_title") == "💼 当前持仓"
    assert translate("execution_summary_title") == "🧾 执行摘要"
    assert translate("target_weights_title") == "目标持仓"
    assert translate("market_status_risk_on", asset="SOXL") == "🚀 风险开启（SOXL）"
    assert translate("signal_risk_on", window=150, ratio="40.2%") == "SOXL 站上 150 日均线，持有 SOXL，交易层风险仓位 40.2%"
    assert translate("paper_liquidation_only") == "IBKR 模拟账户清仓模式"
    assert translate("paper_liquidation_positions_seen", count=4) == "识别持仓=4"
    assert translate("market_status_blend_gate_risk_on", asset="SOXX+SOXL") == "🚀 风险开启（SOXX+SOXL）"
    assert translate("market_status_blend_gate_overlay_capped", asset="SOXX") == "🧯 风控降档（SOXX）"
    assert (
        translate(
            "signal_blend_gate_risk_on",
            trend_symbol="SOXX",
            window=140,
            soxl_ratio="70.0%",
            soxx_ratio="20.0%",
        )
        == "SOXX 站上 140 日门槛线，持有 SOXL 70.0% + SOXX 20.0%"
    )
    assert (
        translate(
            "blend_gate_reason_volatility_delever",
            symbol="SOXX",
            window=10,
            volatility="55.0%",
            threshold="50.0%",
            redirect_symbol="SOXX",
        )
        == "SOXX 10 日年化波动率 55.0% 高于 50.0%，SOXL 转向 SOXX"
    )
    assert (
        translate(
            "blend_gate_reason_volatility_delever_dynamic",
            symbol="SOXX",
            window=10,
            volatility="61.0%",
            threshold="60.0%",
            threshold_detail=translate(
                "blend_gate_volatility_threshold_detail_dynamic",
                percentile="p95",
                lookback="252",
                floor="50.0%",
                cap="75.0%",
                sample_count="252",
            ),
            redirect_symbol="SOXX",
        )
        == "SOXX 10 日年化波动率 61.0% 高于实际阈值 60.0%（动态 p95，252日窗口，范围 50.0%-75.0%，样本 252），SOXL 转向 SOXX"
    )
    en_translate = build_translator("en")
    assert (
        en_translate(
            "blend_gate_reason_volatility_delever_dynamic",
            symbol="SOXX",
            window=10,
            volatility="61.0%",
            threshold="60.0%",
            threshold_detail=en_translate(
                "blend_gate_volatility_threshold_detail_dynamic",
                percentile="p95",
                lookback="252",
                floor="50.0%",
                cap="75.0%",
                sample_count="252",
            ),
            redirect_symbol="SOXX",
        )
        == "SOXX 10d annualized volatility 61.0% is above effective threshold 60.0% (dynamic p95, 252d lookback, bounded 50.0%-75.0%, samples 252); redirect SOXL to SOXX"
    )
    assert (
        translate(
            "risk_control_tqqq_volatility_delever_applied_dynamic",
            window=5,
            volatility="31.2%",
            threshold="30.0%",
            threshold_detail=translate(
                "blend_gate_volatility_threshold_detail_dynamic",
                percentile="p90",
                lookback="252",
                floor="24.0%",
                cap="36.0%",
                sample_count="252",
            ),
            source_symbol="TQQQ",
            redirect_symbol="QQQM",
            allocation_detail=translate(
                "tqqq_volatility_delever_allocation_detail",
                retained_ratio="25.0%",
                redirect_symbol="QQQM",
                redirected_ratio="75.0%",
            ),
        )
        == "🛡️ 风控: QQQ 5 日年化波动率 31.2% 高于实际阈值 30.0%（动态 p90，252日窗口，范围 24.0%-36.0%，样本 252），TQQQ 转向 QQQM（杠杆仓位：TQQQ 保留 25.0%，QQQM 75.0%）"
    )
    assert (
        en_translate(
            "risk_control_tqqq_volatility_delever_hysteresis_dynamic",
            window=5,
            volatility="26.2%",
            exit_threshold="24.0%",
            threshold="30.0%",
            threshold_detail=en_translate(
                "blend_gate_volatility_threshold_detail_dynamic",
                percentile="p90",
                lookback="252",
                floor="24.0%",
                cap="36.0%",
                sample_count="252",
            ),
            source_symbol="TQQQ",
            redirect_symbol="QQQM",
            allocation_detail=en_translate(
                "tqqq_volatility_delever_allocation_detail",
                retained_ratio="0.0%",
                redirect_symbol="QQQM",
                redirected_ratio="100.0%",
            ),
        )
        == "🛡️ Risk control: QQQ 5d annualized volatility 26.2% remains above exit threshold 24.0%; entry effective threshold 30.0% (dynamic p90, 252d lookback, bounded 24.0%-36.0%, samples 252); keep TQQQ redirected to QQQM (leveraged sleeve: TQQQ retained 0.0%, QQQM 100.0%)"
    )
    assert (
        translate(
            "strategy_plugin_line",
            plugin=translate("strategy_plugin_name_crisis_response_shadow"),
            enabled=translate("strategy_plugin_enabled_true"),
            mode=translate("strategy_plugin_mode_shadow"),
            route=translate("strategy_plugin_route_no_action"),
            action=translate("strategy_plugin_action_watch_only"),
        )
        == "🧩 插件：危机观察通知 | 启用：是 | 状态：未触发 | 提醒：仅通知"
    )
    assert (
        translate(
            "strategy_plugin_line",
            plugin=translate("strategy_plugin_name_taco_rebound_shadow"),
            enabled=translate("strategy_plugin_enabled_true"),
            mode=translate("strategy_plugin_mode_shadow"),
            route=translate("strategy_plugin_route_taco_rebound"),
            action=translate("strategy_plugin_action_notify_manual_review"),
        )
        == "🧩 插件：TACO 反弹观察通知 | 启用：是 | 状态：TACO 反弹确认 | 提醒：通知人工复核"
    )
    assert (
        translate(
            "strategy_plugin_line",
            plugin=translate("strategy_plugin_name_market_regime_control"),
            enabled=translate("strategy_plugin_enabled_true"),
            mode=translate("strategy_plugin_mode_shadow"),
            route=translate("strategy_plugin_route_risk_reduced"),
            action=translate("strategy_plugin_action_delever"),
        )
        == "🧩 插件：市场状态控制 | 启用：是 | 状态：风险降低 | 提醒：降杠杆"
    )
    assert translate("strategy_plugin_alert_guidance", guidance="小仓位博弈") == "处置建议：小仓位博弈"
    assert translate("strategy_plugin_alert_scope_note", scope_note="不会自动下单") == "执行范围：不会自动下单"
    assert "降低杠杆" in translate("strategy_plugin_guidance_crisis_response_shadow_true_crisis_defend")
    assert "策略侧已批准" in translate("strategy_plugin_guidance_market_regime_control_risk_reduced_delever")
    assert "小仓位" in translate("strategy_plugin_guidance_taco_rebound_shadow_taco_rebound_notify_manual_review")
    assert translate("account_ids_detail", account_ids="U1234567") == "🆔 账户: U1234567"
    assert (
        translate(
            "small_account_warning_note",
            portfolio_equity="$0",
            min_recommended_equity="$1,000",
            reason=translate(
                "small_account_warning_reason_integer_shares_min_position_value_may_prevent_backtest_replication"
            ),
        )
        == "小账户提示：净值 $0 低于建议 $1,000；整数股和最小仓位限制可能导致实盘无法完全复现回测"
    )


def test_skip_order_batch_templates_stay_locale_pure():
    zh = build_translator("zh")
    en = build_translator("en")
    skipped = [{"symbol": "SOXX", "side": "sell", "reason": "quantity_zero"}]

    zh_details = _summarize_skipped_orders(skipped, translator=zh)
    en_details = _summarize_skipped_orders(skipped, translator=en)

    assert zh_details == "SOXX（整数股不足 1 股，无需下单）"
    assert en_details == "SOXX (whole-share quantity rounds to 0; no order needed)"
    assert "（" in zh_details
    assert "(" in en_details
    assert "（" not in en_details
    assert zh("deferred_sell_batch", details=zh_details) == f"ℹ️ [卖出跳过] {zh_details}"
    assert en("deferred_sell_batch", details=en_details) == f"ℹ️ [Sell skipped] {en_details}"
    assert (
        en("failed_sell_batch", details=en("skip_order_detail_with_qty", symbol="SOXL", quantity="1", reason="order cancelled"))
        == "❌ [Sell failed] SOXL 1 (order cancelled)"
    )


def test_strategy_display_name_translates_new_live_profiles():
    zh_name = build_strategy_display_name(build_translator("zh"))
    en_name = build_strategy_display_name(build_translator("en"))

    assert zh_name("global_etf_confidence_vol_gate") == "全球 ETF 置信波动门控"
    assert en_name("global_etf_confidence_vol_gate") == "Global ETF Confidence Vol Gate"
    assert zh_name("russell_top50_leader_rotation") == "罗素 Top50 领涨轮动"
    assert en_name("russell_top50_leader_rotation") == "Russell Top50 Leader Rotation"
    assert zh_name("nasdaq_sp500_smart_dca") == "纳指100 / 标普500 智能定投"
    assert en_name("nasdaq_sp500_smart_dca") == "Nasdaq 100 / S&P 500 Smart DCA"
    assert zh_name("hk_global_etf_tactical_rotation") == "港股全球 ETF 战术轮动"
    assert en_name("hk_global_etf_tactical_rotation") == "HK Global ETF Tactical Rotation"
    assert zh_name("hk_low_vol_dividend_quality_snapshot") == "港股低波股息质量快照"
    assert en_name("hk_low_vol_dividend_quality_snapshot") == "HK Low-Vol Dividend Quality Snapshot"


def test_supported_strategy_profiles_have_translated_names():
    zh_name = build_strategy_display_name(build_translator("zh"))
    en_name = build_strategy_display_name(build_translator("en"))

    for profile in SUPPORTED_STRATEGY_PROFILES:
        assert zh_name(profile) != profile
        assert en_name(profile) != profile


def test_dashboard_renders_tqqq_volatility_delever_risk_control():
    dashboard = build_dashboard(
        positions={},
        account_values={"equity": 10000.0, "buying_power": 1000.0},
        signal_desc="Entry signal",
        status_desc="Entry signal",
        strategy_profile="tqqq_growth_income",
        strategy_display_name="TQQQ Growth Income",
        signal_metadata={
            "dashboard_text": "📌 Strategy account overview",
            "dual_drive_volatility_delever_applied": True,
            "dual_drive_volatility_delever_window": 5,
            "dual_drive_volatility_delever_metric": 0.312,
            "dual_drive_volatility_delever_threshold": 0.28,
            "dual_drive_volatility_delever_threshold_mode": "rolling_percentile",
            "dual_drive_volatility_delever_dynamic_threshold": 0.30,
            "dual_drive_volatility_delever_dynamic_sample_count": 252,
            "dual_drive_volatility_delever_dynamic_lookback": 252,
            "dual_drive_volatility_delever_dynamic_percentile": 0.90,
            "dual_drive_volatility_delever_dynamic_min_periods": 126,
            "dual_drive_volatility_delever_dynamic_floor": 0.24,
            "dual_drive_volatility_delever_dynamic_cap": 0.36,
            "dual_drive_volatility_delever_redirect_symbol": "QQQM",
            "dual_drive_volatility_delever_retained_ratio": 0.0,
            "dual_drive_volatility_delever_redirected_ratio": 1.0,
        },
        translator=build_translator("en"),
        separator="━━━━━━━━━━━━━━━━━━",
    )

    assert (
        "🛡️ Risk control: QQQ 5d annualized volatility 31.2% is above effective threshold 30.0% "
        "(dynamic p90, 252d lookback, bounded 24.0%-36.0%, samples 252); TQQQ redirects to QQQM "
        "(leveraged sleeve: TQQQ retained 0.0%, QQQM 100.0%)"
    ) in dashboard


def test_send_telegram_message_logs_non_200_response(capsys):
    class FakeResponse:
        status_code = 401
        text = "unauthorized"

    class FakeRequests:
        @staticmethod
        def post(*args, **kwargs):
            return FakeResponse()

    sent = send_telegram_message(
        "hello",
        token="token",
        chat_id="chat-id",
        requests_module=FakeRequests,
    )

    assert sent is False
    captured = capsys.readouterr()
    assert "Telegram send failed with status 401: unauthorized" in captured.out


def test_send_telegram_message_breaks_market_symbol_auto_links():
    calls = []

    class FakeResponse:
        status_code = 200
        text = ""

    class FakeRequests:
        @staticmethod
        def post(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeResponse()

    sent = send_telegram_message(
        "SOXL.US 预计；00700.HK 持仓；https://example.com 保持原样",
        token="token",
        chat_id="chat-id",
        requests_module=FakeRequests,
    )

    assert sent is True
    payload = calls[0][1]["json"]
    assert payload["text"] == "SOXL.\u2060US 预计；00700.\u2060HK 持仓；https://example.com 保持原样"


def test_send_telegram_message_redacts_exception_detail(capsys):
    class FakeRequests:
        @staticmethod
        def post(*args, **kwargs):
            raise RuntimeError("failed https://api.telegram.org/bottoken/sendMessage")

    send_telegram_message(
        "hello",
        token="token",
        chat_id="chat-id",
        requests_module=FakeRequests,
    )

    captured = capsys.readouterr()
    assert "Telegram send failed: RuntimeError" in captured.out
    assert "token" not in captured.out
    assert "api.telegram.org" not in captured.out
