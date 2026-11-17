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
