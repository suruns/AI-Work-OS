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
    workflow_copy = page.locator("#workflow-detail").inner_text()
    assert "工作流步骤" in workflow_copy
    assert "查看任务" in workflow_copy
    assert "排队中" in workflow_copy
    assert "任务已排队" in workflow_copy
    assert "task.queued" not in workflow_copy
    assert "Workflow steps" not in workflow_copy
    assert "queued" not in workflow_copy
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
    page.locator("#workflow-detail .pill").filter(has_text="已取消").wait_for(timeout=8_000)
    page.locator(f'#workflows [data-workflow-id="{workflow_id}"] .pill').filter(
        has_text="已取消"
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
    page.locator("#workflow-detail .pill").filter(has_text="已取消").wait_for(timeout=8_000)

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
    assert "空闲" in idle_card.inner_text()
    assert "离线" in offline_card.inner_text()
    assert f"最近心跳：{expected_local_time}" in idle_card.inner_text()


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
    detail.get_by_text("需要关注").wait_for()
    detail.get_by_text("Trace verify: verification command failed", exact=True).wait_for()
    timeline = detail.locator(".event-list")
    timeline.locator('[data-workflow-event-task="' + tasks["Trace prepare"] + '"]').first.wait_for()
    timeline.locator('[data-workflow-event-task="' + tasks["Trace verify"] + '"]').first.wait_for()
    timeline.locator('[data-workflow-event-task="' + tasks["Trace verify"] + '"]').first.click()
    page.locator("#detail").get_by_text("Trace verify").wait_for()
    page.locator("#detail .event").filter(has_text="verification command failed").first.wait_for()


def test_dashboard_requires_explicit_approval_for_configured_task(dashboard):
    page: Page = dashboard["page"]
    base_url = dashboard["base_url"]
    workspace = dashboard["workspace"]
    project_response = page.request.post(base_url + "/projects", data={
        "name": "审批界面项目", "slug": "approval-dashboard-project",
        "workspace_path": str(workspace),
    })
    assert project_response.status == 201
    project = project_response.json()

    def create_pending(title):
        task_response = page.request.post(base_url + "/tasks", data={
            "project_id": project["id"], "title": title,
            "prompt": "在项目工作区中创建一个小型变更",
            "workspace_path": str(workspace), "permission_mode": "configured",
        })
        assert task_response.status == 201
        task = task_response.json()
        approval_response = page.request.post(base_url + f"/execution-approvals/request/{task['id']}")
        assert approval_response.status == 200
        return task, approval_response.json()

    approved_task, approved = create_pending("批准测试任务")
    rejected_task, rejected = create_pending("拒绝测试任务")
    revoked_task, revoked = create_pending("撤销测试任务")
    page.reload(wait_until="domcontentloaded")
    page.locator("#connection").filter(has_text="API 已连接").wait_for(timeout=10_000)

    approved_card = page.locator("#approval-list .approval-card").filter(has_text="批准测试任务")
    approval_copy = approved_card.inner_text()
    assert "执行隔离：" in approval_copy
    assert "网络策略：" in approval_copy
    assert "权限配置来源：" in approval_copy
    assert "最大自动修复次数：" in approval_copy
    assert "审批前请检查 Claude 设置" in approval_copy
    assert "hooks 等命令配置" in approval_copy
    assert "CLAUDE.md" in approval_copy
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_response(lambda response: response.url.endswith(f"/execution-approvals/{approved['id']}/approve")) as approve_response:
        approved_card.get_by_role("button", name="批准并允许执行").click()
    assert approve_response.value.status == 200, approve_response.value.text()
    approved_status = page.request.get(base_url + f"/execution-approvals/{approved['id']}")
    assert approved_status.status == 200
    assert approved_status.json()["status"] == "approved"

    rejected_card = page.locator("#approval-list .approval-card").filter(has_text="拒绝测试任务")
    with page.expect_response(lambda response: response.url.endswith(f"/execution-approvals/{rejected['id']}/reject")) as reject_response:
        rejected_card.get_by_role("button", name="拒绝").click()
    assert reject_response.value.status == 200, reject_response.value.text()
    rejected_status = page.request.get(base_url + f"/execution-approvals/{rejected['id']}")
    assert rejected_status.status == 200
    assert rejected_status.json()["status"] == "rejected"
    with page.expect_response(lambda response: response.url.endswith(f"/execution-approvals/request/{rejected_task['id']}")) as resubmit_response:
        rejected_card.get_by_role("button", name="重新申请审批").click()
    assert resubmit_response.value.status == 200, resubmit_response.value.text()
    assert resubmit_response.value.json()["status"] == "pending"

    revoke_pending_card = page.locator("#approval-list .approval-card").filter(has_text="撤销测试任务")
    page.once("dialog", lambda dialog: dialog.accept())
    revoke_pending_card.get_by_role("button", name="批准并允许执行").click()
    revoke_approved_card = page.locator("#approval-list .approval-card").filter(has_text="撤销测试任务")
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_response(lambda response: response.url.endswith(f"/execution-approvals/{revoked['id']}/revoke")) as revoke_response:
        revoke_approved_card.get_by_role("button", name="撤销审批").click()
    assert revoke_response.value.status == 200, revoke_response.value.text()
    revoked_status = page.request.get(base_url + f"/execution-approvals/{revoked['id']}")
    assert revoked_status.status == 200
    assert revoked_status.json()["status"] == "revoked"

    assert page.request.get(base_url + f"/tasks/{approved_task['id']}").json()["status"] == "queued"
    assert page.request.get(base_url + f"/tasks/{rejected_task['id']}").json()["status"] == "queued"
    assert page.request.get(base_url + f"/tasks/{revoked_task['id']}").json()["status"] == "queued"
