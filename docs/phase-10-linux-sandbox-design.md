# Phase 10 — Linux execution sandbox design

## Status

Phase 10.1 now includes an opt-in Bubblewrap backend. It remains disabled by default (`AI_WORK_OS_SANDBOX_MODE=off`) until the operator completes real-host acceptance. When disabled, subprocesses retain the existing host execution behavior; when enabled, normal Claude execution, Repair, and Verification are wrapped and fail closed on sandbox setup errors.

Base commit: `3a34bb9f8c752a74f1194ffbbc72470c06b82846` (Phase 9).

## Why this phase exists

The current Worker applies Claude CLI policy controls (`--permission-mode plan`, settings-source selection, and deny-all MCP configuration), but starts the CLI with the host process environment and the selected workspace as its working directory. These controls do not prevent the agent or its child processes from reading other files available to the Worker user, accessing host processes, or using inherited credentials.

The workspace change report is a before/after Git snapshot, not an access-control boundary or complete filesystem audit. Verification commands and repair executions are also host subprocesses today.

## Phase 10 security objective

Run every task-controlled process inside one explicit Linux isolation boundary:

- the Claude Code process;
- its descendants and shell/tool subprocesses;
- repair invocations;
- configured verification commands.

The Worker/API, database, task event writer, and artifact collection remain outside the sandbox. The sandbox must not be able to access the database or other task directories.

## Proposed backend

Use **bubblewrap (`bwrap`)** as the first Linux backend. It is a small host dependency and uses Linux namespaces and mount isolation; it does not require a Docker daemon or a long-running privileged service.

The backend is opt-in during rollout and must fail closed when selected but unavailable or misconfigured. Never silently fall back to unsandboxed execution.

Proposed configuration (exact names are subject to implementation review):

- `AI_WORK_OS_SANDBOX_MODE=off|bubblewrap` (initial default `off` for compatibility; production guidance should recommend `bubblewrap`).
- `AI_WORK_OS_BWRAP_BIN` (optional executable override; default `bwrap`).
- `AI_WORK_OS_SANDBOX_NETWORK=none|host` (default `none`; `host` is an explicit opt-in because network access can exfiltrate data).
- `AI_WORK_OS_SANDBOX_RUNTIME_PATHS` (narrow, explicit read-only runtime paths only if the Claude launcher or Node runtime lives outside system directories).

The exact runtime mount list must be derived from resolved executable paths and tested on the supported Linux distributions. Do not bind the entire home directory, `~/.claude`, the Worker repository, the database, or the whole storage root into the sandbox.

## Filesystem model

The first implementation should expose a small, stable filesystem view:

| Sandbox path | Access | Purpose |
| --- | --- | --- |
| `/workspace` | read-only for `plan`; read-write for configured/editing tasks | Only task workspace |
| `/awos/task` | read-only | MCP config and task-scoped input files needed by the CLI |
| `/tmp` | read-write, ephemeral | Temporary files |
| system runtime paths | read-only allowlist | Dynamic loader, system libraries, CA certificates, Claude launcher and Node runtime |
| `/home`, `/root`, unrelated host paths | hidden/empty | Prevent ambient access to user files and credentials |
| `/proc`, `/dev` | sandbox-local minimal mounts | Process/runtime support |

The task artifact directory must not be writable by the sandbox. Worker stdout/stderr capture and event persistence stay in the host Worker. If the CLI requires writable home/cache state, provide a per-task ephemeral home rather than mounting the real user home.

Do not assume a read-only bind of the host root is safe: it would still expose unrelated host files. Mount only the paths needed to run the CLI.

## Process and network model

- Use user, mount, PID, IPC, UTS, and (when network mode is `none`) network namespaces where supported.
- Do not grant host capabilities or use privileged mode.
- Ensure all descendants remain in the sandbox process namespace and that cancellation/timeout tears down the complete sandbox process tree.
- Default network mode is disabled. If network is explicitly enabled, document that the agent can reach external services and potentially exfiltrate anything it can read.
- Preserve only explicitly approved provider/auth environment variables. Never pass the complete Worker environment through by default; use an allowlist and redact values from events/logs.

## Workspace and path handling

