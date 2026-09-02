import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
QPK_SRC = ROOT.parent / "QuantPlatformKit" / "src"
UES_SRC = ROOT.parent / "UsEquityStrategies" / "src"
HES_SRC = ROOT.parent / "HkEquityStrategies" / "src"
for candidate in (ROOT, QPK_SRC, UES_SRC, HES_SRC):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import strategy_runtime as strategy_runtime_module
from quant_platform_kit.common.models import PortfolioSnapshot
from quant_platform_kit.common.runtime_target import build_runtime_target
from quant_platform_kit.strategy_contracts import (
    PositionTarget,
    StrategyDecision,
    StrategyManifest,
    StrategyRuntimeAdapter,
    StrategyRuntimePolicy,
)
from runtime_config_support import PlatformRuntimeSettings


class _FakeStrategyPluginSignal:
    plugin = "taco_rebound_shadow"
    execution_controls = {"strategy_runtime_metadata_allowed": True}
    payload = {
        "plugin": "taco_rebound_shadow",
        "canonical_route": "taco_rebound",
        "rebound_context_active": True,
    }

    def report_summary(self):
        return {"plugin": self.plugin, "canonical_route": "taco_rebound"}


def _build_runtime_settings(
    profile: str = "tech_communication_pullback_enhancement",
    *,
    display_name: str = "Tech/Communication Pullback Enhancement",
    target_mode: str = "weight",
    income_layer_enabled: bool | None = None,
    income_layer_start_usd: float | None = None,
    income_layer_max_ratio: float | None = None,
    dca_mode: str | None = None,
    dca_base_investment_usd: float | None = None,
    ibit_zscore_exit_enabled: bool | None = None,
    ibit_zscore_exit_mode: str | None = None,
    ibit_zscore_exit_parking_symbol: str | None = None,
    ibit_zscore_exit_risk_reduced_exposure: float | None = None,
    ibit_zscore_exit_risk_off_exposure: float | None = None,
    ibit_zscore_exit_allow_outside_execution_window: bool | None = None,
    reserved_cash_floor_usd: float = 0.0,
    reserved_cash_ratio: float | None = None,
) -> PlatformRuntimeSettings:
    return PlatformRuntimeSettings(
        project_id=None,
        ib_gateway_instance_name="127.0.0.1",
        ib_gateway_zone="",
        ib_gateway_mode="live",
        ib_gateway_port=4001,
        ib_gateway_ip_mode="internal",
        ib_client_id=1,
        strategy_profile=profile,
        strategy_display_name=display_name,
        strategy_domain="us_equity",
        strategy_target_mode=target_mode,
        strategy_artifact_root=None,
        strategy_artifact_dir=None,
        feature_snapshot_path="/tmp/snapshot.csv",
        feature_snapshot_manifest_path=None,
        strategy_config_path="/tmp/config.json",
        strategy_config_source="env",
        reconciliation_output_path=None,
        dry_run_only=True,
        reserved_cash_floor_usd=reserved_cash_floor_usd,
        reserved_cash_ratio=reserved_cash_ratio,
        income_layer_enabled=income_layer_enabled,
        income_layer_start_usd=income_layer_start_usd,
        income_layer_max_ratio=income_layer_max_ratio,
        dca_mode=dca_mode,
        dca_base_investment_usd=dca_base_investment_usd,
        ibit_zscore_exit_enabled=ibit_zscore_exit_enabled,
        ibit_zscore_exit_mode=ibit_zscore_exit_mode,
        ibit_zscore_exit_parking_symbol=ibit_zscore_exit_parking_symbol,
        ibit_zscore_exit_risk_reduced_exposure=ibit_zscore_exit_risk_reduced_exposure,
        ibit_zscore_exit_risk_off_exposure=ibit_zscore_exit_risk_off_exposure,
        ibit_zscore_exit_allow_outside_execution_window=(
            ibit_zscore_exit_allow_outside_execution_window
        ),
        account_group="default",
        service_name=None,
        account_ids=(),
        tg_token=None,
        tg_chat_id=None,
        notify_lang="en",
    )


def test_loaded_runtime_attaches_v2_capital_base_only_for_verified_ibkr_net_liquidation():
    entrypoint = SimpleNamespace(manifest=SimpleNamespace(profile="soxl_soxx_trend_income"))
    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=entrypoint,
        runtime_settings=replace(
            _build_runtime_settings(profile="soxl_soxx_trend_income"),
            runtime_target=build_runtime_target(
                platform_id="ibkr",
                strategy_profile="soxl_soxx_trend_income",
                dry_run_only=True,
                account_scope="paper-account-scope",
                service_name="ibkr-paper-service",
                execution_environment="dry_run",
            ),
        ),
        runtime_adapter=StrategyRuntimeAdapter(),
        logger=lambda _message: None,
    )
    verified_snapshot = PortfolioSnapshot(
        as_of=strategy_runtime_module.pd.Timestamp("2026-08-27", tz="UTC").to_pydatetime(),
        total_equity=1_000.0,
        metadata={
            "total_equity_source": "broker_net_liquidation",
            "source_digest_sha256": "b" * 64,
        },
    )

    capabilities, status = runtime._build_capital_base_capabilities(verified_snapshot)

    assert status == "verified:broker_account_net_liquidation"
    assert capabilities["capital_base"].to_safe_dict()["valuation_basis"] == "broker_account_net_liquidation"
    assert capabilities["capital_base_binding"].capital_scope.value == "account"

    unverified = replace(
        verified_snapshot,
        metadata={"total_equity_source": "unverified_net_liquidation"},
    )
    capabilities, status = runtime._build_capital_base_capabilities(unverified)
    assert capabilities == {}
    assert status == "unavailable:unverified_net_liquidation"


