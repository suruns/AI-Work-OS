from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Browser, sync_playwright


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
                page.goto(base_url + "/dashboard", wait_until="domcontentloaded")
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

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call":
        item.rep_call = report
