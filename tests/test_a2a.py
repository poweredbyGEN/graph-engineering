from __future__ import annotations

import json
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from graph_engineering.adapters import (
    AdapterError,
    AdapterRequest,
    ExecutionLimits,
    execute_profile,
)
from graph_engineering.config import ConfigError, parse_agent_config
from graph_engineering.orchestrator import (
    CHANGE_SET_SCHEMA,
    PortableRuntime,
    change_set_value,
)
from graph_engineering.state import StaleAttemptError, StateStore
from graph_engineering.worktrees import WorktreeManager


class Remote:
    def __init__(self) -> None:
        self.auth = "private-token-77"
        self.reset()

    def reset(self) -> None:
        self.identity = "remote-review"
        self.skill = {"id": "review-diff", "name": "Review a diff"}
        self.interface_url: str | None = None
        self.protocol_version = "1.0"
        self.bearer_security = True
        self.security_schemes = {
            "bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}
        }
        self.security_requirements = [{"schemes": {"bearer": {"list": []}}}]
        self.status = "TASK_STATE_COMPLETED"
        self.result = {"answer": "accepted"}
        self.poll_task_id: str | None = None
        self.sends = self.gets = self.cancels = 0
        self.disconnect_get_once = self.malformed = self.oversized = False
        self.redirect_card = False


@contextmanager
def server(remote: Remote):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def _json(self, value: Any, status: int = 200) -> None:
            raw = json.dumps(value).encode()
            self.send_response(status)
            media_type = (
                "application/json"
                if self.path == "/.well-known/agent-card.json"
                else "application/a2a+json"
            )
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _authorized(self) -> bool:
            return self.headers.get("Authorization") == f"Bearer {remote.auth}"

        def _task(self, *, polling: bool = False) -> dict[str, Any]:
            task_id = remote.poll_task_id if polling else None
            task = {
                "id": task_id or "task-1",
                "status": {"state": remote.status},
            }
            if remote.status == "TASK_STATE_COMPLETED":
                task["artifacts"] = [{"parts": [{"data": remote.result}]}]
            return task

        def do_GET(self) -> None:
            if self.path == "/.well-known/agent-card.json":
                assert self.headers.get("Authorization") is None
                if remote.redirect_card:
                    self.send_response(302)
                    self.send_header("Location", "/redirected-card.json")
                    self.end_headers()
                    return
                port = self.server.server_port
                card = {
                    "name": remote.identity,
                    "supportedInterfaces": [
                        {
                            "url": remote.interface_url or f"http://127.0.0.1:{port}",
                            "protocolBinding": "HTTP+JSON",
                            "protocolVersion": remote.protocol_version,
                        }
                    ],
                    "skills": [remote.skill],
                }
                if remote.bearer_security:
                    card["securitySchemes"] = remote.security_schemes
                    card["securityRequirements"] = remote.security_requirements
                self._json(card)
                return
            if not self._authorized():
                self._json({"error": "authorization rejected"}, 401)
                return
            if self.path.startswith("/tasks/task-1"):
                remote.gets += 1
                if remote.disconnect_get_once:
                    remote.disconnect_get_once = False
                    self.connection.shutdown(2)
                    self.connection.close()
                    return
                if remote.malformed:
                    raw = b"not-json"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if remote.oversized:
                    self._json({"padding": "x" * 10_000})
                    return
                self._json(self._task(polling=True))
                return
            self._json({}, 404)

        def do_POST(self) -> None:
            if not self._authorized():
                self._json({"error": "authorization rejected"}, 401)
                return
            assert self.headers.get("A2A-Version") == "1.0"
            assert self.headers.get("Content-Type") == "application/a2a+json"
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size))
            if self.path == "/message:send":
                remote.sends += 1
                assert body["metadata"]["skillIds"] == ["review-diff"]
                self._json({"task": self._task()})
                return
            if self.path == "/tasks/task-1:cancel":
                remote.cancels += 1
                self._json(
                    {
                        "task": {
                            "id": "task-1",
                            "status": {"state": "TASK_STATE_CANCELED"},
                        }
                    }
                )
                return
            self._json({}, 404)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=2)
        httpd.server_close()


def config(base: str, *, mcp: bool = False, write: bool = False):
    return parse_agent_config(
        {
            "version": 1,
            "profiles": {
                "remote": {
                    "adapter": "a2a",
                    "model": "remote-owned",
                    "capabilities": {
                        "read": True,
                        "write": write,
                        "structured_output": True,
                        "worktree": write,
                        "resume": True,
                        "mcp": mcp,
                    },
                    "a2a": {
                        "agent_card_url": f"{base}/.well-known/agent-card.json",
                        "auth_env": "A2A_TEST_TOKEN",
                        "allowed_skills": ["review-diff"],
                        "expected_identity": "remote-review",
                    },
                }
            },
        }
    )


def request(tmp_path: Path, *, cancelled=lambda: False, changeset=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    state_path = tmp_path / "state.db"
    store = StateStore(state_path)
    if not state_path.with_suffix(".initialized").exists():
        try:
            store.create_run(
                {"id": "wf", "nodes": [{"id": "review", "required": True}]},
                "run-1",
            )
            lease = store.acquire_lease("run-1")
            store.start_attempt("run-1", "review", lease)
            store.release_lease(lease)
        except Exception as exc:
            if "UNIQUE constraint" not in str(exc):
                raise
        state_path.with_suffix(".initialized").touch()
    return AdapterRequest(
        prompt="review this bounded change",
        cwd=tmp_path,
        allowed_root=tmp_path,
        node_id="review",
        run_id="run-1",
        attempt=1,
        result_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["answer"],
            "properties": {"answer": {"type": "string"}},
        },
        changeset_schema=CHANGE_SET_SCHEMA if changeset else None,
        state_path=state_path,
        cancelled=cancelled,
    )


def execute(base: str, remote: Remote, req: AdapterRequest, *, max_bytes=4096):
    return execute_profile(
        config(base).profiles["remote"],
        req,
        environ={"A2A_TEST_TOKEN": remote.auth},
        limits=ExecutionLimits(timeout_seconds=2, max_stdout_bytes=max_bytes),
    )


def test_a2a_http_json_task_is_pinned_polled_and_normalized(tmp_path: Path):
    remote = Remote()
    remote.reset()
    remote.status = "TASK_STATE_SUBMITTED"
    with server(remote) as base:

        def complete() -> None:
            while remote.gets == 0:
                pass
            remote.status = "TASK_STATE_COMPLETED"

        threading.Thread(target=complete, daemon=True).start()
        result = execute(base, remote, request(tmp_path))
    assert result.value == {"answer": "accepted"}
    assert result.receipt.transport == "a2a"
    assert result.receipt.protocol_version == "1.0"
    assert result.receipt.remote_task_id_digest is not None
    assert remote.sends == 1 and remote.gets >= 1


def test_spec_shaped_bearer_security_requirement_is_accepted(tmp_path: Path):
    remote = Remote()
    with server(remote) as base:
        result = execute(base, remote, request(tmp_path))
    assert result.value == {"answer": "accepted"}
    assert remote.sends == 1


@pytest.mark.parametrize(
    "requirements",
    [
        [{"bearer": []}],
        [{"schemes": {"bearer": []}}],
        [
            {"schemes": {"bearer": {"list": []}}},
            {"schemes": {"bearer": {"list": []}}},
        ],
    ],
)
def test_malformed_or_ambiguous_security_requirements_fail_closed(
    tmp_path: Path, requirements: list[dict[str, Any]]
):
    remote = Remote()
    remote.security_requirements = requirements
    with server(remote) as base, pytest.raises(AdapterError) as caught:
        execute(base, remote, request(tmp_path))
    assert caught.value.code == "A2A_AUTH_SCHEME"
    assert remote.sends == 0


def test_resume_polls_persisted_task_without_duplicate_send_after_disconnect(
    tmp_path: Path,
):
    remote = Remote()
    remote.reset()
    remote.status = "TASK_STATE_WORKING"
    remote.disconnect_get_once = True
    with server(remote) as base:
        with pytest.raises(AdapterError) as caught:
            execute_profile(
                config(base).profiles["remote"],
                request(tmp_path),
                environ={"A2A_TEST_TOKEN": remote.auth},
                limits=ExecutionLimits(timeout_seconds=1, max_stdout_bytes=4096),
            )
        assert caught.value.code == "A2A_TIMEOUT"
        # intent: prove the simulated disconnect occurred before testing durable resume.
        assert remote.gets >= 1 and not remote.disconnect_get_once
        remote.status = "TASK_STATE_COMPLETED"
        result = execute(base, remote, request(tmp_path))
    assert result.value == {"answer": "accepted"}
    assert remote.sends == 1


def test_poll_rejects_task_identity_different_from_durable_binding(tmp_path: Path):
    # intent: a server must not substitute task-evil after task-good was durably bound.
    remote = Remote()
    remote.status = "TASK_STATE_WORKING"
    remote.poll_task_id = "task-evil"
    with server(remote) as base, pytest.raises(AdapterError) as caught:
        execute(base, remote, request(tmp_path))

    assert caught.value.code == "A2A_TASK_IDENTITY"
    assert remote.sends == 1
    assert remote.gets == 1
    binding = StateStore(tmp_path / "state.db").remote_task("run-1", "review")
    assert binding is not None
    assert binding["task_id"] == "task-1"


