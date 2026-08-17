"""Shell-free execution adapters for configured graph workers.

This module is intentionally a narrow process boundary.  It does not schedule nodes,
own durable state, or decide which profile to use.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

import jsonschema

from .a2a import A2AError, A2ALimits, execute_a2a
from .artifacts import canonical_json
from .config import A2AAdapter, OpenAICompatibleAdapter, Profile, SubprocessAdapter

SAFE_ENV = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TERM",
        "TMPDIR",
        "USER",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    }
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")


class AdapterError(RuntimeError):
    """A deterministic worker-boundary failure."""

    def __init__(
        self,
        code: str,
        message: str,
        receipt: ExecutionReceipt | None = None,
    ):
        self.code = code
        self.message = message
        self.receipt = receipt
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ExecutionLimits:
    timeout_seconds: float = 900
    terminate_grace_seconds: float = 2
    max_stdout_bytes: int = 8 * 1024 * 1024
    max_stderr_bytes: int = 2 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.terminate_grace_seconds < 0:
            raise ValueError("timeouts must be positive (grace may be zero)")
        if self.max_stdout_bytes <= 0 or self.max_stderr_bytes <= 0:
            raise ValueError("output caps must be positive")


@dataclass(frozen=True)
class AdapterRequest:
    prompt: str
    cwd: Path
    allowed_root: Path
    node_id: str
    run_id: str
    attempt: int | None = None
    result_schema: Mapping[str, Any] | None = None
    base_sha: str | None = None
    idempotency_key: str | None = None
    changeset_schema: Mapping[str, Any] | None = None
    state_path: Path | None = None
    confine_writes: bool = False
    confined_writable_roots: tuple[Path, ...] = ()
    confined_writable_bindings: tuple[tuple[Path, Path], ...] = ()
    confined_readonly_bindings: tuple[tuple[Path, Path], ...] = ()
    confined_environment: tuple[tuple[str, str], ...] = ()
    audit_write_attempts: bool = True
    confined_max_file_bytes: int = 4 * 1024 * 1024
    cancelled: Callable[[], bool] = field(
        default=lambda: False, repr=False, compare=False
    )
    dispatch_guard: Callable[[], None] = field(
        default=lambda: None, repr=False, compare=False
    )


@dataclass(frozen=True)
class ExecutionReceipt:
    run_id: str
    node_id: str
    profile: str
    model: str
    command_digest: str
    result_schema_digest: str | None
    started_at_unix: float
    duration_ms: int
    exit_code: int
    stdout_digest: str
    stderr_digest: str
    stdout_bytes: int
    stderr_bytes: int
    base_sha: str | None
    result_sha: str | None
    idempotency_key_digest: str | None = None
    transport: str = "subprocess"
    remote_task_id_digest: str | None = None
    agent_card_digest: str | None = None
    capability_digest: str | None = None
    protocol_version: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_microusd: int | None = None


@dataclass(frozen=True)
class AdapterResult:
    value: Any
    text: str
    events: tuple[Mapping[str, Any], ...]
    receipt: ExecutionReceipt
    changeset: Any | None = None


def _usage_from_events(
    events: Sequence[Mapping[str, Any]],
) -> tuple[int | None, int | None, int | None]:
    """Extract the final bounded provider usage report without guessing prices."""

    aliases = {
        "input": ("input_tokens", "prompt_tokens", "inputTokens", "promptTokens"),
        "output": (
            "output_tokens",
            "completion_tokens",
            "outputTokens",
            "completionTokens",
        ),
        "cost_usd": ("cost_usd", "total_cost_usd", "costUsd", "totalCostUsd"),
    }
    for event in reversed(events):
        usage = event.get("usage")
        candidates = [usage, event] if isinstance(usage, Mapping) else [event]
        for candidate in candidates:
            input_tokens = next(
                (candidate[key] for key in aliases["input"] if key in candidate), None
            )
            output_tokens = next(
                (candidate[key] for key in aliases["output"] if key in candidate), None
            )
            cost_usd = next(
                (candidate[key] for key in aliases["cost_usd"] if key in candidate),
                None,
            )
            if input_tokens is None and output_tokens is None and cost_usd is None:
                continue
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value < 0
                for value in (input_tokens, output_tokens, cost_usd)
                if value is not None
            ):
                return None, None, None
            return (
                int(input_tokens) if input_tokens is not None else None,
                int(output_tokens) if output_tokens is not None else None,
                round(float(cost_usd) * 1_000_000) if cost_usd is not None else None,
            )
    return None, None, None


@dataclass(frozen=True)
class DoctorProbe:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class _ProcessOutput:
    stdout: bytes
    stderr: bytes
    exit_code: int
    failure_code: str | None = None
    failure_message: str | None = None


_WRITE_TRACE_SYSCALLS = (
    "execve,open,openat,openat2,creat,unlink,unlinkat,rename,renameat,renameat2,"
    "mkdir,mkdirat,rmdir,link,linkat,symlink,symlinkat,truncate,ftruncate,"
    "chmod,fchmod,fchmodat,chown,fchown,fchownat,lchown,utime,utimes,"
    "utimensat,mknod,mknodat,setxattr,lsetxattr,fsetxattr,removexattr,"
    "lremovexattr,fremovexattr"
)
_MUTATING_TRACE = re.compile(
    r"\b(?:creat|unlink(?:at)?|rename(?:at2?)?|mkdir(?:at)?|rmdir|link(?:at)?|"
    r"symlink(?:at)?|truncate|ftruncate|chmod|fchmod(?:at)?|chown|fchown(?:at)?|"
    r"lchown|utime|utimes|utimensat|mknod(?:at)?|(?:l|f)?setxattr|"
    r"(?:l|f)?removexattr)\("
    r"|\bopen(?:at2?)?\([^\n]*(?:O_WRONLY|O_RDWR|O_CREAT|O_TRUNC|O_APPEND)"
)
_TRACE_QUOTED = re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"')


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _bounded_cwd(cwd: Path, allowed_root: Path) -> tuple[Path, Path]:
    try:
        resolved_cwd = cwd.expanduser().resolve(strict=True)
        resolved_root = allowed_root.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AdapterError(
            "INVALID_CWD", "cwd or allowed root cannot be resolved"
        ) from exc
    if not resolved_cwd.is_dir() or not resolved_root.is_dir():
        raise AdapterError("INVALID_CWD", "cwd and allowed root must be directories")
    if not resolved_cwd.is_relative_to(resolved_root):
        raise AdapterError("CWD_ESCAPE", "cwd is outside the authorized worktree root")
    return resolved_cwd, resolved_root


def _scratch_root(environ: Mapping[str, str]) -> Path:
    configured = environ.get("TMPDIR")
    if not configured:
        raise AdapterError(
            "SCRATCH_UNCONFIGURED", "TMPDIR must name disk-backed scratch"
        )
    try:
        root = Path(configured).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AdapterError(
            "SCRATCH_UNWRITABLE", "TMPDIR is not a resolvable directory"
        ) from exc
    if not root.is_dir() or not os.access(root, os.W_OK | os.X_OK):
        raise AdapterError("SCRATCH_UNWRITABLE", "TMPDIR is not a writable directory")
    return root


def _environment(
    adapter: SubprocessAdapter, environ: Mapping[str, str]
) -> dict[str, str]:
    names = SAFE_ENV | frozenset(adapter.env_allowlist)
    return {name: environ[name] for name in sorted(names) if name in environ}


def _write_confined_argv(
    argv: Sequence[str],
    *,
    cwd: Path,
    allowed_root: Path,
    writable_roots: Sequence[Path],
    writable_bindings: Sequence[tuple[Path, Path]],
    readonly_bindings: Sequence[tuple[Path, Path]],
    audit_path: Path,
    path: str,
    audit_write_attempts: bool,
    max_file_bytes: int,
) -> list[str]:
    """Wrap a smoke worker in a read-only mount namespace and syscall audit.

    The worker can read the host and use the network, but only explicitly disposable
    roots are writable. The requested repository stays read-only. The trace records
    both successful and denied mutation attempts, including writes a worker catches
    and suppresses. There is deliberately no unconfined fallback.
    """

    required_tools = ["bwrap", "prlimit"]
    if audit_write_attempts:
        required_tools.append("strace")
    tools = {name: shutil.which(name, path=path) for name in required_tools}
    if any(value is None for value in tools.values()):
        raise AdapterError(
            "CONFINEMENT_UNAVAILABLE",
            "required local write-confinement helper is unavailable",
        )
    try:
        root = allowed_root.resolve(strict=True)
        writable = tuple(item.resolve(strict=True) for item in writable_roots)
        writable_aliases = tuple(
            (source.resolve(strict=True), target.resolve(strict=True))
            for source, target in writable_bindings
        )
        readonly = tuple(
            (source.resolve(strict=True), target.resolve(strict=True))
            for source, target in readonly_bindings
        )
        audit = audit_path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise AdapterError(
            "CONFINEMENT_UNAVAILABLE", "write-confinement root is invalid"
        ) from exc
    if any(
        not any(source == item or source.is_relative_to(item) for item in writable)
        or source.is_dir() != target.is_dir()
        or target == root
        or target.is_relative_to(root)
        or root.is_relative_to(target)
        or audit == target
        or audit.is_relative_to(target)
        for source, target in writable_aliases
    ) or (
        not audit.parent.is_dir()
        or audit.is_relative_to(root)
        or not cwd.is_relative_to(root)
        or any(item == root or root.is_relative_to(item) for item in writable)
        or any(audit.is_relative_to(item) for item in writable)
    ):
        raise AdapterError(
            "CONFINEMENT_UNAVAILABLE", "write-confinement boundary is invalid"
        )
    command = [
        tools["prlimit"],
        f"--fsize={max_file_bytes}:{max_file_bytes}",
        "--",
    ]
    if audit_write_attempts:
        command.extend(
            (
                tools["strace"],
                "-f",
                "-qq",
                "-yy",
                "-e",
                f"trace={_WRITE_TRACE_SYSCALLS}",
                "-o",
                str(audit),
                "--",
            )
        )
    command.extend(
        (
            tools["bwrap"],
            "--unshare-all",
            "--unshare-user",
            "--share-net",
            "--die-with-parent",
            "--disable-userns",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--chdir",
            str(cwd),
        )
    )
    for item in writable:
        command.extend(("--bind", str(item), str(item)))
    for source, target in writable_aliases:
        command.extend(("--bind", str(source), str(target)))
    for source, target in readonly:
        command.extend(("--ro-bind", str(source), str(target)))
    command.extend(("--", *argv))
    return command


def _write_attempted(
    audit_path: Path,
    *,
    writable_roots: Sequence[Path],
    writable_aliases: Sequence[Path] = (),
) -> bool:
    try:
        trace = audit_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    allowed_paths = tuple(
        root.resolve(strict=True) for root in (*writable_roots, *writable_aliases)
    )
    allowed = tuple(str(root) for root in allowed_paths)
    successful_execs = 0
    worker_started = False
    for line in trace.splitlines():
        if " execve(" in line and line.rstrip().endswith("= 0"):
            successful_execs += 1
            worker_started = successful_execs >= 2
            continue
        if not worker_started:
            continue
        if not _MUTATING_TRACE.search(line):
            continue
        # Disposable HOME/XDG/tool state is intentionally writable. With strace
        # ``-yy``, fd-relative mutations also carry their resolved backing path.
        quoted = _TRACE_QUOTED.findall(line)
        parent_escape = any(".." in Path(item).parts for item in quoted)
        # Multi-path mutations must classify every filesystem operand.  An
        # allowed source must never sanitize a forbidden destination (for
        # example rename(disposable, repo) or link(disposable, host)).
        multi_path = re.search(
            r"\b(rename(?:at2?)?|link(?:at)?|symlink(?:at)?)\(", line
        )
        if multi_path:
            syscall = multi_path.group(1)
            path_indexes = {
                "rename": (0, 1),
                "renameat": (0, 1),
                "renameat2": (0, 1),
                "link": (0, 1),
                "linkat": (0, 1),
                "symlink": (0, 1),
                "symlinkat": (0, 1),
            }[syscall]
            operands = [quoted[index] for index in path_indexes if index < len(quoted)]
            if syscall.startswith("symlink") and _confined_relative_symlink(
                line, allowed_paths
            ):
                continue
            if len(operands) < 2 or any(
                not _trace_operand_is_allowed(item, allowed_paths) for item in operands
            ):
                return True
        # Device handles are process plumbing for single-path mutations.  This
        # exemption comes after multi-path classification so a /proc or /dev
        # source cannot hide a forbidden rename/link destination.
        if any(prefix in line for prefix in ("/dev/", "/proc/", "/sys/")):
            continue
        in_disposable = any(
            f'"{root}"' in line or f'"{root}/' in line or f"<{root}/" in line
            for root in allowed
        )
        if in_disposable and (
            not parent_escape or _confined_relative_symlink(line, allowed_paths)
        ):
            continue
        return True
    return False


def _trace_operand_is_allowed(value: str, allowed_roots: Sequence[Path]) -> bool:
    """Return whether one absolute trace operand stays in disposable state."""

    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        return False
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return any(
        resolved == root or resolved.is_relative_to(root) for root in allowed_roots
    )


def _confined_relative_symlink(line: str, allowed_roots: Sequence[Path]) -> bool:
    """Accept a lexical parent only when a symlink still resolves inside its root."""

    if not re.search(r"\bsymlink\(", line):
        return False
    quoted = _TRACE_QUOTED.findall(line)
    if len(quoted) != 2:
        return False
    target = Path(quoted[0])
    link = Path(quoted[1])
    if not link.is_absolute():
        return False
    owner = next(
        (root for root in allowed_roots if link == root or link.is_relative_to(root)),
        None,
    )
    if owner is None:
        return False
    try:
        resolved = (
            target.resolve(strict=False)
            if target.is_absolute()
            else (link.parent / target).resolve(strict=False)
        )
    except (OSError, RuntimeError):
        return False
    return resolved == owner or resolved.is_relative_to(owner)


def _format_argv(template: Sequence[str], values: Mapping[str, str]) -> list[str]:
    """Format validated templates without a shell or implicit coercions."""

    try:
        return [argument.format_map(values) for argument in template]
    except (KeyError, ValueError) as exc:
        raise AdapterError(
            "ARGV_FORMAT", f"cannot format validated argv: {exc}"
        ) from exc


def _terminate_group(process: subprocess.Popen[bytes], grace: float) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    if process.poll() is None:
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
    elif grace:
        # A worker can exit while a descendant still owns its stdout/stderr pipes.
        # Give that process group the same bounded TERM grace before escalation.
        time.sleep(grace)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.wait()


def _run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdin: bytes | None,
    limits: ExecutionLimits,
) -> _ProcessOutput:
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        raise AdapterError("SPAWN_FAILED", str(exc)) from exc

    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    stdin_view = memoryview(stdin) if stdin is not None else None
    stdin_offset = 0
    if stdin_view is not None:
        assert process.stdin is not None
        os.set_blocking(process.stdin.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    caps = {
        "stdout": limits.max_stdout_bytes,
        "stderr": limits.max_stderr_bytes,
    }
    started = time.monotonic()
    failure: AdapterError | None = None
    try:
        # Keep the wall-clock bound even when a worker closes both output streams
        # and remains alive.  Waiting solely on registered file descriptors would
        # fall through to an unbounded process.wait() in that state.
        while process.poll() is None or selector.get_map():
            elapsed = time.monotonic() - started
            if elapsed >= limits.timeout_seconds:
                failure = AdapterError("TIMEOUT", "worker exceeded wall-clock timeout")
                break
            ready = (
                selector.select(timeout=min(0.1, limits.timeout_seconds - elapsed))
                if selector.get_map()
                else ()
            )
            if not ready and not selector.get_map():
                time.sleep(min(0.05, limits.timeout_seconds - elapsed))
            for key, _ in ready:
                if key.data == "stdin":
                    assert stdin_view is not None
                    try:
                        written = os.write(
                            key.fd, stdin_view[stdin_offset : stdin_offset + 65536]
                        )
                    except BrokenPipeError:
                        written = len(stdin_view) - stdin_offset
                    stdin_offset += written
                    if stdin_offset >= len(stdin_view):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                stream = key.data
                if len(buffers[stream]) + len(chunk) > caps[stream]:
                    allowed = caps[stream] - len(buffers[stream])
                    buffers[stream].extend(chunk[:allowed])
                    failure = AdapterError(
                        "OUTPUT_LIMIT", f"{stream} exceeded configured byte cap"
                    )
                    break
                buffers[stream].extend(chunk)
            if failure is not None:
                break
        if failure is not None:
            _terminate_group(process, limits.terminate_grace_seconds)
        exit_code = process.wait()
    finally:
        selector.close()
        if process.poll() is None:
            _terminate_group(process, limits.terminate_grace_seconds)
    return _ProcessOutput(
        bytes(buffers["stdout"]),
        bytes(buffers["stderr"]),
        exit_code,
        failure.code if failure is not None else None,
        failure.message if failure is not None else None,
    )


def _json_from_text(text: str) -> Any | None:
    candidate = text.strip()
    if candidate.startswith("```json") and candidate.endswith("```"):
        candidate = candidate[7:-3].strip()
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        pass

    # Some coding CLIs stream a brief status sentence before obeying the final
    # JSON-only instruction. Accept only a complete JSON object/array anchored at
    # the end of the authoritative assistant text channel; earlier embedded JSON
    # and tool/progress events cannot win.
    decoder = json.JSONDecoder()
    for index in range(len(candidate) - 1, -1, -1):
        if candidate[index] not in "{[":
            continue
        try:
            value, end = decoder.raw_decode(candidate, index)
        except json.JSONDecodeError:
            continue
        if not candidate[end:].strip():
            return value
    return None


def _event_text(
    event: Mapping[str, Any],
) -> tuple[str, Literal["append", "replace"]] | None:
    """Extract text only from a known provider result event.

    Event streams are untrusted process output.  In particular, arbitrary progress
    and tool events must not be able to impersonate the worker's final result.
    """

    event_type = event.get("type")
    # Claude stream-json uses one terminal result event.
    if event_type in {None, "result"}:
        value = event.get("result")
        if isinstance(value, str):
            return value, "replace"
        # Grok's JSON envelope names the final response ``text``.
        value = event.get("text")
        if event_type is None and isinstance(value, str):
            return value, "replace"

    # Codex JSONL: item.completed -> item(agent_message).text.  A turn may emit
    # more than one agent message, so the last completed message is authoritative.
    item = event.get("item")
    if (
        event_type == "item.completed"
        and isinstance(item, Mapping)
        and item.get("type") == "agent_message"
    ):
        for key in ("text", "content"):
            value = item.get(key)
            if isinstance(value, str):
                return value, "replace"

    # Grok streaming-json emits final response chunks as {"type":"text","data":"..."}.
    # Tool/progress events use other types and cannot impersonate this channel.
    if event_type == "text" and isinstance(event.get("data"), str):
        return event["data"], "append"

    # OpenCode JSONL: text events are streamed chunks. Kimi/GLM use this surface.
    part = event.get("part")
    if (
        event_type == "text"
        and isinstance(part, Mapping)
        and isinstance(part.get("text"), str)
    ):
        return part["text"], "append"
    return None


def normalize_output(
    raw: bytes, *, output_format: Literal["text", "json", "jsonl"]
) -> tuple[Any, str, tuple[Mapping[str, Any], ...]]:
    """Normalize Claude, Codex, Grok, and OpenCode (Kimi/GLM) outputs."""

    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AdapterError("OUTPUT_ENCODING", "stdout is not UTF-8") from exc

    if output_format == "text":
        parsed = _json_from_text(decoded)
        return (parsed if parsed is not None else decoded, decoded, ())

    if output_format == "json":
        try:
            document = json.loads(decoded)
        except json.JSONDecodeError as exc:
            raise AdapterError("MALFORMED_OUTPUT", f"invalid JSON: {exc}") from exc
        if not isinstance(document, Mapping):
            return document, decoded, ()
        structured = document.get("structured_output")
        if structured is not None:
            return structured, decoded, (document,)
        extracted = _event_text(document)
        if extracted is not None:
            text, _ = extracted
            parsed = _json_from_text(text)
            return (parsed if parsed is not None else text, text, (document,))
        return document, decoded, (document,)

    events: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(decoded.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AdapterError(
                "MALFORMED_OUTPUT", f"invalid JSONL at line {line_number}: {exc}"
            ) from exc
        if not isinstance(event, Mapping):
            raise AdapterError(
                "MALFORMED_OUTPUT", f"JSONL line {line_number} is not an object"
            )
        events.append(event)
    if not events:
        raise AdapterError("MALFORMED_OUTPUT", "JSONL stdout contained no events")

    # Parse the complete stream before extracting a result.  This both catches a
    # malformed suffix and prevents an early progress event from winning.
    structured = [
        event["structured_output"]
        for event in events
        if event.get("type") == "result" and "structured_output" in event
    ]
    if structured:
        return structured[-1], decoded, tuple(events)

    text = ""
    found_text = False
    for event in events:
        extracted = _event_text(event)
        if extracted is None:
            continue
        chunk, mode = extracted
        text = text + chunk if mode == "append" else chunk
        found_text = True
    parsed = _json_from_text(text)
    if parsed is not None:
        return parsed, text, tuple(events)
    if found_text:
        return text, text, tuple(events)
    raise AdapterError("MALFORMED_OUTPUT", "JSONL stdout contained no final result")


def probe_profile(
    profile: Profile,
    *,
    cwd: Path,
    allowed_root: Path,
    environ: Mapping[str, str] | None = None,
) -> tuple[DoctorProbe, ...]:
    """Return non-mutating doctor checks; callers decide presentation/policy."""

    source = os.environ if environ is None else environ
    probes: list[DoctorProbe] = []
    try:
        _bounded_cwd(cwd, allowed_root)
        probes.append(DoctorProbe("cwd-boundary", True, "cwd is within allowed root"))
    except AdapterError as exc:
        probes.append(DoctorProbe("cwd-boundary", False, exc.code))
    try:
        _scratch_root(source)
        probes.append(DoctorProbe("scratch", True, "TMPDIR is writable"))
    except AdapterError as exc:
        probes.append(DoctorProbe("scratch", False, exc.code))

    if isinstance(profile.adapter, OpenAICompatibleAdapter):
        probes.append(
            DoctorProbe(
                "adapter", False, "openai-compatible execution is not implemented"
            )
        )
        return tuple(probes)
    if isinstance(profile.adapter, A2AAdapter):
        missing = not source.get(profile.adapter.auth_env)
        probes.append(
            DoctorProbe(
                "a2a-auth",
                not missing,
                "configured" if not missing else "required reference missing (1)",
            )
        )
        probes.append(
            DoctorProbe(
                "a2a-profile",
                bool(
                    profile.adapter.allowed_skills and profile.adapter.expected_identity
                ),
                "bounded private Agent Card profile",
            )
        )
        return tuple(probes)
    path = source.get("PATH", os.defpath)
    executable = shutil.which(profile.adapter.argv[0], path=path)
    probes.append(
        DoctorProbe(
            "executable",
            executable is not None,
            "found" if executable else "not found on reduced PATH",
        )
    )
    missing_env = sorted(
        name for name in profile.adapter.env_allowlist if not source.get(name)
    )
    probes.append(
        DoctorProbe(
            "environment",
            not missing_env,
            "complete"
            if not missing_env
            else f"required references missing ({len(missing_env)})",
        )
    )
    return tuple(probes)


def execute_profile(
    profile: Profile,
    request: AdapterRequest,
    *,
    limits: ExecutionLimits | None = None,
    environ: Mapping[str, str] | None = None,
    base_sha_hook: Callable[[], str | None] | None = None,
    result_sha_hook: Callable[[], str | None] | None = None,
) -> AdapterResult:
    """Execute one configured subprocess profile and return a redacted receipt."""

    if isinstance(profile.adapter, OpenAICompatibleAdapter):
        raise AdapterError(
            "UNSUPPORTED_ADAPTER",
            "openai-compatible execution is not implemented; use a subprocess harness",
        )
    source = os.environ if environ is None else environ
    for label, identifier in (("run_id", request.run_id), ("node_id", request.node_id)):
        if not _IDENTIFIER.fullmatch(identifier):
            raise AdapterError("INVALID_IDENTIFIER", f"{label} is not portable")
    cwd, _ = _bounded_cwd(request.cwd, request.allowed_root)
    active_limits = limits or ExecutionLimits()
    schema_json: str | None = None
    if request.result_schema is not None:
        try:
            jsonschema.validators.validator_for(request.result_schema).check_schema(
                request.result_schema
            )
            schema_json = json.dumps(request.result_schema, sort_keys=True)
        except (jsonschema.SchemaError, TypeError, ValueError) as exc:
            raise AdapterError("INVALID_RESULT_SCHEMA", str(exc)) from exc
    if request.changeset_schema is not None:
        try:
            jsonschema.validators.validator_for(request.changeset_schema).check_schema(
                request.changeset_schema
            )
        except (jsonschema.SchemaError, TypeError, ValueError) as exc:
            raise AdapterError("INVALID_CHANGESET_SCHEMA", str(exc)) from exc
    base_sha = base_sha_hook() if base_sha_hook is not None else request.base_sha
    started_wall = time.time()
    started_monotonic = time.monotonic()

    if request.confine_writes and not isinstance(profile.adapter, SubprocessAdapter):
        raise AdapterError(
            "CONFINEMENT_UNAVAILABLE",
            "local write confinement is unavailable for this adapter",
        )

    if isinstance(profile.adapter, A2AAdapter):
        if request.state_path is None:
            raise AdapterError(
                "A2A_STATE_REQUIRED", "A2A execution requires durable state"
            )
        auth = source.get(profile.adapter.auth_env, "")
        # This is deliberately the last local action before the remote request.
        # Preflight performed while constructing the scheduler is not enough: a
        # reviewed project/private policy may drift while a node waits in the
        # ready queue.
        request.dispatch_guard()
        try:
            outcome = execute_a2a(
                profile,
                prompt=request.prompt,
                run_id=request.run_id,
                node_id=request.node_id,
                attempt=request.attempt,
                state_path=str(request.state_path),
                idempotency_key=request.idempotency_key,
                auth=auth,
                limits=A2ALimits(
                    timeout_seconds=active_limits.timeout_seconds,
                    max_body_bytes=active_limits.max_stdout_bytes,
                ),
                cancelled=request.cancelled,
                writer=request.changeset_schema is not None,
            )
        except A2AError as exc:
            duration_ms = round((time.monotonic() - started_monotonic) * 1000)
            receipt = ExecutionReceipt(
                run_id=request.run_id,
                node_id=request.node_id,
                profile=profile.name,
                model=profile.model,
                command_digest=_digest(
                    canonical_json(
                        {
                            "adapter": "a2a",
                            "identity": profile.adapter.expected_identity,
                            "skills": profile.adapter.allowed_skills,
                        }
                    )
                ),
                result_schema_digest=(
                    _digest(schema_json.encode("utf-8")) if schema_json else None
                ),
                started_at_unix=started_wall,
                duration_ms=duration_ms,
                exit_code=1,
                stdout_digest=_digest(b""),
                stderr_digest=_digest(b""),
                stdout_bytes=0,
                stderr_bytes=0,
                base_sha=base_sha,
                result_sha=None,
                idempotency_key_digest=(
                    _digest(request.idempotency_key.encode("utf-8"))
                    if request.idempotency_key is not None
                    else None
                ),
                transport="a2a",
            )
            raise AdapterError(exc.code, exc.message, receipt) from None
        duration_ms = round((time.monotonic() - started_monotonic) * 1000)
        receipt = ExecutionReceipt(
            run_id=request.run_id,
            node_id=request.node_id,
            profile=profile.name,
            model=profile.model,
            command_digest=_digest(
                canonical_json(
                    {
                        "adapter": "a2a",
                        "identity": profile.adapter.expected_identity,
                        "skills": profile.adapter.allowed_skills,
                    }
                )
            ),
            result_schema_digest=(
                _digest(schema_json.encode("utf-8")) if schema_json else None
            ),
            started_at_unix=started_wall,
            duration_ms=duration_ms,
            exit_code=0,
            stdout_digest=outcome.response_digest,
            stderr_digest=_digest(b""),
            stdout_bytes=outcome.response_bytes,
            stderr_bytes=0,
            base_sha=base_sha,
            result_sha=result_sha_hook() if result_sha_hook is not None else None,
            idempotency_key_digest=(
                _digest(request.idempotency_key.encode("utf-8"))
                if request.idempotency_key is not None
                else None
            ),
            transport="a2a",
            remote_task_id_digest=(
                _digest(outcome.task_id.encode("utf-8")) if outcome.task_id else None
            ),
            agent_card_digest=outcome.card_digest,
            capability_digest=outcome.capability_digest,
            protocol_version=outcome.protocol_version,
        )
        try:
            if request.result_schema is not None:
                jsonschema.validate(outcome.value, request.result_schema)
            if request.changeset_schema is not None:
                jsonschema.validate(outcome.changeset, request.changeset_schema)
        except jsonschema.ValidationError:
            raise AdapterError(
                "SCHEMA_MISMATCH",
                "remote artifact did not match its requested schema",
                receipt,
            ) from None
        events = tuple(
            {"type": "a2a.task-status", "state": state} for state in outcome.statuses
        )
        return AdapterResult(outcome.value, "", events, receipt, outcome.changeset)

    adapter = profile.adapter
    scratch = _scratch_root(source)

    with TemporaryDirectory(prefix="graph-engineering-", dir=scratch) as temp_dir:
        temp = Path(temp_dir)
        prompt_file = temp / "prompt.txt"
        schema_file = temp / "result-schema.json"
        if adapter.prompt_transport == "file":
            prompt_file.write_text(request.prompt, encoding="utf-8")
            prompt_file.chmod(0o600)
        if schema_json is not None:
            schema_file.write_text(schema_json, encoding="utf-8")
            schema_file.chmod(0o600)

        values = {
            "cwd": str(cwd),
            "idempotency_key": request.idempotency_key or "",
            "model": profile.model,
            "node_id": request.node_id,
            "prompt": request.prompt,
            "prompt_file": str(prompt_file),
            "run_id": request.run_id,
            "schema_file": str(schema_file),
        }
        argv = _format_argv(adapter.argv, values)
        audit_path = temp / "write-audit.log"
        if request.confine_writes:
            if request.confined_max_file_bytes <= 0:
                raise AdapterError(
                    "CONFINEMENT_UNAVAILABLE",
                    "confined file-size limit must be positive",
                )
            argv = _write_confined_argv(
                argv,
                cwd=cwd,
                allowed_root=request.allowed_root,
                writable_roots=request.confined_writable_roots,
                writable_bindings=request.confined_writable_bindings,
                readonly_bindings=request.confined_readonly_bindings,
                audit_path=audit_path,
                path=source.get("PATH", os.defpath),
                audit_write_attempts=request.audit_write_attempts,
                max_file_bytes=request.confined_max_file_bytes,
            )
        stdin = (
            request.prompt.encode("utf-8")
            if adapter.prompt_transport == "stdin"
            else None
        )
        # Fence the actual process boundary, after all local request setup but
        # immediately before the worker can observe or mutate anything.
        request.dispatch_guard()
        process_env = _environment(adapter, source)
        if request.confine_writes:
            if any(name not in SAFE_ENV for name, _ in request.confined_environment):
                raise AdapterError(
                    "CONFINEMENT_UNAVAILABLE",
                    "confined environment contains an unsupported variable",
                )
            process_env.update(request.confined_environment)
            if request.audit_write_attempts:
                process_env["GRAPH_ENGINEERING_WRITE_AUDIT"] = str(audit_path)
        completed = _run_process(
            argv,
            cwd=cwd,
            env=process_env,
            stdin=stdin,
            limits=active_limits,
        )
        if (
            request.confine_writes
            and request.audit_write_attempts
            and _write_attempted(
                audit_path,
                writable_roots=request.confined_writable_roots,
                writable_aliases=tuple(
                    target for _, target in request.confined_writable_bindings
                ),
            )
        ):
            completed = _ProcessOutput(
                completed.stdout,
                completed.stderr,
                completed.exit_code,
                "WRITE_DETECTED",
                "worker attempted a filesystem mutation",
            )
        if request.confine_writes:
            audit_path.unlink(missing_ok=True)

    duration_ms = round((time.monotonic() - started_monotonic) * 1000)
    schema_digest = _digest(schema_json.encode("utf-8")) if schema_json else None
    redacted_values = {
        **values,
        "prompt": "<redacted-prompt>",
        "prompt_file": "<prompt-file>",
        "schema_file": "<schema-file>",
    }
    receipt_argv = _format_argv(adapter.argv, redacted_values)
    receipt = ExecutionReceipt(
        run_id=request.run_id,
        node_id=request.node_id,
        profile=profile.name,
        model=profile.model,
        command_digest=_digest("\0".join(receipt_argv).encode("utf-8")),
        result_schema_digest=schema_digest,
        started_at_unix=started_wall,
        duration_ms=duration_ms,
        exit_code=completed.exit_code,
        stdout_digest=_digest(completed.stdout),
        stderr_digest=_digest(completed.stderr),
        stdout_bytes=len(completed.stdout),
        stderr_bytes=len(completed.stderr),
        base_sha=base_sha,
        result_sha=result_sha_hook() if result_sha_hook is not None else None,
        idempotency_key_digest=(
            _digest(request.idempotency_key.encode("utf-8"))
            if request.idempotency_key is not None
            else None
        ),
    )
    if completed.failure_code is not None:
        raise AdapterError(
            completed.failure_code,
            completed.failure_message or "worker execution failed",
            receipt,
        )
    if completed.exit_code != 0:
        raise AdapterError(
            "WORKER_EXIT", f"worker exited {completed.exit_code}", receipt
        )
    try:
        value, text, events = normalize_output(
            completed.stdout, output_format=adapter.output_format
        )
    except AdapterError as exc:
        exc.receipt = receipt
        raise
    input_tokens, output_tokens, cost_microusd = _usage_from_events(events)
    receipt = replace(
        receipt,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_microusd=cost_microusd,
    )
    if request.result_schema is not None:
        try:
            jsonschema.validate(value, request.result_schema)
        except jsonschema.ValidationError:
            # jsonschema's exception renders the rejected instance.  Chaining it
            # into a traceback would bypass receipt redaction and can expose a
            # prompt or secret echoed by the worker.
            raise AdapterError(
                "SCHEMA_MISMATCH",
                "worker result did not match the requested schema",
                receipt,
            ) from None
    return AdapterResult(value, text, events, receipt)
