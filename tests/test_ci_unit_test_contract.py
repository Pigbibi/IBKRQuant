import re
from pathlib import Path


_LEGACY_FULL_SUITE_COMMAND = (
    "PYTHONPATH=. PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run --no-sync python -m pytest -q tests "
    "--ignore=tests/test_request_handling.py "
    "--ignore=tests/test_event_loop.py "
    "--ignore=tests/test_monitor_dispatcher.py "
    "--ignore=tests/test_notifications.py "
    "--ignore=tests/test_connect_timeout_alert.py"
)


def test_ci_runs_unsuppressed_legacy_full_suite() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    match = re.search(
        r"^      - name: Run unit tests\n(?P<block>.*)\Z",
        workflow,
        flags=re.MULTILINE | re.DOTALL,
    )

    assert match is not None
    step = match.group("block")
    assert _LEGACY_FULL_SUITE_COMMAND in step
    assert "|| true" not in step
    assert "continue-on-error" not in step
