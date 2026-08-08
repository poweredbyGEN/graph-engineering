from __future__ import annotations

import asyncio
import json
import select
import subprocess
import sys
from pathlib import Path

from graph_engineering.mcp.protocol import (
    MODEL_TOOL_NAMES,
    TASKS_EXTENSION,
    ServerProfile,
)
from graph_engineering.mcp.server import create_mcp_server
from graph_engineering.mcp.skills import SkillRecord
from graph_engineering.mcp.store import GraphTaskStore


class WireClient:
    def __init__(self, database: Path, *extra_args: str):
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "graph_engineering.mcp",
                "--database",
                str(database),
                *extra_args,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def request(self, message: dict) -> dict:
        return self.raw(json.dumps(message, separators=(",", ":")))

    def raw(self, line: str) -> dict:
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()
        ready, _, _ = select.select([self.process.stdout], [], [], 10)
        assert ready, self._stderr()
        response = self.process.stdout.readline()
        assert response, self._stderr()
        return json.loads(response)

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def _stderr(self) -> str:
        if self.process.poll() is None or self.process.stderr is None:
            return "server produced no response"
        return self.process.stderr.read()


def modern_meta(*, tasks: bool = False, extra_extensions: bool = False) -> dict:
    extensions = {}
    if tasks:
        extensions[TASKS_EXTENSION] = {}
    if extra_extensions:
        extensions["com.example/future"] = {"feature": True}
    capabilities = {"extensions": extensions} if extensions else {}
    return {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "wire-test", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": capabilities,
    }


def call(message_id: int, method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "method": method, "params": params}


def test_modern_discovery_accepts_namespaced_extensions_and_rejects_unknown_version(
    tmp_path,
):
    client = WireClient(tmp_path / "tasks.db")
    try:
        discovered = client.request(
            call(1, "server/discover", {"_meta": modern_meta(extra_extensions=True)})
        )
        capabilities = discovered["result"]["capabilities"]
        assert capabilities["tools"] == {"listChanged": False}
        assert capabilities["resources"] == {"subscribe": False, "listChanged": False}
        assert TASKS_EXTENSION in capabilities["extensions"]
        assert "prompts" not in capabilities
    finally:
        client.close()

    unsupported = WireClient(tmp_path / "unsupported.db")
    try:
        meta = modern_meta() | {"io.modelcontextprotocol/protocolVersion": "2099-01-01"}
        response = unsupported.request(call(2, "tools/list", {"_meta": meta}))
        assert response["error"]["code"] == -32022
        assert response["error"]["data"]["supported"] == ["2026-07-28"]
    finally:
        unsupported.close()


def test_legacy_initialize_negotiates_and_preinit_or_unadvertised_calls_fail(tmp_path):
    preinit = WireClient(tmp_path / "preinit.db")
    try:
        rejected = preinit.request(call(1, "tools/list", {}))
        assert "error" in rejected
        assert rejected["error"]["code"] in {-32600, -32602}
    finally:
        preinit.close()

    client = WireClient(tmp_path / "legacy.db")
    try:
        initialized = client.request(
            call(
                1,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "legacy-test", "version": "1"},
                },
            )
        )
        assert initialized["result"]["protocolVersion"] == "2025-06-18"
        listed = client.request(call(2, "tools/list", {}))
        assert {tool["name"] for tool in listed["result"]["tools"]} == set(
            MODEL_TOOL_NAMES
        )
        missing = client.request(call(3, "prompts/list", {}))
        assert missing["error"]["code"] == -32601
    finally:
        client.close()

    fallback = WireClient(tmp_path / "fallback.db")
    try:
        negotiated = fallback.request(
            call(
                1,
                "initialize",
                {
                    "protocolVersion": "1.0",
                    "capabilities": {},
                    "clientInfo": {"name": "legacy-test", "version": "1"},
                },
            )
        )
        assert negotiated["result"]["protocolVersion"] == "2025-11-25"
    finally:
        fallback.close()


