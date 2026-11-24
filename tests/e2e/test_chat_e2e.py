from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.e2e


def test_chat_first_project_conversation_persists_and_cancels_queued_turn(dashboard):
    page: Page = dashboard["page"]
    workspace: Path = dashboard["workspace"]
    page.goto(dashboard["base_url"], wait_until="domcontentloaded")
    page.locator("#connection").filter(has_text="API 已连接").wait_for(timeout=10_000)

    project_response = page.request.post(
        dashboard["base_url"] + "/projects",
        data={
            "name": "Chat UI project",
            "slug": "chat-ui-project",
            "description": "Chat-first browser acceptance fixture",
            "workspace_path": str(workspace),
        },
    )
    assert project_response.status == 201
    project = project_response.json()

    page.locator("#project-context").select_option(project["id"])
    page.locator("#new-conversation").click()
    page.locator("#conversation-title").filter(has_text="新对话").wait_for()
    page.locator("#message-input").fill(
        "Please inspect this project and explain the first safe next step."
    )
    page.locator("#send").click()

    user_message = page.locator(".message.user").filter(
        has_text="Please inspect this project"
    )
    user_message.wait_for(timeout=10_000)
    user_message.locator(".turn-chip").filter(has_text="排队中").wait_for(timeout=10_000)

    conversation_id = page.evaluate("activeConversationId")
    assert conversation_id
    turn_response = page.request.get(
        dashboard["base_url"] + "/conversations/" + conversation_id + "/turns"
    )
    assert turn_response.status == 200
    turns = turn_response.json()
    assert len(turns) == 1
    turn_id = turns[0]["id"]
    assert turns[0]["status"] == "queued"

    page.locator("#cancel-turn").click()
    page.wait_for_function(
        """async (turnId) => {
          const response = await fetch('/agent-turns/' + turnId);
          const turn = await response.json();
          return turn.status === 'cancelled';
        }""",
        arg=turn_id,
        timeout=10_000,
    )
    page.reload(wait_until="domcontentloaded")
    page.locator("#connection").filter(has_text="API 已连接").wait_for(timeout=10_000)
    page.locator(".message.user").filter(
        has_text="Please inspect this project"
    ).wait_for()
    assert page.locator("#project-context").input_value() == project["id"]
    assert page.locator(".turn-chip").filter(has_text="已取消").count() == 1
    assert page.locator("#event-list").get_by_text("本轮已取消").count() == 1


def test_chat_home_and_dashboard_routes_are_separate(dashboard):
    page: Page = dashboard["page"]
    page.goto(dashboard["base_url"], wait_until="domcontentloaded")
    page.locator("#connection").filter(has_text="API 已连接").wait_for(timeout=10_000)
    assert page.locator("#message-input").count() == 1
    page.goto(dashboard["base_url"] + "/dashboard", wait_until="domcontentloaded")
    page.locator("#create-form").wait_for()
    assert page.locator("#message-input").count() == 0



def test_project_management_create_archive_restore_and_scoped_chat(dashboard):
    page: Page = dashboard["page"]
    workspace: Path = dashboard["workspace"]
    base_url = dashboard["base_url"]

    page.goto(base_url + "/project-management", wait_until="domcontentloaded")
    page.locator("#create-btn").wait_for()

    page.locator("#create-btn").click()
    page.locator("#name").fill("项目管理验收项目")
    page.locator("#workspace").fill(str(workspace))
    page.locator("#description").fill("验证项目管理页面的完整交互")
    page.locator("#create-form button[type=submit]").click()

    project_button = page.locator(".project").filter(has_text="项目管理验收项目")
    project_button.wait_for(timeout=10_000)
    project_id = page.evaluate("selected")
    assert project_id
    assert page.get_by_role("heading", name="项目管理验收项目").count() == 1

    page.get_by_role("button", name="Master Agent 目标").click()
    page.locator("#tab-content").wait_for()
    page.get_by_role("button", name="归档项目").click()
    page.get_by_role("button", name="恢复项目").wait_for(timeout=10_000)

    project = page.request.get(base_url + "/projects/" + project_id).json()
    assert project["status"] == "archived"
    assert page.get_by_text("在此项目中与 Master Agent 对话").count() == 0

    page.get_by_role("button", name="恢复项目").click()
    scoped_chat = page.get_by_role("link", name="在此项目中与 Master Agent 对话")
    scoped_chat.wait_for(timeout=10_000)
    scoped_chat.click()

    page.locator("#connection").filter(has_text="API 已连接").wait_for(timeout=10_000)
    page.wait_for_function("new URL(location.href).searchParams.has('project_id') === false")
    conversation_id = page.evaluate("activeConversationId")
    assert conversation_id
    conversation = page.request.get(
        base_url + "/conversations/" + conversation_id
    ).json()
    assert conversation["project_id"] == project_id
    assert page.locator("#project-context").input_value() == project_id