def test_agent_card_capability_drift_fails_closed_on_resume(tmp_path: Path):
    remote = Remote()
    remote.reset()
    remote.status = "TASK_STATE_WORKING"
    with server(remote) as base:
        with pytest.raises(AdapterError):
            execute_profile(
                config(base).profiles["remote"],
                request(tmp_path),
                environ={"A2A_TEST_TOKEN": remote.auth},
                limits=ExecutionLimits(timeout_seconds=0.12, max_stdout_bytes=4096),
            )
        remote.skill = {"id": "review-diff", "name": "Changed capability"}
        with pytest.raises(AdapterError) as caught:
            execute(base, remote, request(tmp_path))
    assert caught.value.code == "A2A_IDENTITY_DRIFT"
    assert remote.sends == 1


def test_identity_origin_and_protocol_drift_fail_before_dispatch(tmp_path: Path):
    remote = Remote()
    with server(remote) as base:
        remote.identity = "impersonator"
        with pytest.raises(AdapterError) as caught:
            execute(base, remote, request(tmp_path / "identity"))
        assert caught.value.code == "A2A_IDENTITY_MISMATCH"
        assert remote.sends == 0

        remote.reset()
        remote.interface_url = "https://different.example/a2a"
        with pytest.raises(AdapterError) as caught:
            execute(base, remote, request(tmp_path / "origin"))
        assert caught.value.code == "A2A_ORIGIN_MISMATCH"
        assert remote.sends == 0

        remote.reset()
        remote.protocol_version = "0.3.0"
        with pytest.raises(AdapterError) as caught:
            execute(base, remote, request(tmp_path / "legacy"))
        assert caught.value.code == "A2A_PROTOCOL"
        assert remote.sends == 0

        remote.reset()
        remote.bearer_security = False
        with pytest.raises(AdapterError) as caught:
            execute(base, remote, request(tmp_path / "auth-scheme"))
        assert caught.value.code == "A2A_AUTH_SCHEME"
        assert remote.sends == 0

        remote.reset()
        remote.redirect_card = True
        with pytest.raises(AdapterError) as caught:
            execute(base, remote, request(tmp_path / "redirect"))
        assert caught.value.code == "A2A_HTTP"
        assert remote.sends == 0


def test_oversized_message_is_rejected_before_dispatch(tmp_path: Path):
    remote = Remote()
    with server(remote) as base, pytest.raises(AdapterError) as caught:
        req = replace(request(tmp_path), prompt="x" * 10_000)
        execute(base, remote, req, max_bytes=512)
    assert caught.value.code == "A2A_REQUEST_LIMIT"
    assert remote.sends == 0


def test_late_remote_task_binding_is_fenced_by_active_attempt(tmp_path: Path):
    store = StateStore(tmp_path / "late.db")
    store.create_run(
        {"id": "wf", "nodes": [{"id": "remote", "required": True}]}, "late-run"
    )
    lease = store.acquire_lease("late-run")
    attempt = store.start_attempt("late-run", "remote", lease)
    store.finish_attempt(
        "late-run", "remote", attempt, "failed", "digest", "timed out", lease
    )

    with pytest.raises(StaleAttemptError):
        store.bind_remote_task(
            "late-run",
            "remote",
            task_id="late-task",
            attempt_number=attempt,
            profile="remote-profile",
            protocol_version="1.0",
            interface_url="https://agent.example/a2a",
            card_digest="a" * 64,
            capability_digest="b" * 64,
        )
    assert store.remote_task("late-run", "remote") is None


@pytest.mark.parametrize(
    ("mode", "code"),
    [("malformed", "A2A_MALFORMED"), ("oversized", "A2A_BODY_LIMIT")],
)
def test_malformed_and_oversized_task_responses_fail_closed(
    tmp_path: Path, mode: str, code: str
):
    remote = Remote()
    remote.reset()
    remote.status = "TASK_STATE_WORKING"
    setattr(remote, mode, True)
    with server(remote) as base, pytest.raises(AdapterError) as caught:
        execute(base, remote, request(tmp_path), max_bytes=512)
    assert caught.value.code == code


def test_auth_and_remote_output_are_redacted_on_failure(tmp_path: Path):
    remote = Remote()
    remote.reset()
    with server(remote) as base, pytest.raises(AdapterError) as caught:
        execute_profile(
            config(base).profiles["remote"],
            request(tmp_path),
            environ={"A2A_TEST_TOKEN": "wrong-secret"},
            limits=ExecutionLimits(timeout_seconds=1),
        )
    assert caught.value.code == "A2A_HTTP"
    assert "wrong-secret" not in repr(caught.value)
    assert "wrong-secret" not in repr(caught.value.receipt)


