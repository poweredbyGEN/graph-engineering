"""Optional MCP Tasks extension mapped onto the durable local graph task store."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mcp.server.context import HandlerResult, ServerRequestContext
from mcp.server.mcpserver import Extension, MethodBinding
from mcp.server.mcpserver.server import require_client_extension
from mcp_types import CallToolRequestParams, RequestParams
from pydantic import ConfigDict, Field

from .protocol import CURRENT_PROTOCOL, TASKS_EXTENSION, receipt
from .store import GraphTaskStore, TaskRecord


class TaskIdParams(RequestParams):
    model_config = ConfigDict(populate_by_name=True)
    task_id: str = Field(alias="taskId", min_length=36, max_length=36)


class TaskUpdateParams(TaskIdParams):
    input_responses: dict[str, Any] = Field(
        alias="inputResponses", default_factory=dict, max_length=64
    )


class GraphTasksExtension(Extension):
    """Experimental extension; core graph tools remain the compatibility path."""

    identifier = TASKS_EXTENSION

    def __init__(self, store: GraphTaskStore, manifest_sha256: str):
        self.store = store
        self.manifest_sha256 = manifest_sha256

    def settings(self) -> dict[str, Any]:
        return {
            "status": "experimental",
            "polling": True,
            "manifestSha256": self.manifest_sha256,
        }

    def methods(self) -> tuple[MethodBinding, ...]:
        versions = frozenset({CURRENT_PROTOCOL})
        return (
            MethodBinding("tasks/get", TaskIdParams, self._get, versions),
            MethodBinding("tasks/update", TaskUpdateParams, self._update, versions),
            MethodBinding("tasks/cancel", TaskIdParams, self._cancel, versions),
        )

    async def intercept_tool_call(
        self,
        params: CallToolRequestParams,
        ctx: ServerRequestContext[Any, Any],
        call_next,
    ) -> HandlerResult:
        result = await call_next(ctx)
        if (
            params.name != "graph_task_create"
            or ctx.protocol_version != CURRENT_PROTOCOL
        ):
            return result
        capabilities = ctx.session.client_capabilities
        extensions = capabilities.extensions if capabilities else None
        if not extensions or TASKS_EXTENSION not in extensions:
            return result
        structured = getattr(result, "structured_content", None)
        if not isinstance(structured, dict) or not isinstance(
            structured.get("task"), dict
        ):
            return result
        task_id = structured["task"].get("task_id")
        if not isinstance(task_id, str):
            return result
        task = self.store.inspect(task_id)
        return {"resultType": "task", **_task_shape(task, self.manifest_sha256)}

    async def _get(
        self, ctx: ServerRequestContext[Any, Any], params: TaskIdParams
    ) -> HandlerResult:
        require_client_extension(ctx, TASKS_EXTENSION)
        task = self.store.inspect(params.task_id)
        return {
            "resultType": "complete",
            **_task_shape(task, self.manifest_sha256, detailed=True),
        }

    async def _update(
        self, ctx: ServerRequestContext[Any, Any], params: TaskUpdateParams
    ) -> HandlerResult:
        require_client_extension(ctx, TASKS_EXTENSION)
        self.store.inspect(params.task_id)
        # This store never enters input_required. Per the extension, responses for
        # unknown/already-satisfied keys are ignored rather than becoming authority.
        return {"resultType": "complete"}

    async def _cancel(
        self, ctx: ServerRequestContext[Any, Any], params: TaskIdParams
    ) -> HandlerResult:
        require_client_extension(ctx, TASKS_EXTENSION)
        self.store.request_protocol_cancel(params.task_id)
        return {"resultType": "complete"}


def _task_shape(
    task: TaskRecord, manifest_sha256: str, *, detailed: bool = False
) -> dict[str, Any]:
    status = {
        "pending": "working",
        "claimed": "working",
        "completed": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
    }[task.state]
    shape: dict[str, Any] = {
        "taskId": task.task_id,
        "status": status,
        "statusMessage": f"graph node {task.workflow_id}/{task.node_id}: {task.state}",
        "createdAt": _timestamp(task.created_ms),
        "lastUpdatedAt": _timestamp(task.updated_ms),
        "ttlMs": None,
        "pollIntervalMs": 1_000,
        "_meta": {
            "com.graph-engineering/protocolSnapshot": manifest_sha256,
            "com.graph-engineering/receipt": receipt(
                manifest_sha256,
                "tasks/get" if detailed else "task",
                task.task_id,
                task.generation,
            ),
        },
    }
    if detailed and task.state == "completed":
        shape["result"] = {
            "resultType": "complete",
            "content": [{"type": "text", "text": "graph task completed"}],
            "structuredContent": task.result,
            "isError": False,
        }
    elif detailed and task.state == "failed":
        shape["error"] = {"code": -32603, "message": task.error or "graph task failed"}
    return shape


def _timestamp(milliseconds: int) -> str:
    return (
        datetime.fromtimestamp(milliseconds / 1_000, tz=UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )
