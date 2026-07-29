"""IBKR market-data error handling."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Callable, Iterable
from threading import Lock, RLock
from typing import Any


_EXPECTED_ENTITLEMENT_ERROR_CODES = frozenset({10089, 10168})
_SECONDARY_CANCEL_ERROR_CODE = 300
_ERROR_CODE_PATTERN = re.compile(r"^(?:Error|Warning) (\d+), reqId (-?\d+)")
_LOCK_CREATION_GUARD = Lock()
_QUOTE_FETCH_LOCK_ATTR = "_ibkr_market_data_quote_fetch_lock"


def _quote_fetch_lock(wrapper: Any) -> RLock:
    if wrapper is None:
        return RLock()
    with _LOCK_CREATION_GUARD:
        lock = getattr(wrapper, _QUOTE_FETCH_LOCK_ATTR, None)
        if lock is None:
            lock = RLock()
            setattr(wrapper, _QUOTE_FETCH_LOCK_ATTR, lock)
        return lock


class _ExpectedMarketDataErrorTracker:
    def __init__(self, *, wrapper: Any, requested_symbols: set[str]) -> None:
        self.wrapper = wrapper
        self.requested_symbols = requested_symbols
        self.entitlement_request_ids: set[int] = set()

    def _matches_requested_contract(self, request_id: int, contract: Any = None) -> bool:
        resolved_contract = contract
        if resolved_contract is None:
            contracts = getattr(self.wrapper, "_reqId2Contract", {})
            resolved_contract = contracts.get(request_id) if hasattr(contracts, "get") else None
        symbol = str(getattr(resolved_contract, "symbol", "") or "").strip().upper()
        return bool(symbol and symbol in self.requested_symbols)

    def should_suppress_message(self, message: str) -> bool:
        match = _ERROR_CODE_PATTERN.match(message)
        if match is None:
            return False
        error_code = int(match.group(1))
        request_id = int(match.group(2))
        if (
            error_code in _EXPECTED_ENTITLEMENT_ERROR_CODES
            and self._matches_requested_contract(request_id)
        ):
            self.entitlement_request_ids.add(request_id)
            return True
        if (
            error_code == _SECONDARY_CANCEL_ERROR_CODE
            and request_id in self.entitlement_request_ids
        ):
            return True
        return False

    def should_count_event(self, request_id: int, error_code: int, contract: Any) -> bool:
        if (
            error_code in _EXPECTED_ENTITLEMENT_ERROR_CODES
            and self._matches_requested_contract(request_id, contract)
        ):
            self.entitlement_request_ids.add(request_id)
            return True
        return (
            error_code == _SECONDARY_CANCEL_ERROR_CODE
            and request_id in self.entitlement_request_ids
        )


class _InstanceScopedMarketDataLogger(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger, tracker: _ExpectedMarketDataErrorTracker) -> None:
        super().__init__(logger, {})
        self.tracker = tracker

    def log(self, level, msg, *args, **kwargs) -> None:
        if not self.isEnabledFor(level):
            return
        try:
            message = str(msg) % args if args else str(msg)
        except (TypeError, ValueError):
            message = str(msg)
        if self.tracker.should_suppress_message(message):
            return
        super().log(level, msg, *args, **kwargs)


def fetch_quote_snapshots_with_expected_error_summary(
    ib: Any,
    symbols: Iterable[str],
    *,
    fetch_quote_snapshots: Callable[..., dict],
    printer: Callable[[str], None] = print,
    **kwargs,
) -> dict:
    """Collapse expected entitlement errors while preserving unrelated broker errors."""
    normalized_symbols = tuple(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols))
    error_counts: Counter[int] = Counter()
    wrapper = getattr(ib, "wrapper", None)
    tracker = _ExpectedMarketDataErrorTracker(
        wrapper=wrapper,
        requested_symbols=set(normalized_symbols),
    )

    def record_error(req_id, error_code, _error_string, contract) -> None:
        try:
            code = int(error_code)
            request_id = int(req_id)
        except (TypeError, ValueError):
            return
        if tracker.should_count_event(request_id, code, contract):
            error_counts[code] += 1

    error_event = getattr(ib, "errorEvent", None)
    original_logger = getattr(wrapper, "_logger", None)
    quote_fetch_lock = _quote_fetch_lock(wrapper)
    scoped_logger = (
        _InstanceScopedMarketDataLogger(original_logger, tracker)
        if isinstance(original_logger, logging.Logger)
        else None
    )

    with quote_fetch_lock:
        if scoped_logger is not None:
            wrapper._logger = scoped_logger
        if error_event is not None:
            error_event += record_error
        try:
            snapshots = fetch_quote_snapshots(ib, normalized_symbols, **kwargs)
        finally:
            if error_event is not None:
                error_event -= record_error
            if scoped_logger is not None and getattr(wrapper, "_logger", None) is scoped_logger:
                wrapper._logger = original_logger

    if error_counts:
        printer(
            "ibkr_market_data_fallback "
            + json.dumps(
                {
                    "error_counts": {
                        str(code): error_counts[code] for code in sorted(error_counts)
                    },
                    "requested_symbols": len(normalized_symbols),
                    "resolved_symbols": len(snapshots),
                },
                sort_keys=True,
            )
        )
    return snapshots
