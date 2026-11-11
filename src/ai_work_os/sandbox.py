from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

AUTH_ENV_KEYS = {
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION", "AWS_REGION",
    "AWS_DEFAULT_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
}
SAFE_ENV_KEYS = {"LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM"}
SYSTEM_ROOTS = ("/usr", "/bin", "/sbin", "/lib", "/lib64")


@dataclass(frozen=True)
class SandboxInvocation:
    command: list[str]
    cwd: str
    env: dict[str, str] | None
    metadata: dict


def sandbox_mode() -> str:
    mode = os.getenv("AI_WORK_OS_SANDBOX_MODE", "off").strip().lower()
    if mode not in {"off", "bubblewrap"}:
        raise ValueError("AI_WORK_OS_SANDBOX_MODE must be 'off' or 'bubblewrap'")
    return mode


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _target_dirs(paths: set[str]) -> list[str]:
    result: set[str] = set()
    for value in paths:
        current = Path(value)
        while str(current) not in {".", "/"}:
            result.add(str(current))
            current = current.parent
    return sorted(result, key=lambda item: (item.count("/"), item))


def _system_mounts() -> tuple[list[list[str]], set[str]]:
    args: list[list[str]] = []
    dirs = {"/etc", "/proc", "/dev", "/tmp", "/workspace"}
    for source in SYSTEM_ROOTS:
        path = Path(source)
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink():
            args.append(["--symlink", os.readlink(path), source])
        else:
            dirs.add(source)
            args.append(["--ro-bind", source, source])
    for source in ("/etc/ssl", "/etc/pki"):
        if Path(source).exists():
            dirs.add(source)
            args.append(["--ro-bind", source, source])
    return args, dirs


def _resolve_executable(command: Sequence[str], env: dict[str, str] | None) -> Path:
    if not command:
        raise ValueError("sandbox command is empty")
    found = shutil.which(command[0], path=(env or {}).get("PATH"))
    if found is None:
        raise FileNotFoundError(f"execution executable not found: {command[0]}")
    resolved = Path(found).resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"execution executable is not a regular file: {resolved}")
    return resolved


def _sandbox_path(path: Path, workspace: Path) -> str:
    if _inside(path, workspace):
        return str(Path("/workspace") / path.relative_to(workspace))
    return str(path)


def _runtime_mounts(
    executable: Path, workspace: Path, env: dict[str, str] | None, include_node: bool,
) -> tuple[list[list[str]], set[str], list[str], str, str]:
    mounts: list[list[str]] = []
    dirs: set[str] = set()
    visible: list[str] = []
    command_path = _sandbox_path(executable, workspace)
    if not _inside(executable, workspace) and not any(
        _inside(executable, Path(root)) for root in SYSTEM_ROOTS
    ):
        if include_node:
            package_dir = executable.parent
            mounts.append(["--ro-bind", str(package_dir), str(package_dir)])
            dirs.add(str(package_dir))
            visible.append(str(package_dir))
        else:
            configured = [
                Path(item).expanduser().resolve(strict=True)
                for item in os.getenv("AI_WORK_OS_SANDBOX_RUNTIME_PATHS", "").split(os.pathsep)
                if item.strip()
            ]
            match = next((item for item in configured if executable == item or _inside(executable, item)), None)
            if match is None:
                raise ValueError(
                    f"executable outside workspace/system paths requires "
                    f"AI_WORK_OS_SANDBOX_RUNTIME_PATHS: {executable}"
                )
            source = match if match.is_dir() else executable
            mounts.append(["--ro-bind", str(source), str(source)])
            dirs.add(str(source) if source.is_dir() else str(Path(source).parent))
            visible.append(str(source))
    node_dir = ""
    if include_node:
        node_found = shutil.which("node", path=(env or {}).get("PATH", os.defpath))
        if node_found is None:
            raise FileNotFoundError("Node.js runtime not found in Claude execution PATH")
        node_path = Path(node_found).resolve(strict=True)
        if not node_path.is_file():
            raise ValueError(f"Node.js runtime is not a regular file: {node_path}")
        node_dir = _sandbox_path(node_path.parent, workspace)
        if not _inside(node_path, workspace) and not any(
            _inside(node_path, Path(root)) for root in SYSTEM_ROOTS
        ):
            mounts.append(["--ro-bind", str(node_path), str(node_path)])
            dirs.add(str(node_path.parent))
            visible.append(str(node_path))
    return mounts, dirs, visible, command_path, node_dir


