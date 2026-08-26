from __future__ import annotations

from pathlib import Path
import tomllib


def test_qsl_metadata_has_runtime_platform_fields() -> None:
    qsl_path = Path(__file__).resolve().parents[1] / "qsl.toml"
    with qsl_path.open("rb") as f:
        qsl = tomllib.load(f)["qsl"]

    assert qsl["tier"] == "runtime"
    assert qsl["upgrade_ring"] == "ring_d"
    assert qsl.get("repo") == "InteractiveBrokersPlatform"
    assert qsl["compat"]["bundle"] == "2026.08.0"
    requires = qsl["requires"]
    assert "quant_platform_kit" in requires
    assert "us_equity_strategies" in requires
    assert "hk_equity_strategies" in requires
