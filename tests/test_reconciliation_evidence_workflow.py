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


def test_reconciliation_evidence_records_receipt_source_without_self_reference() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    required_markers = (
        'gcloud logging read',
        '--format=json',
        'log_entry=',
        'serving_revision',
        'gcloud run revisions describe "$serving_revision"',
        'id: receipt_artifact',
        'steps.receipt_artifact.outputs.artifact-id',
        'steps.receipt_artifact.outputs.artifact-digest',
        'Generate reconciliation source record',
        'Upload reconciliation source record',
        'ibkr_reconciliation_source_receipt_record.v1',
        'schema_version',
        'service_revision_commit_sha',
        'service_deploy_run_id',
        'does not prove scheduler correlation or that this workflow run created the revision',
    )
    for marker in required_markers:
        assert marker in workflow

    record_fields = (
        'schema_version',
        'repository',
        'workflow_path',
        'workflow_run_id',
        'workflow_run_attempt',
        'workflow_head_sha',
        'artifact_id',
        'artifact_name',
        'artifact_sha256',
        'evidence_sha256',
        'service_name',
        'service_revision',
        'service_revision_commit_sha',
        'service_deploy_run_id',
    )
    for field in record_fields:
        assert f'--arg {field}' in workflow

    receipt_upload = workflow.index('id: receipt_artifact')
    source_record = workflow.index('Generate reconciliation source record')
    source_upload = workflow.index('Upload reconciliation source record')
    assert workflow.index('Delete temporary internal reconciliation job') < receipt_upload
    assert receipt_upload < source_record < source_upload
    assert 'steps.source_record_artifact.outputs.artifact-digest' not in workflow
