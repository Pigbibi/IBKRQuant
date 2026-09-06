from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application.rebalance_service import (
    _is_physical_ibkr_account_id,
    _resolve_physical_account_id,
)
from application.runtime_dependencies import IBKRRebalanceConfig
from notifications.telegram import build_translator
from quant_platform_kit.common.execution_state import ExecutionMarkerStore, claim_account_owner


class AccountOwnerFenceTests(unittest.TestCase):
    def _config(self, **overrides):
        base = dict(
            translator=build_translator("en"),
            separator="---",
            strategy_profile="global_etf_rotation",
            account_ids=("DU1234567",),
            execution_state_account_scope="PAPER",
        )
        base.update(overrides)
        return IBKRRebalanceConfig(**base)

    def test_rejects_scope_labels_as_account_id(self):
        self.assertFalse(_is_physical_ibkr_account_id("PAPER"))
        self.assertFalse(_is_physical_ibkr_account_id("LIVE"))
        self.assertFalse(_is_physical_ibkr_account_id("paper-group"))
        self.assertTrue(_is_physical_ibkr_account_id("DU1234567"))

    def test_resolve_requires_exactly_one_physical_id(self):
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            _resolve_physical_account_id(
                config=self._config(account_ids=("DU1", "DU2")),
                snapshot=type("S", (), {"metadata": {}})(),
            )
        with self.assertRaisesRegex(RuntimeError, "requires configured account_ids"):
            _resolve_physical_account_id(
                config=self._config(account_ids=()),
                snapshot=type("S", (), {"metadata": {}})(),
            )

    def test_contested_owner_primitive_blocks_second_profile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ExecutionMarkerStore(local_dir=tmpdir, cloud_prefix_uri=None)
            first = claim_account_owner(
                store,
                broker="ibkr",
                account_id="DU1234567",
                owner_id="global_etf_rotation",
            )
            second = claim_account_owner(
                store,
                broker="ibkr",
                account_id="DU1234567",
                owner_id="other_profile",
            )
            self.assertTrue(first.allowed)
            self.assertFalse(second.allowed)
            self.assertTrue(second.contested)


if __name__ == "__main__":
    unittest.main()