def test_main_compute_signals_uses_strategy_runtime_decision(strategy_module, monkeypatch):
    observed = {}

    class FakeRuntime:
        def evaluate(
            self,
            *,
            ib,
            current_holdings,
            historical_close_loader,
            historical_candle_loader,
            run_as_of,
            translator,
            pacing_sec,
        ):
            observed["ib"] = ib
            observed["current_holdings"] = tuple(sorted(current_holdings))
            observed["run_as_of"] = str(run_as_of.date())
            observed["pacing_sec"] = pacing_sec
            observed["translator_sample"] = translator("equity")
            observed["historical_loader"] = historical_close_loader
            observed["historical_candle_loader"] = historical_candle_loader
            return type(
                "Evaluation",
                (),
                {
                    "decision": StrategyDecision(
                        positions=(
                            PositionTarget(symbol="AAA", target_weight=0.8),
                            PositionTarget(symbol="BIL", target_weight=0.2, role="safe_haven"),
                        ),
                        diagnostics={
                            "signal_description": "rotation signal",
                            "status_description": "canary=ok",
                        },
                    ),
                    "metadata": {
                        "strategy_profile": "global_etf_rotation",
                        "managed_symbols": ("AAA", "BIL"),
                        "status_icon": "🐤",
                        "dry_run_only": False,
                    },
                },
            )()

    monkeypatch.setattr(strategy_module, "STRATEGY_RUNTIME", FakeRuntime())
    monkeypatch.setattr(strategy_module, "resolve_run_as_of_date", lambda: strategy_module.pd.Timestamp("2026-04-07"))

    result = strategy_module.compute_signals("fake-ib", {"AAA"})

    assert result[0] == {"AAA": 0.8, "BIL": 0.2}
    assert result[1] == "rotation signal"
    assert result[2] is False
    assert result[3] == "canary=ok"
    assert result[4]["managed_symbols"] == ("AAA", "BIL")
    assert observed["ib"] == "fake-ib"
    assert observed["current_holdings"] == ("AAA",)
    assert observed["run_as_of"] == "2026-04-07"
    assert observed["pacing_sec"] == strategy_module.HIST_DATA_PACING_SEC
    assert observed["translator_sample"]
    assert callable(observed["historical_candle_loader"])


def test_main_compute_signals_passes_strategy_plugin_signals_to_runtime(strategy_module, monkeypatch):
    observed = {}

    class FakeRuntime:
        def evaluate(
            self,
            *,
            ib,
            current_holdings,
            historical_close_loader,
            historical_candle_loader,
            run_as_of,
            translator,
            pacing_sec,
            strategy_plugin_signals=(),
        ):
            del ib, current_holdings, historical_close_loader, historical_candle_loader, run_as_of, translator, pacing_sec
            observed["strategy_plugin_signals"] = strategy_plugin_signals
            return type(
                "Evaluation",
                (),
                {
                    "decision": StrategyDecision(
                        positions=(PositionTarget(symbol="BOXX", target_weight=1.0),),
                        diagnostics={"signal_description": "defense", "status_description": "plugin-aware"},
                    ),
                    "metadata": {
                        "strategy_profile": "global_etf_rotation",
                        "managed_symbols": ("BOXX",),
                        "status_icon": "🐤",
                        "dry_run_only": False,
                    },
                },
            )()

    signal = _FakeStrategyPluginSignal()
    monkeypatch.setattr(strategy_module, "STRATEGY_RUNTIME", FakeRuntime())
    monkeypatch.setattr(strategy_module, "resolve_run_as_of_date", lambda: strategy_module.pd.Timestamp("2026-04-07"))

    result = strategy_module.compute_signals("fake-ib", {"AAA"}, strategy_plugin_signals=(signal,))

    assert result[0] == {"BOXX": 1.0}
    assert observed["strategy_plugin_signals"] == (signal,)


def test_loaded_strategy_runtime_attaches_strategy_plugin_metadata_to_portfolio(monkeypatch):
    observed = {}

    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="tqqq_growth_income",
            domain="us_equity",
            display_name="TQQQ Growth Income",
            description="test",
            required_inputs=frozenset({"market_history"}),
            default_config={"managed_symbols": ("TQQQ", "QQQ", "BOXX")},
        )

        def evaluate(self, ctx):
            observed["portfolio_metadata"] = dict(ctx.portfolio.metadata)
            return StrategyDecision(
                positions=(PositionTarget(symbol="BOXX", target_weight=1.0),),
                diagnostics={"signal_description": "defense", "status_description": "plugin-aware"},
            )

    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=FakeEntrypoint(),
        runtime_settings=_build_runtime_settings(profile="tqqq_growth_income", display_name="TQQQ Growth Income"),
        runtime_adapter=StrategyRuntimeAdapter(
            available_inputs=frozenset({"market_history"}),
            portfolio_input_name="portfolio_snapshot",
            runtime_policy=StrategyRuntimePolicy(),
        ),
        merged_runtime_config={"managed_symbols": ("TQQQ", "QQQ", "BOXX")},
        logger=lambda _message: None,
    )
    monkeypatch.setattr(
        strategy_runtime_module,
        "fetch_portfolio_snapshot",
        lambda _ib: PortfolioSnapshot(
            as_of=strategy_runtime_module.pd.Timestamp("2026-04-07").to_pydatetime(),
            total_equity=100000.0,
            metadata={"account_hash": "demo"},
        ),
    )

    runtime.evaluate(
        ib=object(),
        current_holdings={"TQQQ"},
        historical_close_loader=lambda *_args, **_kwargs: strategy_runtime_module.pd.Series(dtype=float),
        historical_candle_loader=lambda *_args, **_kwargs: (),
        run_as_of=strategy_runtime_module.pd.Timestamp("2026-04-07"),
        translator=lambda value, **_kwargs: value,
        pacing_sec=0.0,
        strategy_plugin_signals=(_FakeStrategyPluginSignal(),),
    )

    assert observed["portfolio_metadata"]["account_hash"] == "demo"
    assert observed["portfolio_metadata"]["taco_rebound_shadow"]["canonical_route"] == "taco_rebound"
    assert (
        observed["portfolio_metadata"]["strategy_plugins"]["taco_rebound_shadow"]["rebound_context_active"]
        is True
    )


