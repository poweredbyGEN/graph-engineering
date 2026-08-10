from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import time
import traceback
from dataclasses import replace
from pathlib import Path

import pytest

from graph_engineering.adapters import (
    AdapterError,
    AdapterRequest,
    ExecutionLimits,
    _write_attempted,
    execute_profile,
    normalize_output,
    probe_profile,
)
from graph_engineering.config import parse_agent_config


def executable(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def profile(
    script: Path,
    *,
    transport: str = "stdin",
    output_format: str = "json",
    extra_argv: list[str] | None = None,
    env_allowlist: list[str] | None = None,
):
    marker = {"stdin": [], "argv": ["{prompt}"], "file": ["{prompt_file}"]}[transport]
    raw = {
        "version": 1,
        "profiles": {
            "worker": {
                "adapter": "subprocess",
                "model": "test-model",
                "capabilities": {
                    "read": True,
                    "write": True,
                    "structured_output": True,
                    "worktree": True,
                    "resume": False,
                    "mcp": False,
                },
                "subprocess": {
                    "argv": [sys.executable, str(script), *marker, *(extra_argv or [])],
                    "prompt_transport": transport,
                    "output_format": output_format,
                    "env_allowlist": env_allowlist or [],
                },
            }
        },
    }
    return parse_agent_config(raw).profiles["worker"]


def environment(tmp_path: Path, **values: str) -> dict[str, str]:
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return {
        "HOME": str(tmp_path),
        "PATH": os.environ["PATH"],
        "TMPDIR": str(scratch),
        **values,
    }


def request(tmp_path: Path, prompt: str = "private prompt") -> AdapterRequest:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    return AdapterRequest(
        prompt=prompt,
        cwd=tmp_path,
        allowed_root=tmp_path,
        node_id="node-1",
        run_id="run-1",
        result_schema=schema,
        base_sha="abc123",
    )


@pytest.mark.parametrize("transport", ["stdin", "argv", "file"])
def test_prompt_transports_and_temp_cleanup(tmp_path: Path, transport: str):
    script = executable(
        tmp_path / "worker.py",
        """
import json, pathlib, sys
transport = sys.argv[-1]
if transport == 'stdin':
    prompt = sys.stdin.read()
elif transport == 'argv':
    prompt = sys.argv[1]
else:
    prompt = pathlib.Path(sys.argv[1]).read_text()
print(json.dumps({'answer': prompt}))
""",
    )
    selected = profile(script, transport=transport, extra_argv=[transport])
    result = execute_profile(selected, request(tmp_path), environ=environment(tmp_path))
    assert result.value == {"answer": "private prompt"}
    assert list((tmp_path / "scratch").iterdir()) == []


def test_environment_is_reduced_to_safe_baseline_and_allowlist(tmp_path: Path):
    script = executable(
        tmp_path / "env.py",
        """
import json, os, sys
prompt = sys.stdin.read()
print(json.dumps({'answer': '|'.join([prompt, os.getenv('ALLOWED', ''), os.getenv('FORBIDDEN', '')])}))
""",
    )
    selected = profile(script, env_allowlist=["ALLOWED"])
    env = environment(tmp_path, ALLOWED="yes", FORBIDDEN="no")
    result = execute_profile(selected, request(tmp_path, "p"), environ=env)
    assert result.value == {"answer": "p|yes|"}


def test_large_stdin_does_not_deadlock_before_worker_reads_it(tmp_path: Path):
    script = executable(
        tmp_path / "large.py",
        "import json, sys\nprompt = sys.stdin.read()\nprint(json.dumps({'answer': str(len(prompt))}))\n",
    )
    large_prompt = "x" * (2 * 1024 * 1024)
    result = execute_profile(
        profile(script),
        request(tmp_path, large_prompt),
        environ=environment(tmp_path),
        limits=ExecutionLimits(timeout_seconds=2),
    )
    assert result.value == {"answer": str(len(large_prompt))}


def test_cwd_cannot_escape_worktree_boundary(tmp_path: Path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    script = executable(tmp_path / "noop.py", "print('{}')\n")
    selected = profile(script)
    bad = AdapterRequest("p", outside, root, "n", "r")
    with pytest.raises(AdapterError, match="CWD_ESCAPE"):
        execute_profile(selected, bad, environ=environment(tmp_path))


def test_timeout_kills_worker_process_group(tmp_path: Path):
    pid_file = tmp_path / "child.pid"
    script = executable(
        tmp_path / "hang.py",
        """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
open(sys.argv[1], 'w').write(str(child.pid))
time.sleep(60)
""",
    )
    selected = profile(script, extra_argv=[str(pid_file)])
    with pytest.raises(AdapterError) as caught:
        execute_profile(
            selected,
            request(tmp_path),
            environ=environment(tmp_path),
            limits=ExecutionLimits(timeout_seconds=0.3, terminate_grace_seconds=0.1),
        )
    assert caught.value.code == "TIMEOUT"
    assert caught.value.receipt is not None
    assert caught.value.receipt.exit_code != 0
    child_pid = int(pid_file.read_text())
    for _ in range(30):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        status = Path(f"/proc/{child_pid}/status")
        assert status.is_file() and "State:\tZ" in status.read_text()


def test_timeout_remains_bounded_after_worker_closes_output_streams(tmp_path: Path):
    script = executable(
        tmp_path / "closed.py",
        "import os, time\nos.close(1); os.close(2); time.sleep(60)\n",
    )
    started = time.monotonic()
    with pytest.raises(AdapterError) as caught:
        execute_profile(
            profile(script),
            request(tmp_path),
            environ=environment(tmp_path),
            limits=ExecutionLimits(timeout_seconds=0.2, terminate_grace_seconds=0.1),
        )
    assert caught.value.code == "TIMEOUT"
    assert time.monotonic() - started < 0.8


def test_idempotency_key_is_dispatched_and_bound_to_receipt(tmp_path: Path):
    script = executable(
        tmp_path / "idempotency.py",
        "import json, sys\nsys.stdin.read()\nprint(json.dumps({'answer': sys.argv[1]}))\n",
    )
    selected = profile(script, extra_argv=["{idempotency_key}"])
    key = "stable-run-node-key"
    result = execute_profile(
        selected,
        replace(request(tmp_path), idempotency_key=key),
        environ=environment(tmp_path),
    )
    assert result.value == {"answer": key}
    assert (
        result.receipt.idempotency_key_digest
        == hashlib.sha256(key.encode("utf-8")).hexdigest()
    )


def test_timeout_kills_descendant_after_worker_parent_exits(tmp_path: Path):
    pid_file = tmp_path / "orphan.pid"
    script = executable(
        tmp_path / "orphan.py",
        """
import subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
open(sys.argv[1], 'w').write(str(child.pid))
""",
    )
    selected = profile(script, extra_argv=[str(pid_file)])
    with pytest.raises(AdapterError) as caught:
        execute_profile(
            selected,
            request(tmp_path),
            environ=environment(tmp_path),
            limits=ExecutionLimits(timeout_seconds=0.3, terminate_grace_seconds=0.1),
        )
    assert caught.value.code == "TIMEOUT"
    child_pid = int(pid_file.read_text())
    status = Path(f"/proc/{child_pid}/status")
    if status.exists():
        assert "State:\tZ" in status.read_text()


def test_output_cap_terminates_worker(tmp_path: Path):
    script = executable(
        tmp_path / "loud.py",
        "import sys, time\nsys.stdout.write('x' * 100000); sys.stdout.flush(); time.sleep(10)\n",
    )
    selected = profile(script, output_format="text")
    with pytest.raises(AdapterError) as caught:
        execute_profile(
            selected,
            request(tmp_path),
            environ=environment(tmp_path),
            limits=ExecutionLimits(max_stdout_bytes=100, timeout_seconds=2),
        )
    assert caught.value.code == "OUTPUT_LIMIT"


def test_malformed_json_and_schema_mismatch_fail_closed(tmp_path: Path):
    malformed = executable(tmp_path / "malformed.py", "print('not json')\n")
    with pytest.raises(AdapterError) as caught:
        execute_profile(
            profile(malformed), request(tmp_path), environ=environment(tmp_path)
        )
    assert caught.value.code == "MALFORMED_OUTPUT"

    mismatch = executable(tmp_path / "mismatch.py", "print('{\"answer\": 7}')\n")
    with pytest.raises(AdapterError) as caught:
        execute_profile(
            profile(mismatch), request(tmp_path), environ=environment(tmp_path)
        )
    assert caught.value.code == "SCHEMA_MISMATCH"


def test_schema_mismatch_traceback_does_not_leak_rejected_output(tmp_path: Path):
    # intent: exception chaining must not bypass the receipt's output redaction.
    secret = "worker-secret-7d8ea91"
    mismatch = executable(
        tmp_path / "secret.py",
        f"print({json.dumps(json.dumps({'answer': {'secret': secret}}))})\n",
    )
    with pytest.raises(AdapterError) as caught:
        execute_profile(
            profile(mismatch), request(tmp_path), environ=environment(tmp_path)
        )
    rendered = "".join(
        traceback.format_exception(
            type(caught.value), caught.value, caught.value.__traceback__
        )
    )
    assert caught.value.code == "SCHEMA_MISMATCH"
    assert secret not in rendered


def test_receipt_is_redacted_and_records_hashes_and_sha_hooks(tmp_path: Path):
    script = executable(
        tmp_path / "ok.py",
        "import json, sys\nprint(json.dumps({'answer': sys.stdin.read()}))\n",
    )
    secret_prompt = "do not leak 7c7329"
    result = execute_profile(
        profile(script),
        request(tmp_path, secret_prompt),
        environ=environment(tmp_path),
        base_sha_hook=lambda: "hook-base",
        result_sha_hook=lambda: "def456",
    )
    other = execute_profile(
        profile(script),
        request(tmp_path, "different secret prompt"),
        environ=environment(tmp_path),
    )
    receipt = result.receipt
    assert secret_prompt not in repr(receipt)
    assert receipt.base_sha == "hook-base" and receipt.result_sha == "def456"
    assert len(receipt.command_digest) == len(receipt.stdout_digest) == 64
    assert receipt.command_digest == other.receipt.command_digest
    assert receipt.result_schema_digest is not None


@pytest.mark.parametrize(
    ("style", "raw"),
    [
        (
            "claude",
            json.dumps({"type": "result", "structured_output": {"answer": "claude"}}),
        ),
        (
            "codex",
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": '{"answer":"codex"}',
                    },
                }
            ),
        ),
        ("grok", json.dumps({"result": '{"answer":"grok"}'})),
        (
            "kimi-k3",
            json.dumps({"type": "text", "part": {"text": '{"answer":"kimi-k3"}'}}),
        ),
        (
            "glm-5.2",
            json.dumps({"type": "text", "part": {"text": '{"answer":"glm-5.2"}'}}),
        ),
    ],
)
def test_jsonl_event_normalization_for_five_profiles(style: str, raw: str):
    value, _, events = normalize_output((raw + "\n").encode(), output_format="jsonl")
    assert value == {"answer": style}
    assert len(events) == 1


