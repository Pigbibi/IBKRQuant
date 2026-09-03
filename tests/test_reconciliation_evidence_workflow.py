from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "collect-reconciliation-evidence.yml"
)


def test_reconciliation_evidence_uses_internal_one_shot_scheduler_and_cleans_up() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert '"${SERVICE_URL}/reconcile"' in workflow
    scheduler_command = workflow.split("gcloud scheduler jobs create http", 1)[1].split("--quiet", 1)[0]
    assert "--max-retry-attempts=0" in scheduler_command
    assert "--max-retry-duration=0s" in scheduler_command
    assert 'schedule="$(date -u -d "+${delay_minutes} minutes"' in workflow
    assert "gcloud scheduler jobs delete" in workflow
    assert "gcloud storage cat \"$report_uri\"" in workflow
    assert "gcloud scheduler jobs run" not in workflow
    assert "/run" not in workflow
    assert "curl " not in workflow