def test_load_strategy_runtime_uses_entrypoint_defaults_and_runtime_adapter(monkeypatch):
    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="tech_communication_pullback_enhancement",
            domain="us_equity",
            display_name="Tech/Communication Pullback Enhancement",
            description="test",
            required_inputs=frozenset({"feature_snapshot"}),
            default_config={
                "safe_haven": "BOXX",
                "benchmark_symbol": "QQQ",
                "execution_rebalance_threshold_ratio": 0.0,
            },
        )

        def evaluate(self, ctx):
            return StrategyDecision()

    monkeypatch.setattr(
        strategy_runtime_module,
        "load_strategy_definition",
        lambda raw_profile: SimpleNamespace(profile="tech_communication_pullback_enhancement"),
    )
    monkeypatch.setattr(
        strategy_runtime_module,
        "load_strategy_entrypoint_for_profile",
        lambda raw_profile: FakeEntrypoint(),
    )
    monkeypatch.setattr(
        strategy_runtime_module,
        "load_strategy_runtime_adapter_for_profile",
        lambda raw_profile: StrategyRuntimeAdapter(
            status_icon="🧲",
            runtime_parameter_loader=lambda **_kwargs: {
                "benchmark_symbol": "SPY",
                "rebalance_months": (1, 4, 7, 10),
            },
        ),
    )

    runtime = strategy_runtime_module.load_strategy_runtime(
        "tech_communication_pullback_enhancement",
        runtime_settings=_build_runtime_settings(
            income_layer_enabled=False,
            income_layer_start_usd=250000.0,
            income_layer_max_ratio=0.25,
        ),
        logger=lambda _message: None,
    )

    assert runtime.entrypoint.manifest.profile == "tech_communication_pullback_enhancement"
    assert runtime.runtime_config["benchmark_symbol"] == "SPY"
    assert runtime.runtime_config["income_layer_enabled"] is False
    assert runtime.runtime_config["income_layer_start_usd"] == 250000.0
    assert runtime.runtime_config["income_layer_max_ratio"] == 0.25
    assert runtime.merged_runtime_config["safe_haven"] == "BOXX"
    assert runtime.merged_runtime_config["benchmark_symbol"] == "SPY"
    assert runtime.merged_runtime_config["income_layer_enabled"] is False
    assert runtime.merged_runtime_config["income_layer_start_usd"] == 250000.0
    assert runtime.merged_runtime_config["income_layer_max_ratio"] == 0.25
    assert runtime.merged_runtime_config["rebalance_months"] == (1, 4, 7, 10)
    assert runtime.rebalance_threshold_ratio == 0.0
    assert runtime.status_icon == "🧲"


def test_load_strategy_runtime_loads_explicit_config_for_market_data_profile(monkeypatch):
    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="us_equity_combo_leveraged",
            domain="quant_combo",
            display_name="US Equity Combo Leveraged",
            description="test",
            required_inputs=frozenset({"market_data"}),
            default_config={"dynamic": True},
        )

        def evaluate(self, ctx):
            return StrategyDecision()

    monkeypatch.setattr(
        strategy_runtime_module,
        "load_strategy_definition",
        lambda raw_profile: SimpleNamespace(profile="us_equity_combo_leveraged"),
    )
    monkeypatch.setattr(
        strategy_runtime_module,
        "load_strategy_entrypoint_for_profile",
        lambda raw_profile: FakeEntrypoint(),
    )
    monkeypatch.setattr(
        strategy_runtime_module,
        "load_strategy_runtime_adapter_for_profile",
        lambda raw_profile: StrategyRuntimeAdapter(
            runtime_parameter_loader=lambda **_kwargs: {
                "tqqq_weight": 0.35,
                "soxl_weight": 0.20,
                "boxx_weight": 0.45,
                "hard_defense_risk_exposure": 0.0,
            },
        ),
    )

    runtime = strategy_runtime_module.load_strategy_runtime(
        "us_equity_combo_leveraged",
        runtime_settings=_build_runtime_settings(profile="us_equity_combo_leveraged"),
        logger=lambda _message: None,
    )

    assert runtime.runtime_config["tqqq_weight"] == 0.35
    assert runtime.runtime_config["hard_defense_risk_exposure"] == 0.0
    assert runtime.merged_runtime_config["dynamic"] is True
    assert runtime.merged_runtime_config["boxx_weight"] == 0.45


def test_direct_market_data_strategy_builds_spy_ma200_context():
    captured = {}

    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="us_equity_combo_leveraged",
            domain="quant_combo",
            display_name="US Equity Combo Leveraged",
            description="test",
            required_inputs=frozenset({"market_data"}),
            default_config={},
        )

        def evaluate(self, ctx):
            captured["market_data"] = dict(ctx.market_data)
            captured["runtime_config"] = dict(ctx.runtime_config)
            return StrategyDecision(
                positions=(
                    PositionTarget(symbol="TQQQ", target_weight=0.35),
                    PositionTarget(symbol="SOXL", target_weight=0.20),
                    PositionTarget(symbol="BOXX", target_weight=0.45),
                )
            )

    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=FakeEntrypoint(),
        runtime_settings=_build_runtime_settings(profile="us_equity_combo_leveraged"),
        runtime_adapter=StrategyRuntimeAdapter(
            status_icon="🇺🇸",
            available_inputs=frozenset({"market_data"}),
            runtime_policy=StrategyRuntimePolicy(signal_effective_after_trading_days=0),
        ),
        runtime_config={"market_data_ma_window": 200},
        merged_runtime_config={"market_data_ma_window": 200},
        status_icon="🇺🇸",
        logger=lambda _message: None,
    )

    def fake_close_loader(_ib, symbol, **_kwargs):
        if symbol in {"SPY", "QQQ", "SOXX"}:
            return strategy_runtime_module.pd.Series([100.0] * 200 + [120.0])
        return strategy_runtime_module.pd.Series([10.0] * 201)

    result = runtime.evaluate(
        ib=None,
        current_holdings=set(),
        historical_close_loader=fake_close_loader,
        historical_candle_loader=None,
        run_as_of=strategy_runtime_module.pd.Timestamp("2026-07-02"),
        translator=lambda value, **_kwargs: value,
        pacing_sec=0.0,
    )

    market_data = captured["market_data"]["market_data"]
    assert market_data["spy_above_ma200"] is True
    assert market_data["qqq_above_ma200"] is True
    assert market_data["soxx_above_ma200"] is True
    assert market_data["spy_ma20_slope_positive"] is True
    assert market_data["qqq_ma20_slope_positive"] is True
    assert market_data["soxx_ma20_slope_positive"] is True
    assert market_data["trend_symbol"] == "SPY"
    assert market_data["trend_symbols"] == ("SPY", "QQQ", "SOXX")
    assert market_data["trend_ma_window"] == 200
    assert captured["runtime_config"]["market_data_ma_window"] == 200
    assert result.metadata["managed_symbols"] == ("TQQQ", "SOXL", "BOXX")
    assert result.metadata["safe_haven_symbol"] == "BOXX"


def test_dca_overrides_apply_to_runtime_config():
    settings = _build_runtime_settings(
        profile="nasdaq_sp500_smart_dca",
        display_name="Nasdaq 100 / S&P 500 DCA",
        target_mode="value",
        dca_mode="smart",
        dca_base_investment_usd=500.0,
    )

    assert strategy_runtime_module._build_runtime_overrides(settings) == {
        "investment_amount_mode": "fixed",
        "smart_multiplier_enabled": True,
        "base_investment_usd": 500.0,
    }