- Mount the canonical workspace directory at `/workspace`; pass `cwd=/workspace` to Claude and verification commands.
- Reject a workspace that is itself a symlink or resolves to an unexpected location.
- Treat symlinks inside the workspace as untrusted. They must not reveal host paths outside the mounted workspace.
- Do not mount the original host workspace path into the sandbox merely to preserve its absolute pathname.
- Keep the host-side Git snapshot/reporting code outside the sandbox; it observes the selected workspace before and after execution.

## Required execution coverage

The sandbox command builder must be shared by normal execution, repair, and verification. A task is not considered sandboxed if only the initial Claude process is wrapped while repair or verification runs on the host.

The sandbox policy must be recorded in task events, including backend, effective network mode, workspace access mode, and a non-secret summary of mounted paths. Configuration failure must emit a dedicated sandbox failure event and must not start any task-controlled process.

## Security tests required before enabling by default

1. A fake CLI can read and write only the expected workspace paths for an editing task.
2. A plan-mode task cannot write to the workspace at the OS level, even if a fake CLI ignores Claude permission mode.
3. Reads of a canary file outside the workspace (including another home directory file and a sibling project) fail.
4. Writes outside the workspace and task temp area fail.
5. Workspace symlinks pointing outside the workspace cannot read or mutate their targets.
6. Child processes inherit the same namespace and cannot escape through process spawning.
7. Network is unavailable in `none` mode and available only when explicitly configured.
8. Normal execution, repair, and verification all use the same sandbox policy.
9. Missing `bwrap`, unsupported user namespaces, invalid paths, and mount failures fail closed with no unsandboxed fallback.
10. Cancellation and timeout terminate the entire sandbox process tree.
11. The host Worker can still persist events, capture logs, and report workspace changes.
12. The real Claude Code CLI version used by the operator starts successfully without mounting the real user settings or credential directory.

## Rollout plan

### Phase 10.1 — sandbox substrate

Implemented in this PR:
- Add a backend abstraction and a pure command/mount-plan builder.
- Use Bubblewrap user, mount, PID, IPC, UTS, and optional network namespaces; default network mode is `none`.
- Mount the workspace at `/workspace` (read-only for plan tasks, read-write for configured tasks), mount only the generated MCP file read-only, and keep the task artifact directory, Worker, database, and real home outside the sandbox.
- Mount system runtime directories read-only; resolve and mount the Claude CLI package and Node executable individually when they live outside system paths.
- Use an ephemeral `/tmp/home` and an explicit provider/auth environment allowlist. The allowlisted environment is passed via the child environment, not command-line arguments.
- Preserve absolute workspace virtualenv shebangs with a sandbox-local symlink alias to `/workspace`; this does not mount the host parent directory.
- Record `executor.sandbox_configured` / `executor.sandbox_failed` metadata for normal, repair, and verification subprocesses.

Remaining local acceptance work:
- Validate the actual Claude Code 2.1.306 launcher, Node runtime, provider authentication, and host-network DNS/certificate behavior on the operator's Fedora host.
- Exercise read-only/read-write workspace boundaries, host home and sibling-project canaries, symlink escapes, child process inheritance, and cancellation cleanup.
- External verification executables outside workspace/system paths require explicit `AI_WORK_OS_SANDBOX_RUNTIME_PATHS` entries.
- The Worker performs a one-time per-process Bubblewrap preflight for user/PID/IPC/UTS/network namespaces and a minimal system mount before starting task-controlled commands.

### Phase 10.2 — hardening

- Minimize runtime mounts and environment allowlist based on real CLI acceptance.
- Add resource limits (CPU, memory, process count, file size, open files) where they can be enforced reliably.
- Verify cleanup of namespaces and descendants under cancellation, worker shutdown, and timeout.
- Document distro-specific setup and a safe migration path.

### Phase 10.3 — default policy decision

Only consider making sandbox mode the default after real-host acceptance proves that common Claude Code operations work, all adversarial boundary tests pass, and the user explicitly accepts the network and credential model.

## Current known limitations

- Linux only for the first backend; other operating systems remain unsandboxed and must be reported as such.
- Bubblewrap is not a complete defense against kernel vulnerabilities or a compromised host.
- If network access is enabled, the sandbox can still exfiltrate data it is allowed to read.
- Writable workspace mode intentionally permits arbitrary modifications and deletion inside the selected workspace.
- Project/local Claude settings remain available inside the workspace unless a later policy explicitly disables them.
- A sandbox cannot make untrusted verification commands safe unless they run inside the same boundary.