def test_remote_schema_mismatch_and_writer_without_changeset_are_rejected(
    tmp_path: Path,
):
    remote = Remote()
    remote.reset()
    remote.result = {"answer": 7}
    with server(remote) as base, pytest.raises(AdapterError) as caught:
        execute(base, remote, request(tmp_path))
    assert caught.value.code == "SCHEMA_MISMATCH"

    remote.reset()
    writer_root = tmp_path / "writer"
    writer_root.mkdir()
    with server(remote) as base, pytest.raises(AdapterError) as caught:
        execute(base, remote, request(writer_root, changeset=True))
    assert caught.value.code == "SCHEMA_MISMATCH"


def test_cancel_uses_remote_cancel_and_never_accepts_late_success(tmp_path: Path):
    remote = Remote()
    remote.reset()
    remote.status = "TASK_STATE_WORKING"
    called = False

    def cancelled() -> bool:
        nonlocal called
        if remote.sends:
            called = True
        return called

    with server(remote) as base, pytest.raises(AdapterError) as caught:
        execute(base, remote, request(tmp_path, cancelled=cancelled))
    remote.status = "TASK_STATE_COMPLETED"
    assert caught.value.code == "A2A_CANCELLED"
    assert remote.cancels == 1


def test_a2a_config_is_private_strict_and_env_reference_only():
    with pytest.raises(ConfigError) as caught:
        config("http://example.com")
    assert caught.value.code == "INSECURE_A2A_URL"

    value = {
        "version": 1,
        "profiles": {
            "remote": {
                "adapter": "a2a",
                "model": "m",
                "capabilities": {
                    name: False
                    for name in (
                        "read",
                        "write",
                        "structured_output",
                        "worktree",
                        "resume",
                        "mcp",
                    )
                },
                "a2a": {
                    "agent_card_url": "https://agent.example/.well-known/agent-card.json",
                    "auth_token": "literal-secret",
                    "allowed_skills": ["review-diff"],
                    "expected_identity": "remote-review",
                },
            }
        },
    }
    with pytest.raises(ConfigError) as caught:
        parse_agent_config(value)
    assert caught.value.code == "LITERAL_SECRET"


def test_remote_writer_changeset_is_applied_and_must_pass_local_gate(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=repo, check=True)
    subprocess.run(("git", "config", "user.name", "A2A Test"), cwd=repo, check=True)
    subprocess.run(
        ("git", "config", "user.email", "a2a@example.test"), cwd=repo, check=True
    )
    (repo / "src").mkdir()
    (repo / "src/base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(("git", "add", "--", "src/base.txt"), cwd=repo, check=True)
    subprocess.run(("git", "commit", "-qm", "base"), cwd=repo, check=True)

    manager = WorktreeManager(repo)
    producer = manager.create("fixture", "remote", base="HEAD")
    (producer.path / "src/remote.txt").write_text("remote\n", encoding="utf-8")
    changeset = change_set_value(
        manager.capture(producer, write_scope=["src/remote.txt"])
    )
    remote = Remote()
    remote.result = {"result": {"answer": "built"}, "changeset": changeset}

    workflow = {
        "version": "graph-engineering/v1alpha1",
        "id": "a2a-writer",
        "goal": "accept a remote changeset only through a local gate",
        "budgets": {
            "max_nodes": 1,
            "max_concurrency": 1,
            "max_attempts_per_node": 1,
            "max_total_attempts": 1,
            "timeout_seconds": 10,
        },
        "nodes": [
            {
                "id": "remote_writer",
                "kind": "agent",
                "task": "Implement the bounded file change.",
                "needs": [],
                "inputs": {},
                "outputs": {
                    "result": {
                        "schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["answer"],
                            "properties": {"answer": {"type": "string"}},
                        }
                    },
                    "changeset": {"schema": CHANGE_SET_SCHEMA},
                },
                "profile": "remote",
                "workspace": "worktree",
                "write_scope": ["src/remote.txt"],
                "permission": "write",
                "effect": "none",
                "checks": [
                    {
                        "id": "remote_file",
                        "argv": [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; assert Path('src/remote.txt').read_text() == 'remote\\n'",
                        ],
                    }
                ],
                "retry": {"max_attempts": 1, "no_progress_limit": 1},
                "required": True,
            }
        ],
        "outputs": {"result": "remote_writer.result"},
    }
    with server(remote) as base:
        parsed = config(base, write=True)
        result = PortableRuntime(
            workflow,
            parsed,
            repo=repo,
            state_path=tmp_path / "run.db",
            artifact_root=tmp_path / "artifacts",
            environ={"A2A_TEST_TOKEN": remote.auth},
        ).run("a2a-writer-run")
    assert result.run.status == "succeeded"
    attempt_path = result.worktrees["remote_writer#1"]
    assert (attempt_path / "src/remote.txt").read_text() == "remote\n"