def test_ibit_zscore_exit_overrides_apply_to_runtime_config():
    settings = _build_runtime_settings(
        profile="ibit_smart_dca",
        display_name="IBIT Bitcoin ETF DCA",
        target_mode="value",
        ibit_zscore_exit_enabled=True,
        ibit_zscore_exit_mode="live",
        ibit_zscore_exit_parking_symbol="BOXX",
        ibit_zscore_exit_risk_reduced_exposure=0.5,
        ibit_zscore_exit_risk_off_exposure=0.25,
        ibit_zscore_exit_allow_outside_execution_window=True,
    )

    assert strategy_runtime_module._build_runtime_overrides(settings) == {
        "ibit_zscore_exit_enabled": True,
        "ibit_zscore_exit_mode": "live",
        "ibit_zscore_exit_parking_symbol": "BOXX",
        "ibit_zscore_exit_risk_reduced_exposure": 0.5,
        "ibit_zscore_exit_risk_off_exposure": 0.25,
        "ibit_zscore_exit_allow_outside_execution_window": True,
    }


def test_reserved_cash_policy_overrides_apply_to_runtime_config():
    settings = _build_runtime_settings(
        profile="soxl_soxx_trend_income",
        display_name="SOXL/SOXX Semiconductor Trend Income",
        reserved_cash_floor_usd=150.0,
        reserved_cash_ratio=0.03,
    )

    assert strategy_runtime_module._build_runtime_overrides(settings) == {
        "reserved_cash_floor_usd": 150.0,
        "reserved_cash_ratio": 0.03,
    }


def test_dca_overrides_are_ignored_for_non_dca_profiles():
    settings = _build_runtime_settings(
        profile="tqqq_growth_income",
        dca_mode="smart",
        dca_base_investment_usd=500.0,
    )

    assert strategy_runtime_module._build_runtime_overrides(settings) == {}


def test_option_chain_fetch_uses_merged_manifest_overlay_defaults(monkeypatch):
    observed = {}

    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="tqqq_growth_income",
            domain="us_equity",
            display_name="TQQQ",
            description="test",
            required_inputs=frozenset({"benchmark_history", "portfolio_snapshot"}),
            default_config={},
        )

    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=FakeEntrypoint(),
        runtime_adapter=StrategyRuntimeAdapter(),
        runtime_settings=_build_runtime_settings(profile="tqqq_growth_income"),
        runtime_config={},
        merged_runtime_config={
            "option_growth_overlay_enabled": True,
            "option_growth_overlay_recipe": "tqqq_leaps_growth_v1",
            "option_growth_overlay_start_usd": 250000.0,
        },
        logger=lambda _message: None,
    )

    def fake_fetch_option_chain_snapshot(_ib, underlier, **kwargs):
        observed["underlier"] = underlier
        observed["kwargs"] = kwargs
        return {"underlier": underlier, "contracts": ()}

    monkeypatch.setattr(
        strategy_runtime_module.LoadedStrategyRuntime,
        "_option_overlay_recipe_live_allowed",
        staticmethod(lambda _recipe: True),
    )
    monkeypatch.setattr(strategy_runtime_module, "fetch_option_chain_snapshot", fake_fetch_option_chain_snapshot)
    chains = runtime._fetch_option_chains_for_runtime(
        object(),
        {},
        SimpleNamespace(total_equity=300000.0),
    )

    assert observed["underlier"] == "TQQQ"
    assert observed["kwargs"]["rights"] == ("C",)
    assert chains["TQQQ"]["underlier"] == "TQQQ"


def test_option_chain_fetch_skips_research_only_overlay_defaults(monkeypatch):
    observed = {}

    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="tqqq_growth_income",
            domain="us_equity",
            display_name="TQQQ",
            description="test",
            required_inputs=frozenset({"benchmark_history", "portfolio_snapshot"}),
            default_config={},
        )

    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=FakeEntrypoint(),
        runtime_adapter=StrategyRuntimeAdapter(),
        runtime_settings=_build_runtime_settings(profile="tqqq_growth_income"),
        runtime_config={},
        merged_runtime_config={
            "option_growth_overlay_enabled": True,
            "option_growth_overlay_recipe": "tqqq_leaps_growth_v1",
            "option_growth_overlay_start_usd": 250000.0,
        },
        logger=lambda _message: None,
    )

    def fake_fetch_option_chain_snapshot(_ib, underlier, **kwargs):
        observed["underlier"] = underlier
        observed["kwargs"] = kwargs
        return {"underlier": underlier, "contracts": ()}

    monkeypatch.setattr(strategy_runtime_module, "fetch_option_chain_snapshot", fake_fetch_option_chain_snapshot)

    assert runtime._fetch_option_chains_for_runtime(
        object(),
        {},
        SimpleNamespace(total_equity=300000.0),
    ) == {}
    assert observed == {}


def test_feature_snapshot_runtime_prefers_unified_runtime_adapter_metadata(monkeypatch):
    captured = {}

    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="tech_communication_pullback_enhancement",
            domain="us_equity",
            display_name="Tech/Communication Pullback Enhancement",
            description="test",
            required_inputs=frozenset({"feature_snapshot"}),
            default_config={"safe_haven": "BOXX", "benchmark_symbol": "QQQ"},
        )

        def evaluate(self, ctx):
            captured["market_data"] = dict(ctx.market_data)
            captured["portfolio"] = ctx.portfolio
            captured["runtime_config"] = dict(ctx.runtime_config)
            return StrategyDecision()

    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=FakeEntrypoint(),
        runtime_adapter=StrategyRuntimeAdapter(
            status_icon="🧲",
            required_feature_columns=frozenset({"symbol", "close"}),
            snapshot_date_columns=("as_of",),
            max_snapshot_month_lag=2,
            require_snapshot_manifest=True,
            snapshot_contract_version="adapter.contract",
            managed_symbols_extractor=lambda *_args, **_kwargs: ("AAPL", "BOXX"),
        ),
        runtime_settings=_build_runtime_settings(),
        runtime_config={},
        merged_runtime_config={"safe_haven": "BOXX", "benchmark_symbol": "QQQ"},
        status_icon="🧲",
        logger=lambda _message: None,
    )

    def fake_guard(path, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            frame=[{"as_of": "2026-03-31", "symbol": "AAPL", "close": 1.0}],
            metadata={
                "snapshot_guard_decision": "proceed",
                "snapshot_as_of": "2026-03-31",
                "snapshot_path": path,
                "snapshot_age_days": 1,
            },
        )

    monkeypatch.setattr(strategy_runtime_module, "load_feature_snapshot_guarded", fake_guard)
    portfolio_snapshot = SimpleNamespace(total_equity=25000.0)
    monkeypatch.setattr(strategy_runtime_module, "fetch_portfolio_snapshot", lambda _ib: portfolio_snapshot)

    result = runtime.evaluate(
        ib="fake-ib",
        current_holdings={"AAPL"},
        historical_close_loader=lambda *_args, **_kwargs: None,
        run_as_of=strategy_runtime_module.pd.Timestamp("2026-04-01"),
        translator=lambda key, **_kwargs: key,
        pacing_sec=0.5,
    )

    assert captured["required_columns"] == frozenset({"symbol", "close"})
    assert captured["snapshot_date_columns"] == ("as_of",)
    assert captured["max_snapshot_month_lag"] == 2
    assert captured["require_manifest"] is True
    assert captured["expected_contract_version"] == "adapter.contract"
    assert captured["market_data"]["feature_snapshot"] == [{"as_of": "2026-03-31", "symbol": "AAPL", "close": 1.0}]
    assert captured["portfolio"] is portfolio_snapshot
    assert captured["runtime_config"]["translator"]("equity") == "equity"
    assert "pacing_sec" not in captured["runtime_config"]
    assert result.metadata["managed_symbols"] == ("AAPL", "BOXX")