def test_task_extension_is_opt_in_with_plain_call_poll_fallback(tmp_path):
    database = tmp_path / "tasks.db"
    client = WireClient(database)
    try:
        fallback = client.request(
            call(
                1,
                "tools/call",
                {
                    "name": "graph_task_create",
                    "arguments": {
                        "workflow_id": "wf",
                        "node_id": "plain",
                        "payload": {},
                    },
                    "_meta": modern_meta(),
                },
            )
        )
        assert fallback["result"]["resultType"] == "complete"
        task_id = fallback["result"]["structuredContent"]["task"]["task_id"]
        polled = client.request(
            call(
                2,
                "tools/call",
                {
                    "name": "graph_task_inspect",
                    "arguments": {"task_id": task_id},
                    "_meta": modern_meta(),
                },
            )
        )
        assert polled["result"]["structuredContent"]["task"]["state"] == "pending"
        missing_capability = client.request(
            call(3, "tasks/get", {"taskId": task_id, "_meta": modern_meta()})
        )
        assert missing_capability["error"]["code"] == -32021

        asynchronous = client.request(
            call(
                4,
                "tools/call",
                {
                    "name": "graph_task_create",
                    "arguments": {
                        "workflow_id": "wf",
                        "node_id": "async",
                        "payload": {},
                    },
                    "_meta": modern_meta(tasks=True),
                },
            )
        )
        assert asynchronous["result"]["resultType"] == "task"
        assert asynchronous["result"]["status"] == "working"
        assert asynchronous["result"]["_meta"][
            "com.graph-engineering/protocolSnapshot"
        ].startswith("sha256:")
        task_status = client.request(
            call(
                5,
                "tasks/get",
                {
                    "taskId": asynchronous["result"]["taskId"],
                    "_meta": modern_meta(tasks=True),
                },
            )
        )
        assert task_status["result"]["status"] == "working"
    finally:
        client.close()

    disabled = WireClient(tmp_path / "disabled.db", "--disable-tasks-extension")
    try:
        discovered = disabled.request(
            call(1, "server/discover", {"_meta": modern_meta(tasks=True)})
        )
        assert TASKS_EXTENSION not in discovered["result"]["capabilities"].get(
            "extensions", {}
        )
        rejected = disabled.request(
            call(2, "tasks/get", {"taskId": task_id, "_meta": modern_meta(tasks=True)})
        )
        assert rejected["error"]["code"] == -32601
    finally:
        disabled.close()


def test_human_only_tools_are_absent_profiles_intersect_and_skills_stay_public(
    tmp_path,
):
    profile = ServerProfile(
        allowed_tools=frozenset({"graph_task_inspect"}), tasks_extension=True
    )
    public = SkillRecord("review", "1", "public body", "https://example.test/review")
    private = SkillRecord("private", "1", "secret host", "private", public=False)
    server = create_mcp_server(
        GraphTaskStore(tmp_path / "tasks.db"), profile=profile, skills=(private, public)
    )
    tools = asyncio.run(server.list_tools())
    assert [tool.name for tool in tools] == ["graph_task_inspect"]
    forbidden = {"force", "bypass", "admin", "approve", "checkpoint", "skip", "abort"}
    assert all(not any(word in tool.name for word in forbidden) for tool in tools)
    resources = asyncio.run(server.list_resources())
    uris = {str(resource.uri) for resource in resources}
    assert "skill://review/1" in uris
    assert "skill://private/1" not in uris
    assert server._lowlevel_server.extensions == {}


def test_malformed_and_oversize_inputs_fail_without_persisting(tmp_path):
    client = WireClient(tmp_path / "tasks.db")
    try:
        bad_meta = modern_meta()
        bad_meta["io.modelcontextprotocol/clientCapabilities"] = "not-an-object"
        malformed = client.request(call(99, "tools/list", {"_meta": bad_meta}))
        assert malformed["error"]["code"] == -32602
        oversized = client.request(
            call(
                1,
                "tools/call",
                {
                    "name": "graph_task_create",
                    "arguments": {
                        "workflow_id": "wf",
                        "node_id": "node",
                        "payload": {"blob": "x" * 70_000},
                    },
                    "_meta": modern_meta(),
                },
            )
        )
        assert oversized["result"]["isError"] is True
        assert "exceeds 65536 bytes" in oversized["result"]["content"][0]["text"]
        malformed_claim = client.request(
            call(
                2,
                "tools/call",
                {
                    "name": "graph_task_claim",
                    "arguments": {
                        "task_id": "not-a-uuid",
                        "owner": "bad owner",
                        "lease_ms": 0,
                    },
                    "_meta": modern_meta(),
                },
            )
        )
        assert malformed_claim["result"]["isError"] is True
    finally:
        client.close()
