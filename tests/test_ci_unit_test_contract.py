import re
from pathlib import Path


_FULL_SUITE_COMMAND = "PYTHONPATH=. PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run --no-sync python -m pytest -q tests"


def test_ci_runs_unsuppressed_full_suite() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    match = re.search(
        r"^      - name: Run unit tests\n(?P<block>.*)\Z",
        workflow,
        flags=re.MULTILINE | re.DOTALL,
    )

    assert match is not None
    step = match.group("block")
    assert _FULL_SUITE_COMMAND in step
    assert "--ignore=" not in step
    assert "|| true" not in step
    assert "continue-on-error" not in step
