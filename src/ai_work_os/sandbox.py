from __future__ import annotations

import functools
import os
import re
import shlex
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
    "/etc/ld.so.cache",
)
_SYSTEM_DIR_MOUNTS = (
    "/etc/pki/ca-trust",
    "/etc/pki/tls/certs",
    "/etc/ssl/certs",
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
    candidate = Path(os.path.abspath(candidate))
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SandboxError(f"cannot resolve executable {raw!r}: {exc}") from exc
    if not resolved.is_file():
        raise SandboxError(f"command is not a regular executable file: {resolved}")
    return candidate


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
        forbidden = {Path("/"), Path("/home"), Path("/root"), Path("/etc"), Path("/var"), Path("/tmp")}
        if resolved in forbidden or not resolved.is_dir():
            raise SandboxError(f"runtime path must be a narrow existing directory below /: {item}")
        paths.append(resolved)
    return paths


def _read_shebang(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            first_line = stream.readline(512).strip()
    except OSError:
        return []
    if not first_line.startswith("#!"):
        return []
    try:
        return shlex.split(first_line[2:].strip())
    except ValueError:
        raise SandboxError(f"cannot parse executable shebang: {path}")


def _runtime_mounts(
    executable: Path, workspace: Path, inherited_env: Mapping[str, str]
) -> tuple[list[Path], list[str], list[str]]:
    mounts: list[Path] = []
    path_entries: list[str] = []
    shebang = _read_shebang(executable)

    def add_path(path: Path) -> None:
        original = Path(os.path.abspath(path))
        try:
            resolved = original.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SandboxError(f"cannot resolve runtime path {original}: {exc}") from exc
        original_in_workspace = _is_within(original, workspace)
        resolved_in_workspace = _is_within(resolved, workspace)
        resolved_in_system = any(_is_within(resolved, Path(root)) for root in _SYSTEM_DIRS)
        if original_in_workspace and not (resolved_in_workspace or resolved_in_system):
            raise SandboxError(
                f"workspace executable or interpreter symlink points outside the workspace: {original}"
            )
        for candidate in (original, resolved):
            if any(_is_within(candidate, Path(root)) for root in _SYSTEM_DIRS):
                continue
            if _is_within(candidate, workspace):
                continue
            venv_root = candidate.parent.parent if candidate.parent.name == "bin" else None
            if venv_root is not None and (venv_root / "pyvenv.cfg").is_file():
                directory = venv_root
            else:
                directory = candidate if candidate.is_dir() else candidate.parent
            if directory not in mounts:
                mounts.append(directory)

    executable_is_workspace = _is_within(executable, workspace)
    add_path(executable)
    env_program = None
    if shebang and shebang[0] == "/usr/bin/env":
        index = 1
        if index < len(shebang) and shebang[index] in {"-S", "--split-string"}:
            index += 1
        if index < len(shebang) and not shebang[index].startswith("-"):
            env_program = shebang[index]
    needs_node = (
        "node" in " ".join(shebang).lower()
        or executable.suffix.lower() in {".js", ".mjs", ".cjs"}
    )
    if env_program:
        resolved_program = shutil.which(env_program, path=inherited_env.get("PATH"))
        if resolved_program is None:
            raise SandboxError(
                f"script requires {env_program!r}, but it was not found in the worker PATH"
            )
        program_candidate = Path(os.path.abspath(resolved_program))
        try:
            program_path = program_candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SandboxError(f"cannot resolve script runtime {env_program}: {exc}") from exc
        if executable_is_workspace and not (
            any(_is_within(program_path, Path(root)) for root in _SYSTEM_DIRS)
            or _is_within(program_path, workspace)
            or env_program == "node"
        ):
            raise SandboxError(
                f"workspace script requests an unapproved host interpreter: {env_program}"
            )
        add_path(program_candidate)
        if str(program_candidate.parent) not in path_entries:
            path_entries.append(str(program_candidate.parent))
    elif needs_node:
        node = shutil.which("node", path=inherited_env.get("PATH"))
        if node is None:
            raise SandboxError("Claude Code launcher requires Node.js, but node was not found in the worker PATH")
        node_candidate = Path(os.path.abspath(node))
        try:
            node_path = node_candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SandboxError(f"cannot resolve Node.js runtime: {exc}") from exc
        add_path(node_candidate)
        if str(node_candidate.parent) not in path_entries:
            path_entries.append(str(node_candidate.parent))

    if shebang and shebang[0] != "/usr/bin/env" and Path(shebang[0]).is_absolute():
        raw_interpreter = Path(shebang[0])
        try:
            interpreter_path = raw_interpreter.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SandboxError(f"cannot resolve script interpreter {raw_interpreter}: {exc}") from exc
        if executable_is_workspace and not (
            _is_within(raw_interpreter, workspace)
            or any(_is_within(interpreter_path, Path(root)) for root in _SYSTEM_DIRS)
        ):
            raise SandboxError(
                f"workspace script references an unapproved host interpreter: {shebang[0]}"
            )
        add_path(raw_interpreter)

    for path in _extra_runtime_paths():
        add_path(path)
        if path.is_dir() and str(path) not in path_entries:
            path_entries.append(str(path))
    return mounts, path_entries, shebang


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


def _curated_environment(
    inherited_env: Mapping[str, str], runtime_path_entries: list[str], workspace: Path
) -> dict[str, str]:
    allowlist = set(_AUTH_ENV_KEYS)
    raw_extra = os.getenv("AI_WORK_OS_SANDBOX_ENV_ALLOWLIST", "")
    for key in filter(None, (part.strip() for part in raw_extra.split(","))):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key):
            raise SandboxError(f"invalid environment variable name in AI_WORK_OS_SANDBOX_ENV_ALLOWLIST: {key!r}")
        allowlist.add(key)

    result = {key: inherited_env[key] for key in allowlist if key in inherited_env}
    result.update({key: inherited_env[key] for key in _BASIC_ENV_KEYS if key in inherited_env})
    workspace_path_entries: list[str] = []
    for entry in inherited_env.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry)
        if not candidate.is_absolute():
            continue
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if _is_within(resolved, workspace):
            mapped = "/workspace/" + resolved.relative_to(workspace).as_posix()
            if mapped not in workspace_path_entries:
                workspace_path_entries.append(mapped)
    system_path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    result["PATH"] = ":".join([*runtime_path_entries, *workspace_path_entries, system_path])
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
    protected_roots = [
        Path("/etc"), Path("/var"), Path("/proc"), Path("/sys"), Path("/dev"),
        Path("/run"), Path("/usr"), Path("/bin"), Path("/sbin"), Path("/lib"),
        Path("/lib64"), Path("/boot"), Path("/opt"), Path("/root"),
    ]
    for protected in protected_roots:
        try:
            protected = protected.resolve(strict=True)
        except OSError:
            pass
        if _is_within(workspace_root, protected):
            raise SandboxError("workspace must not be inside a host system or root-home directory")
    if workspace_root == Path("/") or workspace_root in {Path("/home"), Path("/tmp")}:
        raise SandboxError("workspace must not be a host system or home root directory")
    if workspace_root.parent == Path("/home"):
        raise SandboxError("workspace must be a project directory below the user's home, not the home directory itself")

    task_root = Path(task_dir).resolve(strict=True)
    if _is_within(task_root, workspace_root) or _is_within(workspace_root, task_root):
        raise SandboxError("workspace and task artifact directory/storage tree must be separate")
    mcp_config = task_root / "mcp-config.isolated.json"
    if mcp_config.is_symlink() or not mcp_config.is_file():
        raise SandboxError("isolated MCP config is missing or is not a regular file")

    executable = _resolve_executable(command, workspace_root, inherited)
    runtime_mounts, runtime_path_entries, shebang = _runtime_mounts(executable, workspace_root, inherited)
    curated_env = _curated_environment(inherited, runtime_path_entries, workspace_root)

    args = [
        bwrap,
        "--die-with-parent",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
    ]
    if network == "none":
        args.append("--unshare-net")

    created: set[str] = {"/"}
    for source in _SYSTEM_DIRS:
        source_path = Path(source)
        if source_path.is_symlink():
            _ensure_target_dir(args, str(source_path.parent), created)
            args.extend(["--symlink", os.readlink(source_path), source])
            created.add(source)
        elif source_path.is_dir():
            _ensure_target_dir(args, source, created)
            args.extend(["--ro-bind", source, source])
    for source in _SYSTEM_FILES:
        source_path = Path(source)
        if source_path.is_symlink():
            _ensure_target_dir(args, str(source_path.parent), created)
            args.extend(["--symlink", os.readlink(source_path), source])
            created.add(source)
        elif source_path.is_file():
            _ensure_target_dir(args, source, created)
            args.extend(["--ro-bind", source, source])
    for source in _SYSTEM_DIR_MOUNTS:
        source_path = Path(source)
        if source_path.is_symlink():
            _ensure_target_dir(args, str(source_path.parent), created)
            args.extend(["--symlink", os.readlink(source_path), source])
            created.add(source)
        elif source_path.is_dir():
            _ensure_target_dir(args, source, created)
            args.extend(["--ro-bind", source, source])
    if network == "host" and Path("/etc/resolv.conf").exists():
        _ensure_target_dir(args, "/etc/resolv.conf", created)
        resolv_source = str(Path("/etc/resolv.conf").resolve(strict=True))
        args.extend(["--ro-bind", resolv_source, "/etc/resolv.conf"])

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
    sandbox_args = [
        "/awos/task/mcp-config.isolated.json" if item == str(mcp_config) else item
        for item in command[1:]
    ]
    if shebang and shebang[0] != "/usr/bin/env" and Path(shebang[0]).is_absolute():
        interpreter_path = Path(shebang[0]).resolve(strict=True)
        raw_interpreter = Path(shebang[0])
        if _is_within(raw_interpreter, workspace_root):
            sandbox_interpreter = "/workspace/" + raw_interpreter.relative_to(workspace_root).as_posix()
        else:
            sandbox_interpreter = str(raw_interpreter)
        args.extend([sandbox_interpreter, *shebang[1:], sandbox_executable, *sandbox_args])
    else:
        args.extend([sandbox_executable, *sandbox_args])

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
