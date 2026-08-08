"""Official-SDK MCP server exposing a least-authority graph task surface."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

from .extension import GraphTasksExtension
from .protocol import MODEL_TOOL_NAMES, ServerProfile, receipt
from .skills import SkillRecord, public_skills
from .store import GraphTaskStore, TaskStoreError


class ToolResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: dict[str, Any]
    receipt: dict[str, str]


def create_mcp_server(
    store: GraphTaskStore,
    *,
    profile: ServerProfile | None = None,
    skills: Iterable[SkillRecord] = (),
) -> MCPServer:
    """Build a server whose advertised surface is the profile intersection."""
    selected = profile or ServerProfile()
    visible_skills = public_skills(tuple(skills)) if selected.resources else ()
    manifest = selected.manifest(
        skill_uris=tuple(skill.uri for skill in visible_skills)
    )
    manifest_sha = manifest["sha256"]
    extensions = (
        [GraphTasksExtension(store, manifest_sha)]
        if selected.effective_tasks_extension
        else []
    )
    server = MCPServer(
        "graph-engineering",
        version="0.1.0a1",
        description="Durable graph task coordination with fenced worker leases.",
        instructions=(
            "Use only the advertised graph_task_* tools. Claims are exclusive and fenced; "
            "include the returned owner and generation on every write. This server grants no "
            "human approval, checkpoint, bypass, force, skip, or administrative authority."
        ),
        extensions=extensions,
    )

    def response(operation: str, task) -> ToolResponse:
        return ToolResponse(
            task=task.to_dict(),
            receipt=receipt(manifest_sha, operation, task.task_id, task.generation),
        )

    def graph_task_create(
        workflow_id: str, node_id: str, payload: dict[str, Any]
    ) -> ToolResponse:
        """Create one bounded graph node task and return its durable task ID."""
        try:
            task = store.create(workflow_id, node_id, payload)
            return response("create", task)
        except TaskStoreError as exc:
            raise ToolError(str(exc)) from exc

    def graph_task_inspect(task_id: str) -> ToolResponse:
        """Poll a graph task by its durable task ID."""
        try:
            task = store.inspect(task_id)
            return response("inspect", task)
        except TaskStoreError as exc:
            raise ToolError(str(exc)) from exc

    def graph_task_claim(
        task_id: str, owner: str, lease_ms: int = 30_000
    ) -> ToolResponse:
        """Atomically claim a pending/expired task and obtain a fencing generation."""
        try:
            claim = store.claim(task_id, owner, lease_ms)
            return response("claim", claim.task)
        except TaskStoreError as exc:
            raise ToolError(str(exc)) from exc

    def graph_task_heartbeat(
        task_id: str, owner: str, generation: int, lease_ms: int = 30_000
    ) -> ToolResponse:
        """Renew exactly one live claim; stale generations are rejected."""
        try:
            task = store.heartbeat(task_id, owner, generation, lease_ms)
            return response("heartbeat", task)
        except TaskStoreError as exc:
            raise ToolError(str(exc)) from exc

    def graph_task_complete(
        task_id: str, owner: str, generation: int, result: Any
    ) -> ToolResponse:
        """Commit a result only while the exact fenced claim remains live."""
        try:
            task = store.complete(task_id, owner, generation, result)
            return response("complete", task)
        except TaskStoreError as exc:
            raise ToolError(str(exc)) from exc

    def graph_task_fail(
        task_id: str, owner: str, generation: int, error: str
    ) -> ToolResponse:
        """Record a bounded failure only for the exact live fenced claim."""
        try:
            task = store.fail(task_id, owner, generation, error)
            return response("fail", task)
        except TaskStoreError as exc:
            raise ToolError(str(exc)) from exc

    def graph_task_cancel(task_id: str, owner: str, generation: int) -> ToolResponse:
        """Cancel only the caller's exact live claim; this is not an administrative abort."""
        try:
            task = store.cancel(task_id, owner, generation)
            return response("cancel", task)
        except TaskStoreError as exc:
            raise ToolError(str(exc)) from exc

    functions = {
        "graph_task_create": graph_task_create,
        "graph_task_inspect": graph_task_inspect,
        "graph_task_claim": graph_task_claim,
        "graph_task_heartbeat": graph_task_heartbeat,
        "graph_task_complete": graph_task_complete,
        "graph_task_fail": graph_task_fail,
        "graph_task_cancel": graph_task_cancel,
    }
    assert set(functions) == set(MODEL_TOOL_NAMES)
    for name in sorted(selected.allowed_tools):
        server.add_tool(functions[name], name=name, structured_output=True)

    if selected.resources:

        @server.resource(
            "graph-engineering://capabilities/manifest",
            name="capability-manifest",
            title="Effective graph-engineering capability manifest",
            description="Public, per-profile capability intersection and protocol snapshot hash.",
            mime_type="application/json",
            meta={"com.graph-engineering/digest": manifest_sha},
        )
        def capability_manifest() -> str:
            return json.dumps(manifest, sort_keys=True, separators=(",", ":"))

        for skill in visible_skills:
            descriptor = skill.descriptor()

            def make_reader(body: str):
                def read_skill() -> str:
                    return body

                return read_skill

            server.resource(
                skill.uri,
                name=skill.name,
                title=descriptor["title"],
                description=descriptor["description"],
                mime_type="text/markdown",
                meta=descriptor["_meta"],
            )(make_reader(skill.body))

    # MCPServer deliberately installs the whole convenience surface. This adapter is a
    # least-authority server, so remove convenience handlers it does not advertise or
    # implement. The dual-era lifecycle/serialization remains owned by the official SDK.
    handlers = server._lowlevel_server._request_handlers  # type: ignore[attr-defined]
    for method in (
        "completion/complete",
        "logging/setLevel",
        "prompts/get",
        "prompts/list",
        "resources/subscribe",
        "resources/templates/list",
        "resources/unsubscribe",
        "subscriptions/listen",
    ):
        handlers.pop(method, None)
    if not selected.resources:
        handlers.pop("resources/list", None)
        handlers.pop("resources/read", None)
    if not selected.allowed_tools:
        handlers.pop("tools/list", None)
        handlers.pop("tools/call", None)

    return server
