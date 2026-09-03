from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


SCRIPT = Path("scripts/check_qpk_pin_consistency.py")
CI_WORKFLOW = Path(".github/workflows/ci.yml")


def _load_guard_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_qpk_pin_consistency_for_test", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dependency_pin_guard_checks_constraints_and_all_qsl_git_refs() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert '"**/constraints*.txt"' in script
    assert "QSL_REF_RE" in script
    assert "inconsistent QuantStrategyLab dependency pin" in script


def test_dependency_pin_guard_rejects_internal_qsl_git_ref_drift(tmp_path, monkeypatch, capsys) -> None:
    module = _load_guard_module()
    qpk_ref = "0" * 40
    old_strategy_ref = "1" * 40
    new_strategy_ref = "2" * 40
    (tmp_path / "requirements.txt").write_text(
        "quant-platform-kit @ git+https://github.com/QuantStrategyLab/QuantPlatformKit.git@" + qpk_ref + "\n"
        "us-equity-strategies @ git+https://github.com/QuantStrategyLab/UsEquityStrategies.git@"
        + old_strategy_ref
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "constraints.txt").write_text(
        "us-equity-strategies @ git+https://github.com/QuantStrategyLab/UsEquityStrategies.git@"
        + new_strategy_ref
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "fetch_pin", lambda: qpk_ref)
    monkeypatch.setattr(sys, "argv", ["check_qpk_pin_consistency.py"])

    assert module.main() == 1
    output = capsys.readouterr().out
    assert "inconsistent QuantStrategyLab dependency pin for UsEquityStrategies" in output


def test_dependency_pin_guard_prefers_explicit_qpk_pin(monkeypatch) -> None:
    module = _load_guard_module()
    expected_ref = "a" * 40
    monkeypatch.setenv("QPK_EXPECTED_PIN", expected_ref)
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network must not be used")),
    )
    monkeypatch.setattr(
        module.subprocess,
        "check_output",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network must not be used")),
    )

    assert module.fetch_pin() == expected_ref


def test_dependency_pin_guard_is_blocking_in_ci() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    step_start = workflow.index("name: Check QPK pin consistency")
    next_step = workflow.find("\n      - name:", step_start + 1)
    step = workflow[step_start : next_step if next_step != -1 else len(workflow)]

    assert "check_qpk_pin_consistency.py" in step
    assert "continue-on-error" not in step