def test_feature_snapshot_runtime_can_add_daily_market_benchmark_and_portfolio_inputs(monkeypatch):
    captured = {}

    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="hybrid_snapshot_history_profile",
            domain="us_equity",
            display_name="Hybrid Snapshot History Profile",
            description="test",
            required_inputs=frozenset({"feature_snapshot", "market_history", "benchmark_history", "portfolio_snapshot"}),
            default_config={"safe_haven": "BOXX", "benchmark_symbol": "QQQ"},
        )

        def evaluate(self, ctx):
            captured["market_data"] = dict(ctx.market_data)
            captured["portfolio"] = ctx.portfolio
            captured["capabilities"] = dict(ctx.capabilities)
            return StrategyDecision(
                positions=(
                    PositionTarget(symbol="NVDL", target_weight=0.4),
                    PositionTarget(symbol="BOXX", target_weight=0.6, role="safe_haven"),
                ),
                diagnostics={"signal_description": "leveraged", "status_description": "ok"},
            )

    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=FakeEntrypoint(),
        runtime_adapter=StrategyRuntimeAdapter(
            status_icon="2x",
            available_inputs=frozenset({"feature_snapshot", "market_history", "benchmark_history", "portfolio_snapshot"}),
            required_feature_columns=frozenset({"symbol", "underlying_symbol"}),
            snapshot_date_columns=("as_of",),
            max_snapshot_month_lag=1,
            require_snapshot_manifest=False,
            snapshot_contract_version=None,
            managed_symbols_extractor=lambda *_args, **_kwargs: ("NVDL", "BOXX"),
            portfolio_input_name="portfolio_snapshot",
        ),
        runtime_settings=_build_runtime_settings(profile="hybrid_snapshot_history_profile"),
        runtime_config={},
        merged_runtime_config={"safe_haven": "BOXX", "benchmark_symbol": "QQQ"},
        status_icon="2x",
        logger=lambda _message: None,
    )

    monkeypatch.setattr(
        strategy_runtime_module,
        "load_feature_snapshot_guarded",
        lambda path, **_kwargs: SimpleNamespace(
            frame=[{"as_of": "2026-03-31", "symbol": "NVDL", "underlying_symbol": "NVDA"}],
            metadata={
                "snapshot_guard_decision": "proceed",
                "snapshot_as_of": "2026-03-31",
                "snapshot_path": path,
                "snapshot_age_days": 1,
            },
        ),
    )
    portfolio_snapshot = SimpleNamespace(total_equity=50000.0)
    monkeypatch.setattr(strategy_runtime_module, "fetch_portfolio_snapshot", lambda _ib: portfolio_snapshot)

    def close_loader(_ib, symbol, **_kwargs):
        return [100.0, 101.0] if symbol == "NVDA" else []

    def candle_loader(_ib, symbol, duration="2 Y", bar_size="1 day"):
        assert symbol == "QQQ"
        assert duration == "2 Y"
        assert bar_size == "1 day"
        return [{"close": 100.0, "high": 101.0, "low": 99.0}]

    result = runtime.evaluate(
        ib="fake-ib",
        current_holdings={"NVDL"},
        historical_close_loader=close_loader,
        historical_candle_loader=candle_loader,
        run_as_of=strategy_runtime_module.pd.Timestamp("2026-04-01"),
        translator=lambda key, **_kwargs: key,
        pacing_sec=0.5,
    )

    assert captured["market_data"]["feature_snapshot"][0]["symbol"] == "NVDL"
    assert captured["market_data"]["market_history"] is close_loader
    assert captured["market_data"]["benchmark_history"][0]["high"] == 101.0
    assert captured["portfolio"] is portfolio_snapshot
    assert captured["capabilities"]["broker_client"] == "fake-ib"
    assert result.metadata["managed_symbols"] == ("NVDL", "BOXX")


def test_market_history_runtime_uses_canonical_market_history_key(monkeypatch):
    captured = {}

    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="global_etf_rotation",
            domain="us_equity",
            display_name="Global ETF Rotation",
            description="test",
            required_inputs=frozenset({"market_history"}),
            default_config={"safe_haven": "BIL", "ranking_pool": ("AAA",)},
        )

        def evaluate(self, ctx):
            captured["market_data"] = dict(ctx.market_data)
            captured["portfolio"] = ctx.portfolio
            captured["runtime_config"] = dict(ctx.runtime_config)
            return StrategyDecision()

    def loader(*_args, **_kwargs):
        return None

    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=FakeEntrypoint(),
        runtime_adapter=StrategyRuntimeAdapter(
            status_icon="🐤",
            runtime_policy=StrategyRuntimePolicy(signal_effective_after_trading_days=1),
        ),
        runtime_settings=_build_runtime_settings(profile="global_etf_rotation"),
        runtime_config={},
        merged_runtime_config={"safe_haven": "BIL", "ranking_pool": ("AAA",)},
        status_icon="🐤",
        logger=lambda _message: None,
    )
    portfolio_snapshot = SimpleNamespace(total_equity=1200.0)
    monkeypatch.setattr(strategy_runtime_module, "fetch_portfolio_snapshot", lambda _ib: portfolio_snapshot)

    result = runtime.evaluate(
        ib="fake-ib",
        current_holdings={"AAA"},
        historical_close_loader=loader,
        run_as_of=strategy_runtime_module.pd.Timestamp("2026-04-01"),
        translator=lambda key, **_kwargs: key,
        pacing_sec=0.5,
    )

    assert captured["market_data"]["market_history"] is loader
    assert captured["portfolio"] is portfolio_snapshot
    assert "historical_close_loader" not in captured["market_data"]
    assert captured["runtime_config"]["signal_effective_after_trading_days"] == 1
    assert result.metadata["signal_date"] == "2026-04-01"
    assert result.metadata["effective_date"] == "2026-04-02"
    assert result.metadata["execution_timing_contract"] == "next_trading_day"


