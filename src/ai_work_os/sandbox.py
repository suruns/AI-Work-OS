from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


class SandboxError(RuntimeError):
    """A selected sandbox could not be prepared safely."""


@dataclass(frozen=True)
class SandboxInvocation:
    command: list[str]
    env: dict[str, str]
    cwd: str
    metadata: dict


_AUTH_ENV_KEYS = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_BEDROCK_AWS_REGION",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "ANTHROPIC_VERTEX_REGION",
    "CLAUDE_CODE_OAUTH_TOKEN",
}
_BASIC_ENV_KEYS = {"LANG", "LC_ALL", "TZ"}
_SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib64")
_SYSTEM_FILES = (
    "/etc/passwd",
    "/etc/group",
    "/etc/nsswitch.conf",
    "/etc/os-release",
)
_SYSTEM_DIR_MOUNTS = (
    "/etc/ssl/certs",
    "/etc/pki",
)


def sandbox_mode() -> str:
    mode = os.getenv("AI_WORK_OS_SANDBOX_MODE", "off").strip().lower()
    if mode not in {"off", "bubblewrap"}:
        raise SandboxError("AI_WORK_OS_SANDBOX_MODE must be 'off' or 'bubblewrap'")
    return mode


def _network_mode() -> str:
    mode = os.getenv("AI_WORK_OS_SANDBOX_NETWORK", "none").strip().lower()
    if mode not in {"none", "host"}:
        raise SandboxError("AI_WORK_OS_SANDBOX_NETWORK must be 'none' or 'host'")
    return mode


@functools.lru_cache(maxsize=8)
def _probe_bwrap(binary: str) -> None:
    """Verify user/PID/IPC/UTS/network namespaces and basic mounts before task launch."""
    command = [
        binary,
        "--die-with-parent",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-net",
        "--dir", "/usr",
        "--ro-bind", "/usr", "/usr",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--", "/usr/bin/true",
    ]
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError(f"bubblewrap capability probe failed: {type(exc).__name__}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        raise SandboxError(
            f"bubblewrap namespace/mount probe failed with exit code {result.returncode}"
            + (f": {detail}" if detail else "")
        )


def _resolve_executable(command: Sequence[str], workspace: Path, env: Mapping[str, str]) -> Path:
    if not command:
        raise SandboxError("cannot sandbox an empty command")
    raw = command[0]
    if "/" in raw:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = workspace / candidate
    else:
        located = shutil.which(raw, path=env.get("PATH"))
        if located is None:
            raise SandboxError(f"executable is not available to the worker: {raw}")
        candidate = Path(located)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SandboxError(f"cannot resolve executable {raw!r}: {exc}") from exc
    if not resolved.is_file():
        raise SandboxError(f"command is not a regular executable file: {resolved}")
    return resolved


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _extra_runtime_paths() -> list[Path]:
    raw = os.getenv("AI_WORK_OS_SANDBOX_RUNTIME_PATHS", "")
    paths: list[Path] = []
    for item in filter(None, (part.strip() for part in raw.split(os.pathsep))):
        candidate = Path(item)
        if not candidate.is_absolute():
            raise SandboxError("AI_WORK_OS_SANDBOX_RUNTIME_PATHS entries must be absolute paths")
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SandboxError(f"runtime path does not exist: {item}: {exc}") from exc
        if resolved == Path("/") or not (resolved.is_dir() or resolved.is_file()):
            raise SandboxError(f"runtime path must be a file or directory below /: {item}")
        paths.append(resolved)
    return paths


def _runtime_mounts(
    executable: Path, workspace: Path, inherited_env: Mapping[str, str]
) -> tuple[list[Path], list[str]]:
    mounts: list[Path] = []
    path_entries: list[str] = []

    def add_path(path: Path) -> None:
        resolved = path.resolve(strict=True)
        if any(_is_within(resolved, Path(root)) for root in _SYSTEM_DIRS):
            return
        if _is_within(resolved, workspace):
            return
        directory = resolved if resolved.is_dir() else resolved.parent
        if directory not in mounts:
            mounts.append(directory)

    add_path(executable)
    try:
        first_line = executable.open("r", encoding="utf-8", errors="replace").readline(512).strip()
    except (OSError, UnicodeError):
        first_line = ""
    needs_node = "node" in first_line.lower() or executable.suffix.lower() in {".js", ".mjs", ".cjs"}
    if needs_node:
        node = shutil.which("node", path=inherited_env.get("PATH"))
        if node is None:
            raise SandboxError("Claude Code launcher requires Node.js, but node was not found in the worker PATH")
        try:
            node_path = Path(node).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SandboxError(f"cannot resolve Node.js runtime: {exc}") from exc
        add_path(node_path)
        node_dir = node_path.parent
        if str(node_dir) not in path_entries:
            path_entries.append(str(node_dir))

    for path in _extra_runtime_paths():
        add_path(path)
        if path.is_dir() and str(path) not in path_entries:
            path_entries.append(str(path))
    return mounts, path_entries


def _ensure_target_dir(args: list[str], target: str, created: set[str]) -> None:
    path = Path(target)
    missing: list[Path] = []
    current = path
    while str(current) not in {".", "/"} and str(current) not in created:
        missing.append(current)
        current = current.parent
    for item in reversed(missing):
        args.extend(["--dir", str(item)])
        created.add(str(item))


