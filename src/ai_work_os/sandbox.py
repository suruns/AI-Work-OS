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
    "/etc/hosts",
    "/etc/localtime",
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


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_system_path(path: Path) -> bool:
    return any(_within(path, Path(root)) for root in _SYSTEM_DIRS)


@functools.lru_cache(maxsize=8)
def _probe_bwrap(binary: str) -> None:
    """Probe namespaces with a trusted true binary; this probe alone sees a read-only host root."""
    command = [
        binary,
        "--die-with-parent",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-net",
        "--ro-bind", "/", "/",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--", "/usr/bin/true",
    ]
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=10, check=False, cwd="/",
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
    except ValueError as exc:
        raise SandboxError(f"cannot parse executable shebang: {path}") from exc


def _venv_root(path: Path) -> Path | None:
    parent = path.parent
    if parent.name == "bin" and (parent.parent / "pyvenv.cfg").is_file():
        return parent.parent
    return None


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


def _runtime_mounts(
    executable: Path, workspace: Path, inherited_env: Mapping[str, str]
) -> tuple[list[tuple[Path, str]], list[str], list[str]]:
    """Resolve only executable/runtime directories and map them under /opt/awos-runtime."""
    mounts: list[tuple[Path, str]] = []
    path_entries: list[str] = []
    shebang = _read_shebang(executable)

    def add_directory(directory: Path) -> str:
        directory = Path(os.path.abspath(directory))
        try:
            resolved = directory.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SandboxError(f"runtime directory cannot be resolved: {directory}: {exc}") from exc
        if not resolved.is_dir():
            raise SandboxError(f"runtime mount is not a directory: {directory}")
        if _is_system_path(directory) or _is_system_path(resolved):
            return str(resolved)
        if _within(directory, workspace) or _within(resolved, workspace):
            if _within(directory, workspace):
                return "/workspace/" + directory.relative_to(workspace).as_posix()
            return "/workspace/" + resolved.relative_to(workspace).as_posix()
        for source, target in mounts:
            if directory == source:
                return target
            if _within(directory, source):
                return target + "/" + directory.relative_to(source).as_posix()
            if resolved == source:
                return target
            if _within(resolved, source):
                return target + "/" + resolved.relative_to(source).as_posix()
        target = f"/opt/awos-runtime/runtime-{len(mounts)}"
        mounts.append((resolved, target))
        return target

    def add_path(path: Path) -> str:
        original = Path(os.path.abspath(path))
        try:
            resolved = original.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SandboxError(f"runtime path cannot be resolved: {original}: {exc}") from exc
        if _within(original, workspace) and not (
            _within(resolved, workspace) or _is_system_path(resolved)
        ):
            raise SandboxError(
                f"workspace executable or interpreter symlink points outside the workspace: {original}"
            )
        if _is_system_path(original):
            return str(original)
        if _is_system_path(resolved):
            return str(resolved)
        if _within(original, workspace):
            return "/workspace/" + original.relative_to(workspace).as_posix()
        if _within(resolved, workspace):
            return "/workspace/" + resolved.relative_to(workspace).as_posix()
        root = _venv_root(original) or _venv_root(resolved)
        if root is not None:
            target_root = add_directory(root)
            selected = original if _within(original, root) else resolved
            return target_root + "/" + selected.relative_to(root).as_posix()
        selected = resolved
        target_root = add_directory(selected if selected.is_dir() else selected.parent)
        return target_root if selected.is_dir() else target_root + "/" + selected.name

    executable_is_workspace = _within(executable, workspace)
    resolved_executable = executable.resolve(strict=True)
    executable_target = add_path(executable)

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
        program = shutil.which(env_program, path=inherited_env.get("PATH"))
        if program is None:
            raise SandboxError(f"script requires {env_program!r}, but it was not found in the worker PATH")
        program_candidate = Path(os.path.abspath(program))
        program_resolved = program_candidate.resolve(strict=True)
        if executable_is_workspace and not (
            _is_system_path(program_resolved)
            or _within(program_resolved, workspace)
            or env_program == "node"
        ):
            raise SandboxError(f"workspace script requests an unapproved host interpreter: {env_program}")
        program_target = add_path(program_candidate)
        program_dir = program_candidate.parent
        if _is_system_path(program_resolved):
            program_target = str(program_resolved.parent)
        elif _within(program_dir, workspace):
            program_target = "/workspace/" + program_dir.relative_to(workspace).as_posix()
        else:
            program_target = add_directory(program_dir)
        if program_target not in path_entries:
            path_entries.append(program_target)
    elif needs_node:
        node = shutil.which("node", path=inherited_env.get("PATH"))
        if node is None:
            raise SandboxError("Claude Code launcher requires Node.js, but node was not found in the worker PATH")
        node_candidate = Path(os.path.abspath(node))
        node_resolved = node_candidate.resolve(strict=True)
        add_path(node_candidate)
        node_dir = node_candidate.parent
        node_target = (
            str(node_resolved.parent) if _is_system_path(node_resolved)
            else ("/workspace/" + node_dir.relative_to(workspace).as_posix()
                  if _within(node_dir, workspace) else add_directory(node_dir))
        )
        if node_target not in path_entries:
            path_entries.append(node_target)

    if shebang and shebang[0] != "/usr/bin/env" and Path(shebang[0]).is_absolute():
        raw_interpreter = Path(shebang[0])
        interpreter_resolved = raw_interpreter.resolve(strict=True)
        if executable_is_workspace and not (
            _within(raw_interpreter, workspace) or _is_system_path(interpreter_resolved)
        ):
            raise SandboxError(f"workspace script references an unapproved host interpreter: {shebang[0]}")
        interpreter_target = add_path(raw_interpreter)

    for runtime_dir in _extra_runtime_paths():
        target = add_directory(runtime_dir)
        if target not in path_entries:
            path_entries.append(target)

    # A venv symlink (for example .venv/bin/python -> /usr/bin/python) must be
    # invoked through its venv path so Python still discovers pyvenv.cfg.
    venv = _venv_root(executable)
    if venv is not None and not executable_is_workspace:
        executable_target = add_directory(venv) + "/" + executable.relative_to(venv).as_posix()
    elif executable_is_workspace:
        executable_target = "/workspace/" + executable.relative_to(workspace).as_posix()
    elif _is_system_path(resolved_executable):
        executable_target = str(resolved_executable)
    else:
        executable_target = add_path(resolved_executable)

    return mounts, path_entries, shebang, executable_target


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
        if _within(resolved, workspace):
            mapped = "/workspace/" + resolved.relative_to(workspace).as_posix()
            if mapped not in workspace_path_entries:
                workspace_path_entries.append(mapped)
    system_path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    result["PATH"] = ":".join([*runtime_path_entries, *workspace_path_entries, system_path])
    result["HOME"] = "/tmp/awos-home"
    result["TMPDIR"] = "/tmp"
    return result