def test_hk_direct_market_history_runtime_materializes_loader(monkeypatch):
    captured = {}
    observed_calls = []

    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="hk_global_etf_tactical_rotation",
            domain="hk_equity",
            display_name="HK Global ETF Tactical Rotation",
            description="test",
            required_inputs=frozenset({"market_history"}),
            default_config={"universe_symbols": ("02800", "02834")},
        )

        def evaluate(self, ctx):
            captured["market_data"] = dict(ctx.market_data)
            return StrategyDecision()

    def close_loader(ib, symbol, **_kwargs):
        observed_calls.append((ib, symbol))
        return strategy_runtime_module.pd.Series(
            [10.0, 11.0],
            index=strategy_runtime_module.pd.to_datetime(
                ["2026-05-29", "2026-06-01"],
                utc=True,
            ),
            dtype=float,
        )

    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=FakeEntrypoint(),
        runtime_adapter=StrategyRuntimeAdapter(
            status_icon="🇭🇰",
            runtime_policy=StrategyRuntimePolicy(signal_effective_after_trading_days=1),
        ),
        runtime_settings=_build_runtime_settings(profile="hk_global_etf_tactical_rotation"),
        runtime_config={},
        merged_runtime_config={"universe_symbols": ("02800", "02834")},
        status_icon="🇭🇰",
        logger=lambda _message: None,
    )
    monkeypatch.setattr(
        strategy_runtime_module,
        "_requires_materialized_market_history",
        lambda profile: profile == "hk_global_etf_tactical_rotation",
    )

    runtime.evaluate(
        ib="fake-ib",
        current_holdings=set(),
        historical_close_loader=close_loader,
        run_as_of=strategy_runtime_module.pd.Timestamp("2026-04-01"),
        translator=lambda key, **_kwargs: key,
        pacing_sec=0.5,
    )

    market_history = captured["market_data"]["market_history"]
    assert observed_calls == [("fake-ib", "02800"), ("fake-ib", "02834")]
    assert sorted(market_history) == ["02800", "02834"]
    assert market_history["02800"][0]["date"] == strategy_runtime_module.pd.Timestamp(
        "2026-05-29",
        tz="UTC",
    )
    assert market_history["02800"][0]["close"] == 10.0


def test_market_history_value_runtime_requires_portfolio_snapshot(monkeypatch):
    captured = {}

    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="nasdaq_sp500_smart_dca",
            domain="us_equity",
            display_name="Nasdaq 100 / S&P 500 Smart DCA",
            description="test",
            required_inputs=frozenset({"market_history", "portfolio_snapshot"}),
            default_config={"managed_symbols": ("QQQM", "SPLG")},
        )

        def evaluate(self, ctx):
            captured["market_data"] = dict(ctx.market_data)
            captured["portfolio"] = ctx.portfolio
            captured["runtime_config"] = dict(ctx.runtime_config)
            return StrategyDecision(
                positions=(
                    PositionTarget(symbol="QQQM", target_value=1500.0),
                    PositionTarget(symbol="SPLG", target_value=1700.0),
                )
            )

    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=FakeEntrypoint(),
        runtime_adapter=StrategyRuntimeAdapter(
            status_icon="🧺",
            portfolio_input_name="portfolio_snapshot",
            runtime_policy=StrategyRuntimePolicy(signal_effective_after_trading_days=0),
        ),
        runtime_settings=_build_runtime_settings(
            profile="nasdaq_sp500_smart_dca",
            display_name="Nasdaq 100 / S&P 500 Smart DCA",
            target_mode="value",
        ),
        runtime_config={},
        merged_runtime_config={"managed_symbols": ("QQQM", "SPLG")},
        status_icon="🧺",
        logger=lambda _message: None,
    )
    portfolio_snapshot = SimpleNamespace(total_equity=10000.0)

    def fetch_snapshot(ib):
        assert ib == "fake-ib"
        return portfolio_snapshot

    close_loader_symbols = []

    def close_loader(_ib, symbol, duration="2 Y", bar_size="1 day"):
        close_loader_symbols.append((symbol, duration, bar_size))
        return strategy_runtime_module.pd.Series([100.0, 101.0])

    monkeypatch.setattr(strategy_runtime_module, "fetch_portfolio_snapshot", fetch_snapshot)

    result = runtime.evaluate(
        ib="fake-ib",
        current_holdings=(),
        historical_close_loader=close_loader,
        run_as_of=strategy_runtime_module.pd.Timestamp("2026-05-26"),
        translator=lambda key, **_kwargs: key,
        pacing_sec=0.0,
    )

    assert captured["market_data"]["market_history"] is close_loader
    assert captured["portfolio"] is portfolio_snapshot
    assert captured["runtime_config"]["signal_effective_after_trading_days"] == 0
    assert result.metadata["portfolio_total_equity"] == 10000.0
    assert result.metadata["managed_symbols"] == ("QQQM", "SPLG")
    assert result.metadata["signal_date"] == "2026-05-26"
    assert result.metadata["effective_date"] == "2026-05-26"
    assert result.metadata["execution_timing_contract"] == "same_trading_day"
    assert close_loader_symbols == [("QQQM", "10 D", "1 day"), ("SPLG", "10 D", "1 day")]