def _curated_environment(inherited_env: Mapping[str, str], runtime_path_entries: list[str]) -> dict[str, str]:
    allowlist = set(_AUTH_ENV_KEYS)
    raw_extra = os.getenv("AI_WORK_OS_SANDBOX_ENV_ALLOWLIST", "")
    for key in filter(None, (part.strip() for part in raw_extra.split(","))):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key):
            raise SandboxError(f"invalid environment variable name in AI_WORK_OS_SANDBOX_ENV_ALLOWLIST: {key!r}")
        allowlist.add(key)

    result = {key: inherited_env[key] for key in allowlist if key in inherited_env}
    result.update({key: inherited_env[key] for key in _BASIC_ENV_KEYS if key in inherited_env})
    system_path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    result["PATH"] = ":".join([*runtime_path_entries, system_path])
    result["HOME"] = "/tmp/awos-home"
    result["TMPDIR"] = "/tmp"
    return result


def build_sandbox_invocation(
    command: Sequence[str],
    workspace: Path,
    task_dir: Path,
    permission_mode: str,
    inherited_env: Mapping[str, str] | None = None,
) -> SandboxInvocation | None:
    """Build a fail-closed bubblewrap invocation; return None when sandbox mode is off."""
    mode = sandbox_mode()
    if mode == "off":
        return None

    network = _network_mode()
    inherited = dict(os.environ if inherited_env is None else inherited_env)
    bwrap_name = os.getenv("AI_WORK_OS_BWRAP_BIN", "bwrap").strip() or "bwrap"
    bwrap = shutil.which(bwrap_name, path=inherited.get("PATH"))
    if bwrap is None:
        raise SandboxError(f"bubblewrap executable not found: {bwrap_name}")
    bwrap = str(Path(bwrap).resolve(strict=True))
    _probe_bwrap(bwrap)

    workspace_input = Path(workspace)
    if workspace_input.is_symlink():
        raise SandboxError("workspace root must not be a symlink")
    try:
        workspace_root = workspace_input.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SandboxError(f"workspace cannot be resolved: {exc}") from exc
    if not workspace_root.is_dir():
        raise SandboxError("workspace must be an existing directory")

    task_root = Path(task_dir).resolve(strict=True)
    mcp_config = task_root / "mcp-config.isolated.json"
    if mcp_config.is_symlink() or not mcp_config.is_file():
        raise SandboxError("isolated MCP config is missing or is not a regular file")

    executable = _resolve_executable(command, workspace_root, inherited)
    runtime_mounts, runtime_path_entries = _runtime_mounts(executable, workspace_root, inherited)
    curated_env = _curated_environment(inherited, runtime_path_entries)

    args = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
    ]
    if network == "none":
        args.append("--unshare-net")

    created: set[str] = {"/"}
    for source in _SYSTEM_DIRS:
        if Path(source).exists():
            _ensure_target_dir(args, source, created)
            args.extend(["--ro-bind", source, source])
    for source in _SYSTEM_FILES:
        if Path(source).is_file():
            _ensure_target_dir(args, source, created)
            args.extend(["--ro-bind", source, source])
    for source in _SYSTEM_DIR_MOUNTS:
        if Path(source).is_dir():
            _ensure_target_dir(args, source, created)
            args.extend(["--ro-bind", source, source])
    if network == "host" and Path("/etc/resolv.conf").is_file():
        _ensure_target_dir(args, "/etc/resolv.conf", created)
        args.extend(["--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf"])

    _ensure_target_dir(args, "/proc", created)
    _ensure_target_dir(args, "/dev", created)
    args.extend(["--proc", "/proc", "--dev", "/dev"])
    args.extend(["--tmpfs", "/tmp", "--tmpfs", "/home", "--tmpfs", "/root"])
    args.extend(["--dir", "/tmp/awos-home"])
    created.update({"/tmp", "/home", "/root", "/tmp/awos-home"})

    for source in runtime_mounts:
        target = str(source)
        _ensure_target_dir(args, target, created)
        args.extend(["--ro-bind", target, target])

    _ensure_target_dir(args, "/workspace", created)
    if permission_mode == "plan":
        args.extend(["--ro-bind", str(workspace_root), "/workspace"])
        workspace_access = "read_only"
    else:
        args.extend(["--bind", str(workspace_root), "/workspace"])
        workspace_access = "read_write"

    _ensure_target_dir(args, "/awos/task", created)
    # The task directory is task-scoped and mounted read-only. This avoids exposing
    # the storage root or other tasks while ensuring the MCP config bind target exists.
    args.extend(["--ro-bind", str(task_root), "/awos/task"])
    sandbox_executable = (
        "/workspace/" + executable.relative_to(workspace_root).as_posix()
        if _is_within(executable, workspace_root)
        else str(executable)
    )
    args.extend(["--chdir", "/workspace", "--"])
    args.extend([sandbox_executable, *list(command[1:])])

    metadata = {
        "backend": "bubblewrap",
        "network": network,
        "workspace_access": workspace_access,
        "workspace_mount": "/workspace",
        "task_input_mount": "/awos/task (read-only, task-scoped)",
        "runtime_mounts": [str(path) for path in runtime_mounts],
        "system_mounts": [path for path in _SYSTEM_DIRS if Path(path).exists()],
        "auth_env_keys": sorted(key for key in curated_env if key in _AUTH_ENV_KEYS),
        "environment_policy": "explicit_auth_allowlist",
    }
    return SandboxInvocation(args, curated_env, "/workspace", metadata)
