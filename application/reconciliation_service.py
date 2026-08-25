"""Structured reconciliation record helpers for InteractiveBrokersPlatform."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd


def _json_safe(value: Any):
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def default_reconciliation_output_path(strategy_profile: str | None) -> Path:
    profile = str(strategy_profile or "unknown").strip() or "unknown"
    safe_profile = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in profile)
    return Path(tempfile.gettempdir()) / f"ibkr_reconciliation_{safe_profile}.json"


def build_reconciliation_record(
    *,
    strategy_profile: str | None,
    mode: str,
    trade_date: str | None,
    snapshot_as_of,
    signal_metadata: dict[str, Any] | None,
    target_weights: dict[str, float] | None,
    execution_summary: dict[str, Any] | None,
    no_op_reason: str | None = None,
) -> dict[str, Any]:
    signal_metadata = dict(signal_metadata or {})
    execution_summary = dict(execution_summary or {})
    target_weights = dict(target_weights or {})
    allocation = dict(signal_metadata.get("allocation") or {})
    allocation_safe_haven_symbols = tuple(allocation.get("safe_haven_symbols") or ())
    allocation_safe_haven_symbol = allocation_safe_haven_symbols[0] if allocation_safe_haven_symbols else None
    record = {
        "strategy_profile": strategy_profile,
        "mode": mode,
        "trade_date": trade_date,
        "snapshot_as_of": snapshot_as_of,
        "snapshot_guard_decision": signal_metadata.get("snapshot_guard_decision"),
        "snapshot_path": signal_metadata.get("feature_snapshot_path") or signal_metadata.get("snapshot_path"),
        "regime": signal_metadata.get("regime"),
        "breadth": signal_metadata.get("breadth_ratio"),
        "target_stock_weight": signal_metadata.get("target_stock_weight"),
        "realized_stock_weight": signal_metadata.get("realized_stock_weight"),
        "target_safe_haven_weight": signal_metadata.get("safe_haven_weight"),
        "realized_safe_haven_weight": execution_summary.get("realized_safe_haven_weight"),
        "safe_haven_symbol": (
            execution_summary.get("safe_haven_symbol")
            or signal_metadata.get("safe_haven_symbol")
            or allocation_safe_haven_symbol
        ),
        "target_holdings": [
            {"symbol": symbol, "target_weight": float(weight)}
            for symbol, weight in sorted(target_weights.items(), key=lambda item: (-item[1], item[0]))
        ],
        "target_vs_current": execution_summary.get("target_vs_current") or [],
        "orders_submitted": execution_summary.get("orders_submitted") or [],
        "orders_pending": execution_summary.get("orders_pending") or [],
        "orders_filled": execution_summary.get("orders_filled") or [],
        "orders_partially_filled": execution_summary.get("orders_partially_filled") or [],
        "orders_skipped": execution_summary.get("orders_skipped") or [],
        "option_orders_submitted": execution_summary.get("option_orders_submitted") or [],
        "option_orders_pending": execution_summary.get("option_orders_pending") or [],
        "option_orders_filled": execution_summary.get("option_orders_filled") or [],
        "option_orders_partially_filled": execution_summary.get("option_orders_partially_filled") or [],
        "option_orders_skipped": execution_summary.get("option_orders_skipped") or [],
        "skipped_reasons": execution_summary.get("skipped_reasons") or [],
        "residual_cash_estimate": execution_summary.get("residual_cash_estimate"),
        "cash_reserve_dollars": execution_summary.get("cash_reserve_dollars"),
        "current_stock_weight": execution_summary.get("current_stock_weight"),
        "current_safe_haven_weight": execution_summary.get("current_safe_haven_weight"),
        "price_source_mode": execution_summary.get("price_source_mode"),
        "quote_snapshot": execution_summary.get("quote_snapshot") or {},
        "paper_execution_admission": execution_summary.get("paper_execution_admission") or {},
        "snapshot_price_fallback_used": execution_summary.get("snapshot_price_fallback_used"),
        "snapshot_price_fallback_count": execution_summary.get("snapshot_price_fallback_count"),
        "snapshot_price_fallback_symbols": execution_summary.get("snapshot_price_fallback_symbols") or [],
        "execution_status": execution_summary.get("execution_status") or ("no_op" if no_op_reason else "executed"),
        "lock_path": execution_summary.get("lock_path"),
        "no_op_reason": no_op_reason or execution_summary.get("no_op_reason"),
        "fail_reason": signal_metadata.get("fail_reason"),
        "status_icon": signal_metadata.get("status_icon"),
    }
    return _json_safe(record)


def write_reconciliation_record(record: dict[str, Any], *, output_path: str | Path | None = None) -> Path:
    path = Path(output_path) if output_path else default_reconciliation_output_path(record.get("strategy_profile"))
    if output_path and path.suffix.lower() != ".json":
        trade_date = str(record.get("trade_date") or "latest").strip() or "latest"
        path = path / trade_date / "reconciliation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_safe(record), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path