def test_feature_snapshot_runtime_fail_closes_on_entrypoint_exception(monkeypatch):
    class ExplodingEntrypoint:
        manifest = StrategyManifest(
            profile="tech_communication_pullback_enhancement",
            domain="us_equity",
            display_name="Tech/Communication Pullback Enhancement",
            description="test",
            required_inputs=frozenset({"feature_snapshot"}),
            default_config={"safe_haven": "BOXX"},
        )

        def evaluate(self, ctx):
            raise RuntimeError("boom")

    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=ExplodingEntrypoint(),
        runtime_adapter=StrategyRuntimeAdapter(
            status_icon="📏",
            required_feature_columns=frozenset(),
            snapshot_date_columns=("as_of",),
            max_snapshot_month_lag=1,
            require_snapshot_manifest=False,
            snapshot_contract_version=None,
            managed_symbols_extractor=lambda *_args, **_kwargs: ("AAA", "BOXX"),
        ),
        runtime_settings=_build_runtime_settings(),
        runtime_config={},
        merged_runtime_config={"safe_haven": "BOXX"},
        status_icon="📏",
        logger=lambda _message: None,
    )

    monkeypatch.setattr(
        strategy_runtime_module,
        "load_feature_snapshot_guarded",
        lambda path, **_kwargs: SimpleNamespace(
            frame=[{"as_of": "2026-03-31", "symbol": "AAA"}],
            metadata={
                "snapshot_guard_decision": "proceed",
                "snapshot_as_of": "2026-03-31",
                "snapshot_path": path,
                "snapshot_age_days": 1,
            },
        ),
    )

    result = runtime.evaluate(
        ib=None,
        current_holdings={"AAA"},
        historical_close_loader=lambda *_args, **_kwargs: None,
        run_as_of=strategy_runtime_module.pd.Timestamp("2026-04-01"),
        translator=lambda key, **_kwargs: key,
        pacing_sec=0.5,
    )

    assert result.metadata["snapshot_guard_decision"] == "fail_closed"
    assert "feature_snapshot_compute_failed:RuntimeError:boom" in result.metadata["fail_reason"]
    assert result.decision.diagnostics["signal_description"] == "feature snapshot compute failed"


def test_value_target_runtime_builds_semiconductor_inputs(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "quant_platform_kit.strategy_lifecycle.live_equity.resolve_consecutive_losses",
        lambda **_kwargs: 0,
    )

    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="soxl_soxx_trend_income",
            domain="us_equity",
            display_name="SOXL/SOXX Semiconductor Trend Income",
            description="test",
            required_inputs=frozenset({"derived_indicators", "portfolio_snapshot"}),
            default_config={"managed_symbols": ("SOXL", "SOXX", "QQQI", "SPYI", "BOXX")},
        )

        def evaluate(self, ctx):
            captured["market_data"] = dict(ctx.market_data)
            captured["portfolio"] = ctx.portfolio
            captured["runtime_config"] = dict(ctx.runtime_config)
            return StrategyDecision(
                positions=(
                    PositionTarget(symbol="SOXL", target_value=30000.0),
                    PositionTarget(symbol="BOXX", target_value=20000.0, role="safe_haven"),
                )
            )

    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=FakeEntrypoint(),
        runtime_adapter=StrategyRuntimeAdapter(
            status_icon="🚀",
            portfolio_input_name="portfolio_snapshot",
            runtime_policy=StrategyRuntimePolicy(signal_effective_after_trading_days=1),
        ),
        runtime_settings=_build_runtime_settings(profile="soxl_soxx_trend_income"),
        runtime_config={},
        merged_runtime_config={"managed_symbols": ("SOXL", "SOXX", "QQQI", "SPYI", "BOXX")},
        status_icon="🚀",
        logger=lambda _message: None,
    )

    portfolio_snapshot = PortfolioSnapshot(
        as_of=strategy_runtime_module.pd.Timestamp("2026-04-01").to_pydatetime(),
        total_equity=50000.0,
        buying_power=50000.0,
        cash_balance=50000.0,
        positions=(),
    )
    monkeypatch.setattr(strategy_runtime_module, "fetch_portfolio_snapshot", lambda _ib: portfolio_snapshot)

    def fake_loader(_ib, symbol, duration="2 Y", bar_size="1 day"):
        if symbol == "SOXL":
            return [100.0] * 170
        if symbol == "SOXX":
            return [200.0] * 170
        raise AssertionError(symbol)

    result = runtime.evaluate(
        ib="fake-ib",
        current_holdings={"SOXL"},
        historical_close_loader=fake_loader,
        run_as_of=strategy_runtime_module.pd.Timestamp("2026-04-01"),
        translator=lambda key, **_kwargs: key,
        pacing_sec=0.5,
    )

    assert captured["market_data"]["derived_indicators"]["soxl"]["price"] == 100.0
    assert captured["market_data"]["derived_indicators"]["soxl"]["ma_trend"] == 100.0
    assert captured["market_data"]["derived_indicators"]["soxx"]["price"] == 200.0
    assert "realized_volatility_10" in captured["market_data"]["derived_indicators"]["soxx"]
    assert captured["portfolio"].total_equity == portfolio_snapshot.total_equity
    assert captured["portfolio"].metadata["consecutive_losses"] == 0
    assert "pacing_sec" not in captured["runtime_config"]
    assert captured["runtime_config"]["signal_effective_after_trading_days"] == 1
    assert result.metadata["portfolio_total_equity"] == 50000.0
    assert result.metadata["managed_symbols"] == ("SOXL", "SOXX", "QQQI", "SPYI", "BOXX")
    assert result.metadata["signal_date"] == "2026-04-01"
    assert result.metadata["effective_date"] == "2026-04-02"
    assert result.metadata["execution_timing_contract"] == "next_trading_day"


