from ai_work_os.master_run import derive_run_status


def _task(status, *, verification="passed", approval=None, required=True):
    return {
        "status": status,
        "verification_status": verification,
        "approval_status": approval,
        "required": required,
    }


def test_queued_tasks_are_ready_not_running():
    assert derive_run_status([_task("queued")]) == "ready"


def test_running_task_keeps_goal_running():
    assert derive_run_status([_task("running")]) == "running"


def test_pending_approval_takes_precedence_over_queued_state():
    assert derive_run_status([
        _task("queued", verification="pending", approval="pending"),
    ]) == "waiting_approval"


def test_failed_required_task_blocks_goal():
    assert derive_run_status([_task("failed")]) == "blocked"


def test_failed_optional_task_does_not_block_success():
    assert derive_run_status([
        _task("succeeded"),
        _task("failed", required=False),
    ]) == "succeeded"


def test_cancelled_required_task_cancels_goal():
    assert derive_run_status([_task("cancelled")]) == "cancelled"


def test_success_without_verification_evidence_is_not_success():
    assert derive_run_status([
        _task("succeeded", verification="not_configured"),
    ]) == "verifying"


def test_verification_failure_blocks_goal():
    assert derive_run_status([
        _task("succeeded", verification="failed"),
    ]) == "blocked"


def test_success_requires_passed_verification_for_every_required_task():
    assert derive_run_status([
        _task("succeeded", verification="passed"),
        _task("succeeded", verification="passed"),
    ]) == "succeeded"


def test_no_required_tasks_remains_in_planning():
    assert derive_run_status([_task("failed", required=False)]) == "planning"


def test_rejected_approval_blocks_goal():
    assert derive_run_status([
        _task("queued", verification="pending", approval="rejected"),
    ]) == "blocked"


def test_revoked_approval_blocks_goal():
    assert derive_run_status([
        _task("queued", verification="pending", approval="revoked"),
    ]) == "blocked"


def test_goal_is_not_reported_succeeded_until_master_run_terminal_transition():
    from ai_work_os.master_run import reported_run_status
    from ai_work_os.models import MasterRun

    rows = [_task("succeeded", verification="passed")]
    run = MasterRun(status="running")
    assert reported_run_status(run, rows) == "verifying"
    run.status = "succeeded"
    assert reported_run_status(run, rows) == "succeeded"
    run.status = "failed"
    assert reported_run_status(run, rows) == "failed"