def _probe_invocation(command: list[str], env: Mapping[str, str]) -> None:
    """Validate the exact mount plan with a trusted test binary before running task code."""
    try:
        separator = command.index("--")
    except ValueError as exc:
        raise SandboxError("sandbox command is missing its execution separator") from exc
    probe_command = command[:separator + 1] + ["/usr/bin/test", "-x", command[separator + 1]]
    try:
        result = subprocess.run(
            probe_command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=10, check=False, env=dict(env), cwd="/",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError(f"bubblewrap mount preflight failed: {type(exc).__name__}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        raise SandboxError(
            f"bubblewrap mount preflight failed with exit code {result.returncode}"
            + (f": {detail}" if detail else "")
        )


def build_sandbox_invocation(
    command: Sequence[str],
    workspace: Path,
    task_dir: Path,
    permission_mode: str,
    inherited_env: Mapping[str, str] | None = None,
) -> SandboxInvocation | None:
    """Build a fail-closed Bubblewrap invocation; None means legacy unsandboxed mode."""
    mode = sandbox_mode()
    if mode == "off":
        return None
    if permission_mode not in {"plan", "configured"}:
        raise SandboxError("permission mode must be 'plan' or 'configured'")

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
        if _within(workspace_root, protected):
            raise SandboxError("workspace must not be inside a host system or root-home directory")
    if workspace_root == Path("/") or workspace_root in {Path("/home"), Path("/tmp")}:
        raise SandboxError("workspace must not be a host system or home root directory")
    if workspace_root.parent == Path("/home"):
        raise SandboxError("workspace must be a project directory below the user's home, not the home directory itself")

    task_root = Path(task_dir).resolve(strict=True)
    if _within(task_root, workspace_root) or _within(workspace_root, task_root):
        raise SandboxError("workspace and task artifact directory/storage tree must be separate")
    mcp_config = task_root / "mcp-config.isolated.json"
    if mcp_config.is_symlink() or not mcp_config.is_file():
        raise SandboxError("isolated MCP config is missing or is not a regular file")

    executable = _resolve_executable(command, workspace_root, inherited)
    runtime_mounts, runtime_path_entries, shebang, sandbox_executable = _runtime_mounts(
        executable, workspace_root, inherited
    )
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

    # Bind every host source before overlaying /tmp and /home with private tmpfs
    # mounts. Runtime targets live under /opt/awos-runtime, never at host paths.
    for source, target in runtime_mounts:
        _ensure_target_dir(args, target, created)
        args.extend(["--ro-bind", str(source), target])

    _ensure_target_dir(args, "/workspace", created)
    if permission_mode == "plan":
        args.extend(["--ro-bind", str(workspace_root), "/workspace"])
        workspace_access = "read_only"
    else:
        args.extend(["--bind", str(workspace_root), "/workspace"])
        workspace_access = "read_write"

    _ensure_target_dir(args, "/awos/task", created)
    args.extend(["--ro-bind", str(task_root), "/awos/task"])

    args.extend(["--tmpfs", "/tmp", "--tmpfs", "/home", "--tmpfs", "/root"])
    _ensure_target_dir(args, "/tmp/awos-home", created)

    sandbox_args = [
        "/awos/task/mcp-config.isolated.json" if item == str(mcp_config) else item
        for item in command[1:]
    ]
    if shebang and shebang[0] != "/usr/bin/env" and Path(shebang[0]).is_absolute():
        raw_interpreter = Path(shebang[0])
        interpreter_resolved = raw_interpreter.resolve(strict=True)
        if _within(raw_interpreter, workspace_root):
            sandbox_interpreter = "/workspace/" + raw_interpreter.relative_to(workspace_root).as_posix()
        elif _is_system_path(raw_interpreter):
            sandbox_interpreter = str(raw_interpreter)
        else:
            # External interpreters were mounted under /opt/awos-runtime.
            for source, target in runtime_mounts:
                if _within(raw_interpreter, source):
                    sandbox_interpreter = target + "/" + raw_interpreter.relative_to(source).as_posix()
                    break
                if _within(interpreter_resolved, source):
                    sandbox_interpreter = target + "/" + interpreter_resolved.relative_to(source).as_posix()
                    break
            else:
                raise SandboxError(f"script interpreter is not mounted: {raw_interpreter}")
        args.extend([sandbox_interpreter, *shebang[1:], sandbox_executable, *sandbox_args])
    else:
        args.extend([sandbox_executable, *sandbox_args])
    args.extend(["--chdir", "/workspace"]) if False else None
    # Bubblewrap's --chdir is an option and must precede the command separator.
    separator = args.index("--") if "--" in args else -1
    if separator < 0:
        args.extend(["--chdir", "/workspace", "--"])
    else:
        args[separator:separator] = ["--chdir", "/workspace"]

    metadata = {
        "backend": "bubblewrap",
        "network": network,
        "workspace_access": workspace_access,
        "workspace_mount": "/workspace",
        "task_input_mount": "/awos/task (read-only, task-scoped)",
        "runtime_mounts": [str(source) for source, _ in runtime_mounts],
        "runtime_mount_targets": [target for _, target in runtime_mounts],
        "system_mounts": [
            path for path in (*_SYSTEM_DIRS, *_SYSTEM_FILES, *_SYSTEM_DIR_MOUNTS)
            if Path(path).exists()
        ],
        "auth_env_keys": sorted(key for key in curated_env if key in _AUTH_ENV_KEYS),
        "environment_policy": "explicit_auth_allowlist",
    }
    _probe_invocation(args, curated_env)
    return SandboxInvocation(args, curated_env, "/", metadata)