def test_grok_json_envelope_extracts_text_result():
    # intent: Grok metadata must not be mistaken for the schema-validated result.
    raw = json.dumps(
        {
            "text": '```json\n{"answer":"grok"}\n```',
            "stopReason": "EndTurn",
            "usage": {"input_tokens": 10},
        }
    )
    value, _, events = normalize_output(raw.encode(), output_format="json")
    assert value == {"answer": "grok"}
    assert len(events) == 1


def test_grok_streaming_json_assembles_only_text_data_events():
    raw = b"\n".join(
        [
            json.dumps({"type": "thought", "data": "not output"}).encode(),
            json.dumps({"type": "text", "data": "I'll make the edit."}).encode(),
            json.dumps({"type": "text", "data": '{"answer":'}).encode(),
            json.dumps({"type": "tool", "data": '{"spoof":true}'}).encode(),
            json.dumps({"type": "text", "data": '"grok"}'}).encode(),
            json.dumps({"type": "end", "stopReason": "EndTurn"}).encode(),
        ]
    )

    value, text, events = normalize_output(raw, output_format="jsonl")

    assert value == {"answer": "grok"}
    assert text == 'I\'ll make the edit.{"answer":"grok"}'
    assert len(events) == 6


def test_jsonl_uses_terminal_result_not_spoofed_progress_event():
    # intent: progress/tool output cannot cross the worker result trust boundary.
    events = [
        {"type": "progress", "structured_output": {"answer": "spoofed"}},
        {"type": "result", "structured_output": {"answer": "final"}},
    ]
    raw = "\n".join(json.dumps(event) for event in events) + "\n"
    value, _, captured = normalize_output(raw.encode(), output_format="jsonl")
    assert value == {"answer": "final"}
    assert tuple(events) == captured


