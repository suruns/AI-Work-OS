from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Browser, Page, sync_playwright

pytestmark = pytest.mark.e2e


@pytest.fixture
def dashboard(tmp_path: Path, request: pytest.FixtureRequest):
    """Start an isolated API process and Chromium page for one browser test."""
    root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "e2e.sqlite3"
    storage_path = tmp_path / "storage"
    storage_path.mkdir()

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    env = os.environ.copy()
    env.update({
        "AI_WORK_OS_DB": f"sqlite:///{db_path}",
        "AI_WORK_OS_STORAGE": str(storage_path),
        "PYTHONPATH": os.pathsep.join(
            [str(root / "src"), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    })
    server = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "ai_work_os.api:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        cwd=root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if server.poll() is not None:
                stderr = server.stderr.read() if server.stderr else ""
                pytest.fail(f"Dashboard API exited during startup:\n{stderr}")
            try:
                with urllib.request.urlopen(f"{base_url}/health", timeout=0.5) as response:
                    if response.status == 200:
                        break
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        else:
            pytest.fail("Dashboard API did not become healthy within 20 seconds")

        with sync_playwright() as playwright_instance:
            browser: Browser = playwright_instance.chromium.launch(
                headless=True, args=["--no-sandbox"]
            )
            context = browser.new_context(timezone_id="Asia/Shanghai")
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            page = context.new_page()
            try:
                page.goto(base_url, wait_until="domcontentloaded")
                page.locator("#connection").filter(
                    has_text="API connected"
                ).wait_for(timeout=10_000)
                yield {
                    "page": page,
                    "base_url": base_url,
                    "db_path": db_path,
                    "workspace": tmp_path,
                    "context": context,
                }
            finally:
                report = getattr(request.node, "rep_call", None)
                artifact_dir = Path(os.getenv("AI_WORK_OS_E2E_ARTIFACTS", "artifacts/e2e"))
                try:
                    if report and report.failed:
                        artifact_dir.mkdir(parents=True, exist_ok=True)
                        name = request.node.name.replace("/", "_")
                        page.screenshot(
                            path=str(artifact_dir / f"{name}.png"), full_page=True
                        )
                        context.tracing.stop(path=str(artifact_dir / f"{name}.zip"))
                    else:
                        context.tracing.stop()
                finally:
                    context.close()
                    browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        if server.stderr:
            server.stderr.close()


def create_workflow(page: Page, workspace: Path, title: str, steps: list[tuple[str, str]]) -> str:
    page.locator("#workflow-title").fill(title)
    page.locator("#workflow-workspace").fill(str(workspace))
    rows = page.locator(".workflow-row")
    for index, (step_title, prompt) in enumerate(steps):
        if index:
            page.locator("#add-workflow-task").click()
        row = rows.nth(index)
        row.locator(".workflow-step-title").fill(step_title)
        row.locator(".workflow-step-prompt").fill(prompt)
    page.locator("#workflow-submit").click()
    button = page.locator("#workflows [data-workflow-id]").filter(has_text=title).first
    button.wait_for(timeout=10_000)
    return button.get_attribute("data-workflow-id")


def test_workflow_click_does_not_select_a_task_or_request_undefined(dashboard):
    page: Page = dashboard["page"]
    workspace: Path = dashboard["workspace"]
    bad_requests: list[str] = []
    page.on("request", lambda request: bad_requests.append(request.url)
            if "/tasks/undefined" in request.url else None)

    page.locator("#title").fill("Keep task selection")
    page.locator("#workspace").fill(str(workspace))
    page.locator("#prompt").fill("Read-only browser test fixture")
    page.locator("#create-form button[type=submit]").click()
    task_row = page.locator("#tasks [data-id]").filter(has_text="Keep task selection")
    task_row.wait_for(timeout=10_000)
    task_row.click()
    page.locator("#detail").get_by_text("Keep task selection").wait_for()

    create_workflow(page, workspace, "Workflow click isolation", [
        ("Workflow step", "A queued fixture step"),
    ])
    page.locator("#workflows [data-workflow-id]").filter(
        has_text="Workflow click isolation"
    ).click()

    page.locator("#workflow-detail").get_by_text("Workflow click isolation").wait_for()
    page.locator("#detail").get_by_text("Keep task selection").wait_for()
    assert bad_requests == []


def test_workflow_detail_refreshes_without_reselection(dashboard):
    page: Page = dashboard["page"]
    workspace: Path = dashboard["workspace"]
    workflow_id = create_workflow(page, workspace, "Auto refresh workflow", [
        ("Refresh step", "A queued fixture step"),
    ])
    page.locator(f'#workflows [data-workflow-id="{workflow_id}"]').click()
    page.locator("#workflow-detail").get_by_text("Auto refresh workflow").wait_for()

    response = page.request.post(f'{dashboard["base_url"]}/workflows/{workflow_id}/cancel')
    assert response.status == 200

    # The dashboard polls every five seconds. No second click or manual refresh is used.
    page.locator("#workflow-detail .pill").filter(has_text="cancelled").wait_for(timeout=8_000)
    page.locator(f'#workflows [data-workflow-id="{workflow_id}"] .pill').filter(
        has_text="cancelled"
    ).wait_for(timeout=8_000)


def test_late_workflow_response_cannot_overwrite_newer_selection(dashboard):
    page: Page = dashboard["page"]
    workspace: Path = dashboard["workspace"]
    first_id = create_workflow(page, workspace, "Slow first workflow", [
        ("First step", "First queued fixture step"),
    ])
    second_id = create_workflow(page, workspace, "Fast second workflow", [
        ("Second step", "Second queued fixture step"),
    ])

    page.evaluate(
        """({firstId}) => {
          const originalFetch = window.fetch.bind(window);
          window.__delayedWorkflowFetch = false;
          window.fetch = async (input, init) => {
            const url = typeof input === "string" ? input : input.url;
            if (url.includes("/workflows/" + firstId) && !window.__delayedWorkflowFetch) {
              window.__delayedWorkflowFetch = true;
              await new Promise(resolve => setTimeout(resolve, 1500));
            }
            return originalFetch(input, init);
          };
        }""",
        {"firstId": first_id},
    )

    page.locator(f'#workflows [data-workflow-id="{first_id}"]').click()
    page.locator(f'#workflows [data-workflow-id="{second_id}"]').click()
    page.locator("#workflow-detail").get_by_text("Fast second workflow").wait_for()
    page.wait_for_function("window.__delayedWorkflowFetch === true")
    page.wait_for_timeout(1_700)

    detail = page.locator("#workflow-detail")
    assert "Fast second workflow" in detail.inner_text()
    assert "Slow first workflow" not in detail.inner_text()


def test_workflow_cancel_handles_queued_and_running_members_from_dashboard(dashboard):
    page: Page = dashboard["page"]
    workspace: Path = dashboard["workspace"]
    workflow_id = create_workflow(page, workspace, "Cancel mixed workflow", [
        ("Running member", "A running fixture step"),
        ("Queued member", "A queued fixture step"),
    ])

    # Model an already-running worker-owned member without starting Claude or a worker.
    # The API cancellation path should request cancellation for it and cancel its queued peer.
    with sqlite3.connect(dashboard["db_path"]) as connection:
        connection.execute(
            """
            UPDATE tasks
               SET status = 'running',
                   started_at = ?,
                   worker_id = 'e2e-fake-worker',
                   lease_expires_at = ?,
                   cancel_requested = 0
             WHERE workflow_id = ? AND title = ?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
                workflow_id,
                "Running member",
            ),
        )
        connection.commit()

    page.locator(f'#workflows [data-workflow-id="{workflow_id}"]').click()
    page.locator("#workflow-detail").get_by_text("Cancel mixed workflow").wait_for()
    page.locator("#cancel-workflow").click()
    page.locator("#workflow-detail .pill").filter(has_text="cancelled").wait_for(timeout=8_000)

    detail_response = page.request.get(f'{dashboard["base_url"]}/workflows/{workflow_id}')
    assert detail_response.status == 200
    detail = detail_response.json()
    assert detail["status"] == "cancelled"
    members = {task["title"]: task for task in detail["tasks"]}
    assert members["Running member"]["status"] == "running"
    assert members["Running member"]["cancel_requested"] == 1
    assert members["Queued member"]["status"] == "cancelled"


def test_worker_monitor_renders_idle_and_expired_workers(dashboard):
    page: Page = dashboard["page"]
    now = datetime.now(timezone.utc).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with sqlite3.connect(dashboard["db_path"]) as connection:
        connection.execute(
            """
            INSERT INTO workers (
                worker_id, hostname, pid, started_at, last_heartbeat_at,
                status, concurrency, active_task_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("e2e-idle-worker", "e2e-host", 4242, now, now, "idle", 2, 0),
        )
        connection.execute(
            """
            INSERT INTO workers (
                worker_id, hostname, pid, started_at, last_heartbeat_at,
                status, concurrency, active_task_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("e2e-offline-worker", "offline-host", 4343, old, old, "busy", 1, 0),
        )
        connection.commit()

    # A naive timestamp returned by SQLite/API represents UTC, not browser-local time.
    expected_local_time = page.evaluate(
        "(value) => new Date(value + 'Z').toLocaleString()",
        now.removesuffix("+00:00"),
    )
    page.locator("#refresh").click()
    page.locator("#workers .worker-card").filter(has_text="e2e-host").wait_for()
    idle_card = page.locator("#workers .worker-card").filter(has_text="e2e-idle-worker")
    offline_card = page.locator("#workers .worker-card").filter(has_text="e2e-offline-worker")
    assert "idle" in idle_card.inner_text()
    assert "offline" in offline_card.inner_text()
    assert f"Last heartbeat: {expected_local_time}" in idle_card.inner_text()


def test_workflow_timeline_shows_cross_task_failures_and_opens_task_detail(dashboard):
    page: Page = dashboard["page"]
    workspace: Path = dashboard["workspace"]
    workflow_id = create_workflow(page, workspace, "Trace workflow", [
        ("Trace prepare", "Prepare fixture"),
        ("Trace verify", "Verify fixture"),
    ])
    response = page.request.get(f'{dashboard["base_url"]}/workflows/{workflow_id}')
    assert response.status == 200
    tasks = {task["title"]: task["id"] for task in response.json()["tasks"]}
    now = datetime.now(timezone.utc)
    with sqlite3.connect(dashboard["db_path"]) as connection:
        connection.execute(
            "UPDATE tasks SET status = 'failed', finished_at = ?, error_summary = ? WHERE id = ?",
            (now.isoformat(), "verification command failed", tasks["Trace verify"]),
        )
        connection.execute(
            """
            INSERT INTO task_events (task_id, occurred_at, event_type, payload)
            VALUES (?, ?, ?, ?)
            """,
            (tasks["Trace prepare"], (now - timedelta(seconds=2)).isoformat(),
             "executor.claimed", '{"attempt": 1}'),
        )
        connection.execute(
            """
            INSERT INTO task_events (task_id, occurred_at, event_type, payload)
            VALUES (?, ?, ?, ?)
            """,
            (tasks["Trace verify"], now.isoformat(),
             "executor.finished", '{"status":"failed","error_summary":"verification command failed"}'),
        )
        connection.commit()

    page.locator(f'#workflows [data-workflow-id="{workflow_id}"]').click()
    detail = page.locator("#workflow-detail")
    detail.get_by_text("Trace workflow").wait_for()
    detail.get_by_text("Needs attention").wait_for()
    detail.get_by_text("verification command failed").wait_for()
    timeline = detail.locator(".event-list")
    timeline.get_by_text("Trace prepare").wait_for()
    timeline.get_by_text("Trace verify").wait_for()
    timeline.locator('[data-workflow-event-task="' + tasks["Trace verify"] + '"]').first.click()
    page.locator("#detail").get_by_text("Trace verify").wait_for()
    page.locator("#detail").get_by_text("verification command failed").wait_for()

