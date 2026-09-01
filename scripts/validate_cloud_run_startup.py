#!/usr/bin/env python3
"""Import the production WSGI app with an inert IBKR configuration.

This check performs no network or broker operations. It catches dependency API
drift, module import failures, and Flask route registration conflicts before a
container can be deployed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_smoke_environment() -> None:
    account_group = "startup-smoke"
    os.environ["ACCOUNT_GROUP"] = account_group
    os.environ["IB_ACCOUNT_GROUP_CONFIG_JSON"] = json.dumps(
        {
            "groups": {
                account_group: {
                    "ib_gateway_instance_name": "127.0.0.1",
                    "ib_gateway_mode": "paper",
                    "ib_client_id": 1,
                    "account_ids": ["DU000000"],
                }
            }
        },
        separators=(",", ":"),
    )
    os.environ["IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME"] = ""
    os.environ["RUNTIME_TARGET_JSON"] = json.dumps(
        {
            "platform_id": "ibkr",
            "strategy_profile": "global_etf_rotation",
            "dry_run_only": True,
            "execution_mode": "paper",
            "account_scope": account_group,
        },
        separators=(",", ":"),
    )


def validate_startup() -> None:
    _install_smoke_environment()

    import main

    routes = {
        rule.rule: set(rule.methods) - {"HEAD", "OPTIONS"}
        for rule in main.app.url_map.iter_rules()
    }
    required_routes = {
        "/health": {"GET"},
        "/run": {"POST"},
        "/dry-run": {"GET", "POST"},
        "/probe": {"POST"},
        "/monitor-dispatch": {"GET", "POST"},
    }
    missing_or_invalid = {
        path: {"expected": sorted(methods), "actual": sorted(routes.get(path, set()))}
        for path, methods in required_routes.items()
        if routes.get(path) != methods
    }
    if missing_or_invalid:
        raise RuntimeError(f"Cloud Run route contract is invalid: {missing_or_invalid}")

    print(f"Cloud Run startup validation passed: routes={len(routes)}")


if __name__ == "__main__":
    validate_startup()