def test_value_target_runtime_builds_tqqq_inputs(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "quant_platform_kit.strategy_lifecycle.live_equity.resolve_consecutive_losses",
        lambda **_kwargs: 0,
    )

    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="tqqq_growth_income",
            domain="us_equity",
            display_name="TQQQ Growth Income",
            description="test",
            required_inputs=frozenset({"benchmark_history", "portfolio_snapshot"}),
            default_config={
                "benchmark_symbol": "QQQ",
                "managed_symbols": ("TQQQ", "QQQ", "BOXX", "SPYI", "QQQI"),
            },
        )

        def evaluate(self, ctx):
            captured["market_data"] = dict(ctx.market_data)
            captured["portfolio"] = ctx.portfolio
            captured["runtime_config"] = dict(ctx.runtime_config)
            return StrategyDecision(
                positions=(
                    PositionTarget(symbol="TQQQ", target_value=30000.0),
                    PositionTarget(symbol="BOXX", target_value=20000.0, role="safe_haven"),
                )
            )

    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=FakeEntrypoint(),
        runtime_adapter=StrategyRuntimeAdapter(
            status_icon="🐤",
            portfolio_input_name="portfolio_snapshot",
            runtime_policy=StrategyRuntimePolicy(signal_effective_after_trading_days=1),
        ),
        runtime_settings=_build_runtime_settings(
            profile="tqqq_growth_income",
            display_name="TQQQ Growth Income",
            target_mode="value",
        ),
        runtime_config={},
        merged_runtime_config={
            "benchmark_symbol": "QQQ",
            "managed_symbols": ("TQQQ", "QQQ", "BOXX", "SPYI", "QQQI"),
        },
        status_icon="🐤",
        logger=lambda _message: None,
    )

    portfolio_snapshot = PortfolioSnapshot(
        as_of=strategy_runtime_module.pd.Timestamp("2026-04-01").to_pydatetime(),
        total_equity=50000.0,
        buying_power=50000.0,
        cash_balance=50000.0,
        positions=(),
    )
    monkeypatch.setattr(strategy_runtime_module, "fetch_portfolio_snapshot", lambda _ib: portfolio_snapshot)

    def fake_candle_loader(_ib, symbol, duration="2 Y", bar_size="1 day"):
        assert symbol == "QQQ"
        assert duration == "2 Y"
        assert bar_size == "1 day"
        return [
            {"close": 100.0, "high": 101.0, "low": 99.0}
            for _ in range(220)
        ]

    close_loader_symbols = []

    def fake_close_loader(_ib, symbol, duration="2 Y", bar_size="1 day"):
        close_loader_symbols.append((symbol, duration, bar_size))
        prices = {"TQQQ": 70.0, "BOXX": 105.0}
        value = prices.get(symbol)
        if value is None:
            return strategy_runtime_module.pd.Series(dtype=float)
        return strategy_runtime_module.pd.Series([value - 1.0, value])

    result = runtime.evaluate(
        ib="fake-ib",
        current_holdings={"TQQQ"},
        historical_close_loader=fake_close_loader,
        historical_candle_loader=fake_candle_loader,
        run_as_of=strategy_runtime_module.pd.Timestamp("2026-04-01"),
        translator=lambda key, **_kwargs: key,
        pacing_sec=0.5,
    )

    assert len(captured["market_data"]["benchmark_history"]) == 220
    assert captured["market_data"]["benchmark_history"][0]["high"] == 101.0
    assert captured["portfolio"].total_equity == portfolio_snapshot.total_equity
    assert captured["portfolio"].metadata["consecutive_losses"] == 0
    assert "pacing_sec" not in captured["runtime_config"]
    assert captured["runtime_config"]["signal_effective_after_trading_days"] == 1
    assert result.metadata["portfolio_total_equity"] == 50000.0
    assert result.metadata["benchmark_symbol"] == "QQQ"
    assert result.metadata["managed_symbols"] == ("TQQQ", "QQQ", "BOXX", "SPYI", "QQQI")
    assert result.metadata["signal_date"] == "2026-04-01"
    assert result.metadata["effective_date"] == "2026-04-02"
    assert result.metadata["execution_timing_contract"] == "next_trading_day"
    assert close_loader_symbols == [("TQQQ", "10 D", "1 day"), ("BOXX", "10 D", "1 day")]
    assert result.metadata["price_fallback_source"] == "historical_close"
    assert result.metadata["price_fallbacks"] == {"TQQQ": 70.0, "BOXX": 105.0}


def test_enrich_portfolio_metadata_includes_unrealized_pnl():
    from datetime import datetime, timezone

    from quant_platform_kit.common.models import Position

    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="tech_communication_pullback_enhancement",
            domain="us_equity",
            display_name="Tech Pullback",
            description="test",
            required_inputs=frozenset(),
        )

        def evaluate(self, ctx):
            return StrategyDecision(positions=())

    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=FakeEntrypoint(),
        runtime_adapter=StrategyRuntimeAdapter(),
        runtime_settings=_build_runtime_settings(),
    )
    snapshot = PortfolioSnapshot(
        as_of=datetime.now(timezone.utc),
        total_equity=10_000.0,
        positions=(
            Position(symbol="SPY", quantity=10.0, market_value=4_500.0, average_cost=500.0),
        ),
        metadata={"consecutive_losses": 2},
    )

    enriched = runtime._enrich_portfolio_metadata({}, snapshot)

    assert enriched["unrealized_pnl_pct"] == -0.05
    assert enriched["consecutive_losses"] == 2
    assert enriched["portfolio_total_equity"] == 10_000.0


def test_with_consecutive_loss_metadata_stamps_from_resolver(monkeypatch):
    from datetime import datetime, timezone

    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="global_etf_rotation",
            domain="us_equity",
            display_name="Global ETF",
            description="test",
            required_inputs=frozenset(),
        )

        def evaluate(self, ctx):
            return StrategyDecision(positions=())

    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=FakeEntrypoint(),
        runtime_adapter=StrategyRuntimeAdapter(),
        runtime_settings=_build_runtime_settings("global_etf_rotation"),
        logger=lambda _msg: None,
    )
    monkeypatch.setattr(
        "quant_platform_kit.strategy_lifecycle.live_equity.resolve_consecutive_losses",
        lambda **_kwargs: 4,
    )
    snapshot = PortfolioSnapshot(
        as_of=datetime.now(timezone.utc),
        total_equity=10_000.0,
        positions=(),
        metadata={},
    )

    stamped = runtime._with_consecutive_loss_metadata(snapshot)

    assert stamped is not snapshot
    assert stamped.metadata["consecutive_losses"] == 4


def test_with_consecutive_loss_metadata_preserves_explicit_value(monkeypatch):
    from datetime import datetime, timezone

    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="global_etf_rotation",
            domain="us_equity",
            display_name="Global ETF",
            description="test",
            required_inputs=frozenset(),
        )

        def evaluate(self, ctx):
            return StrategyDecision(positions=())

    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=FakeEntrypoint(),
        runtime_adapter=StrategyRuntimeAdapter(),
        runtime_settings=_build_runtime_settings("global_etf_rotation"),
        logger=lambda _msg: None,
    )
    called = {"count": 0}

    def _boom(**_kwargs):
        called["count"] += 1
        raise AssertionError("should not resolve when already set")

    monkeypatch.setattr(
        "quant_platform_kit.strategy_lifecycle.live_equity.resolve_consecutive_losses",
        _boom,
    )
    snapshot = PortfolioSnapshot(
        as_of=datetime.now(timezone.utc),
        total_equity=10_000.0,
        positions=(),
        metadata={"consecutive_losses": 1},
    )

    stamped = runtime._with_consecutive_loss_metadata(snapshot)

    assert stamped is snapshot
    assert called["count"] == 0
