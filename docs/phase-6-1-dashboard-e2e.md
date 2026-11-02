# Phase 6.1: Dashboard browser E2E tests

## What is covered

The E2E suite starts a fresh FastAPI process for each test with its own temporary SQLite database and storage directory. It launches headless Chromium and drives the actual Dashboard served by the API.

- Selecting a Workflow does not select a Task, issue /tasks/undefined, or clear the current Task detail.
- An open Workflow detail and its list row reflect server-side state changes through the existing five-second poll, without another click.
- A deliberately delayed response for an older Workflow cannot overwrite the detail for a newer selection.
- The Dashboard cancellation action cancels queued members and requests cancellation for a running member.

The tests do not start the worker, invoke Claude Code, require a Claude account, or call external APIs. The running member in the cancellation test is a database fixture representing an already-running worker-owned task.

## Local setup

From the repository root on Linux:

    python -m pip install -e ".[dev,e2e]"
    python -m playwright install --with-deps chromium

If browser system libraries are already installed, python -m playwright install chromium is sufficient.

## Run tests

    # Existing API and unit tests
    pytest -q -m "not e2e"

    # Dashboard browser tests
    pytest -q tests/e2e -m e2e

    # Repeat the browser suite three times to check for timing flakes
    for attempt in 1 2 3; do
      echo "Dashboard E2E stability run $attempt/3"
      pytest -q tests/e2e -m e2e || exit 1
    done

The E2E fixture allocates an available localhost port and tears down its API process and browser after each test. To keep failure evidence in a chosen directory:

    AI_WORK_OS_E2E_ARTIFACTS=artifacts/e2e pytest -q tests/e2e -m e2e

On a test failure, the fixture writes a full-page PNG screenshot and a Playwright trace ZIP. GitHub Actions uploads these files as the dashboard-e2e-failures artifact when a job fails.

## Isolation notes

- Each test gets a new temporary SQLite database and storage directory.
- The API runs as a separate Uvicorn process, so database environment variables are applied before application import.
- Tests use only the API and browser; they do not launch the task worker or a real agent process.
- The test that covers a running member sets its state directly in its own temporary database, then exercises cancellation through the real Dashboard and API.
