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
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

import jsonschema

from .config import OpenAICompatibleAdapter, Profile, SubprocessAdapter

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
    result_schema: Mapping[str, Any] | None = None
    base_sha: str | None = None
    idempotency_key: str | None = None


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


@dataclass(frozen=True)
class AdapterResult:
    value: Any
    text: str
    events: tuple[Mapping[str, Any], ...]
    receipt: ExecutionReceipt


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
            "complete" if not missing_env else f"missing {missing_env}",
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
    adapter = profile.adapter
    source = os.environ if environ is None else environ
    for label, identifier in (("run_id", request.run_id), ("node_id", request.node_id)):
        if not _IDENTIFIER.fullmatch(identifier):
            raise AdapterError("INVALID_IDENTIFIER", f"{label} is not portable")
    cwd, _ = _bounded_cwd(request.cwd, request.allowed_root)
    scratch = _scratch_root(source)
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
    base_sha = base_sha_hook() if base_sha_hook is not None else request.base_sha
    started_wall = time.time()
    started_monotonic = time.monotonic()

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
        stdin = (
            request.prompt.encode("utf-8")
            if adapter.prompt_transport == "stdin"
            else None
        )
        completed = _run_process(
            argv,
            cwd=cwd,
            env=_environment(adapter, source),
            stdin=stdin,
            limits=active_limits,
        )

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