def build_sandbox_invocation(
    command: Sequence[str], workspace: Path, task_dir: Path, permission_mode: str,
    env: dict[str, str] | None, *, mcp_config_path: Path | None = None,
    execution_kind: str = "task",
) -> SandboxInvocation:
    """Build a fail-closed Bubblewrap invocation; never falls back to host execution."""
    if sandbox_mode() == "off":
        return SandboxInvocation(list(command), str(workspace), env, {
            "enabled": False, "backend": "none", "network": "host",
            "workspace_access": "read_only" if permission_mode == "plan" else "read_write",
            "runtime_mounts": [], "execution_kind": execution_kind,
        })
    bwrap_setting = os.getenv("AI_WORK_OS_BWRAP_BIN", "bwrap")
    bwrap = shutil.which(bwrap_setting)
    if bwrap is None:
        raise FileNotFoundError(f"Bubblewrap executable not found: {bwrap_setting}")
    bwrap_path = str(Path(bwrap).resolve(strict=True))
    workspace_input = Path(workspace).expanduser().absolute()
    if workspace_input.is_symlink() or workspace_input != workspace_input.resolve(strict=True):
        raise ValueError("sandbox workspace must be a canonical, non-symlink path")
    workspace_path = workspace_input.resolve(strict=True)
    if not workspace_path.is_dir():
        raise ValueError("sandbox workspace must be a directory")
    if workspace_path in {Path("/"), Path("/home"), Path("/etc"), Path("/usr"), Path("/tmp"), Path("/proc"), Path("/dev")}:
        raise ValueError("sandbox workspace cannot be a filesystem or reserved mount root")
    task_path = Path(task_dir).resolve(strict=True)
    if not task_path.is_dir():
        raise ValueError("sandbox task artifact directory must exist")
    network = os.getenv("AI_WORK_OS_SANDBOX_NETWORK", "none").strip().lower()
    if network not in {"none", "host"}:
        raise ValueError("AI_WORK_OS_SANDBOX_NETWORK must be 'none' or 'host'")
    executable = _resolve_executable(command, env)
    include_node = execution_kind in {"task", "repair"}
    runtime_mounts, runtime_dirs, visible_mounts, command_path, node_dir = _runtime_mounts(
        executable, workspace_path, env, include_node
    )
    system_mounts, dirs = _system_mounts()
    dirs.update(runtime_dirs)
    dirs.update({"/tmp/home", "/tmp/home/.cache", "/tmp/home/.config",
                 "/tmp/home/.local", "/tmp/home/.local/share", "/tmp/home/.claude"})
    config_target = None
    config_source = None
    if mcp_config_path is not None:
        original_config = Path(mcp_config_path)
        if original_config.is_symlink():
            raise ValueError("MCP config symlinks are not allowed")
        config_source = original_config.resolve(strict=True)
        if not _inside(config_source, task_path) or not config_source.is_file():
            raise ValueError("MCP config must be a regular file inside the task artifact directory")
        config_target = "/awos/task/mcp-config.isolated.json"
        dirs.update({"/awos", "/awos/task"})
    workspace_alias = not any(
        _inside(workspace_path, Path(root)) for root in SYSTEM_ROOTS
    )
    if workspace_alias and not _inside(workspace_path, Path("/tmp")):
        dirs.add(str(workspace_path.parent))
    args = [
        bwrap_path, "--die-with-parent", "--new-session",
        "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
    ]
    if network == "none":
        args.append("--unshare-net")
    args.append("--hostname")
    args.append("ai-work-os")
    for directory in _target_dirs(dirs):
        args.extend(["--dir", directory])
    if network == "host":
        for source in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf"):
            path = Path(source)
            if path.exists():
                args.extend(["--ro-bind", str(path.resolve(strict=True)), source])
    for mount in system_mounts:
        args.extend(mount)
    for mount in runtime_mounts:
        args.extend(mount)
    args.extend(["--ro-bind" if permission_mode == "plan" else "--bind",
                 str(workspace_path), "/workspace"])
    if config_target and config_source:
        args.extend(["--ro-bind", str(config_source), config_target])
    args.extend(["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"])
    args.extend([
        "--dir", "/tmp/home", "--dir", "/tmp/home/.cache",
        "--dir", "/tmp/home/.config", "--dir", "/tmp/home/.local",
        "--dir", "/tmp/home/.local/share", "--dir", "/tmp/home/.claude",
    ])
    if workspace_alias:
        if _inside(workspace_path, Path("/tmp")):
            for directory in _target_dirs({str(workspace_path.parent)}):
                if directory != "/tmp":
                    args.extend(["--dir", directory])
        args.extend(["--symlink", "/workspace", str(workspace_path)])
    path_parts = ["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    if node_dir:
        path_parts.insert(0, node_dir)
    sandbox_env = {
        "PATH": os.pathsep.join(dict.fromkeys(path_parts)),
        "HOME": "/tmp/home", "TMPDIR": "/tmp",
        "XDG_CACHE_HOME": "/tmp/home/.cache",
        "XDG_CONFIG_HOME": "/tmp/home/.config",
        "XDG_DATA_HOME": "/tmp/home/.local/share",
    }
    for key in SAFE_ENV_KEYS:
        if (env or {}).get(key):
            sandbox_env[key] = (env or {})[key]
    if execution_kind in {"task", "repair"}:
        for key in AUTH_ENV_KEYS:
            if (env or {}).get(key):
                sandbox_env[key] = (env or {})[key]
    # Pass a sanitized environment to bubblewrap via execve's envp, not argv:
    # secrets therefore do not appear in /proc/<pid>/cmdline.
    args.extend(["--chdir", "/workspace", "--", command_path, *list(command)[1:]])
    return SandboxInvocation(args, "/", sandbox_env, {
        "enabled": True, "backend": "bubblewrap", "bwrap_path": bwrap_path,
        "network": network,
        "workspace_access": "read_only" if permission_mode == "plan" else "read_write",
        "workspace_mount": "/workspace", "workspace_path_alias": bool(workspace_alias),
        "runtime_mounts": sorted(set(visible_mounts)),
        "task_config_mount": config_target, "execution_kind": execution_kind,
    })