def test_jsonl_uses_last_completed_agent_message():
    # intent: a draft Codex message must not concatenate with or override the final one.
    events = [
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": '{"answer":"draft"}'},
        },
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": '{"answer":"final"}'},
        },
    ]
    raw = "\n".join(json.dumps(event) for event in events) + "\n"
    value, _, _ = normalize_output(raw.encode(), output_format="jsonl")
    assert value == {"answer": "final"}


def test_jsonl_never_promotes_codex_error_items_to_a_result():
    # intent: a zero-exit Codex error stream is not an authoritative final answer.
    events = [
        {"type": "thread.started", "thread_id": "opaque"},
        {
            "type": "item.completed",
            "item": {"type": "error", "message": '{"answer":"spoofed"}'},
        },
        {"type": "turn.started"},
    ]
    raw = ("\n".join(json.dumps(event) for event in events) + "\n").encode()
    with pytest.raises(AdapterError) as caught:
        normalize_output(raw, output_format="jsonl")
    assert caught.value.code == "MALFORMED_OUTPUT"


def test_jsonl_parses_entire_stream_before_accepting_structured_result():
    # intent: a plausible early result cannot hide a malformed trailing event.
    raw = b'{"type":"result","structured_output":{"answer":"early"}}\nnot-json\n'
    with pytest.raises(AdapterError) as caught:
        normalize_output(raw, output_format="jsonl")
    assert caught.value.code == "MALFORMED_OUTPUT"


