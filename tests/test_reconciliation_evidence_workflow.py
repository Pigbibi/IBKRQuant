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
    assert "gcloud scheduler jobs create http" in workflow
    assert 'schedule="$(date -u -d "+${delay_minutes} minutes"' in workflow
    assert "gcloud scheduler jobs delete" in workflow
    assert "gcloud storage cat \"$report_uri\"" in workflow
    assert "gcloud scheduler jobs run" not in workflow
    assert "/run" not in workflow
    assert "curl " not in workflow