def test_doctor_is_non_mutating_and_openai_compatible_fails_preflight(tmp_path: Path):
    raw = {
        "version": 1,
        "profiles": {
            "remote": {
                "adapter": "openai-compatible",
                "model": "remote-model",
                "capabilities": {
                    "read": False,
                    "write": False,
                    "structured_output": True,
                    "worktree": False,
                    "resume": False,
                    "mcp": False,
                },
                "openai_compatible": {
                    "endpoint_env": "REMOTE_URL",
                    "api_key_env": "REMOTE_KEY",
                },
            }
        },
    }
    selected = parse_agent_config(raw).profiles["remote"]
    probes = probe_profile(
        selected, cwd=tmp_path, allowed_root=tmp_path, environ=environment(tmp_path)
    )
    assert any(p.name == "adapter" and not p.ok for p in probes)
    with pytest.raises(AdapterError) as caught:
        execute_profile(selected, request(tmp_path), environ=environment(tmp_path))
    assert caught.value.code == "UNSUPPORTED_ADAPTER"


def test_subprocess_doctor_checks_executable_and_required_environment(tmp_path: Path):
    script = executable(tmp_path / "doctor.py", "print('{}')\n")
    selected = profile(script, env_allowlist=["REQUIRED_AGENT_KEY"])
    probes = probe_profile(
        selected, cwd=tmp_path, allowed_root=tmp_path, environ=environment(tmp_path)
    )
    by_name = {probe.name: probe for probe in probes}
    assert by_name["cwd-boundary"].ok
    assert by_name["scratch"].ok
    assert by_name["executable"].ok
    assert not by_name["environment"].ok


def test_receipt_identifiers_are_validated_before_execution(tmp_path: Path):
    touched = tmp_path / "spawned-by-invalid-id"
    script = executable(
        tmp_path / "id.py", f"open({str(touched)!r}, 'w').write('yes')\n"
    )
    invalid = AdapterRequest("p", tmp_path, tmp_path, "secret\nvalue", "run")
    with pytest.raises(AdapterError) as caught:
        execute_profile(profile(script), invalid, environ=environment(tmp_path))
    assert caught.value.code == "INVALID_IDENTIFIER"
    assert not touched.exists()


def test_missing_tmpdir_fails_instead_of_falling_back_to_ram_tmp(tmp_path: Path):
    script = executable(tmp_path / "noop.py", "print('{}')\n")
    with pytest.raises(AdapterError) as caught:
        execute_profile(
            profile(script),
            request(tmp_path),
            environ={"PATH": os.environ["PATH"]},
        )
    assert caught.value.code == "SCRATCH_UNCONFIGURED"


def test_missing_cwd_and_scratch_are_deterministic_adapter_errors(tmp_path: Path):
    # intent: bad operator paths must not escape the adapter error contract as OSError.
    script = executable(tmp_path / "noop.py", "print('{}')\n")
    selected = profile(script)
    missing_cwd = AdapterRequest("p", tmp_path / "missing", tmp_path, "n", "r")
    with pytest.raises(AdapterError) as caught:
        execute_profile(selected, missing_cwd, environ=environment(tmp_path))
    assert caught.value.code == "INVALID_CWD"

    bad_env = environment(tmp_path)
    bad_env["TMPDIR"] = str(tmp_path / "missing-scratch")
    with pytest.raises(AdapterError) as caught:
        execute_profile(selected, request(tmp_path), environ=bad_env)
    assert caught.value.code == "SCRATCH_UNWRITABLE"


def test_invalid_result_schema_fails_before_worker_is_spawned(tmp_path: Path):
    touched = tmp_path / "spawned"
    script = executable(
        tmp_path / "touch.py", f"open({str(touched)!r}, 'w').write('yes')\n"
    )
    bad_request = AdapterRequest(
        "p",
        tmp_path,
        tmp_path,
        "n",
        "r",
        result_schema={"type": "definitely-not-a-json-schema-type"},
    )
    with pytest.raises(AdapterError) as caught:
        execute_profile(profile(script), bad_request, environ=environment(tmp_path))
    assert caught.value.code == "INVALID_RESULT_SCHEMA"
    assert not touched.exists()


@pytest.mark.parametrize("syscall", ["rename", "link"])
def test_write_audit_rejects_allowed_source_with_forbidden_destination(
    tmp_path: Path, syscall: str
):
    # intent: one disposable operand cannot hide an attempted write outside it.
    disposable = tmp_path / "state"
    disposable.mkdir()
    audit = tmp_path / "audit.log"
    audit.write_text(
        '1 execve("/usr/bin/worker", ["worker"], 0x0) = 0\n'
        '1 execve("/usr/bin/worker", ["worker"], 0x0) = 0\n'
        f'1 {syscall}("{disposable}/source", "/workspace/repo/escape") = -1 EROFS\n',
        encoding="utf-8",
    )

    assert _write_attempted(audit, writable_roots=(disposable,))


def test_write_audit_rejects_proc_source_with_forbidden_linkat_destination(
    tmp_path: Path,
):
    # intent: process-plumbing exemptions never sanitize another path operand.
    disposable = tmp_path / "state"
    disposable.mkdir()
    audit = tmp_path / "audit.log"
    audit.write_text(
        '1 execve("/usr/bin/worker", ["worker"], 0x0) = 0\n'
        '1 execve("/usr/bin/worker", ["worker"], 0x0) = 0\n'
        '1 linkat(AT_FDCWD, "/proc/self/fd/3", AT_FDCWD, '
        '"/workspace/repo/escape", 0) = -1 EROFS\n',
        encoding="utf-8",
    )

    assert _write_attempted(audit, writable_roots=(disposable,))
