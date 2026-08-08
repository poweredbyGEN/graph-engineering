"""Protocol constants, bounded profiles, and auditable capability receipts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

CURRENT_PROTOCOL = "2026-07-28"
LEGACY_PROTOCOL = "2025-06-18"
SUPPORTED_PROTOCOLS = (
    "2024-11-05",
    "2025-03-26",
    LEGACY_PROTOCOL,
    "2025-11-25",
    CURRENT_PROTOCOL,
)
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
MAX_MESSAGE_BYTES = 512 * 1024
MAX_TOOL_NAME = 128

MODEL_TOOL_NAMES = (
    "graph_task_cancel",
    "graph_task_claim",
    "graph_task_complete",
    "graph_task_create",
    "graph_task_fail",
    "graph_task_heartbeat",
    "graph_task_inspect",
)


@dataclass(frozen=True)
class ServerProfile:
    """A public capability intersection, never a provider or credential registry."""

    allowed_tools: frozenset[str] = frozenset(MODEL_TOOL_NAMES)
    resources: bool = True
    tasks_extension: bool = True

    def __post_init__(self) -> None:
        unknown = self.allowed_tools.difference(MODEL_TOOL_NAMES)
        if unknown:
            raise ValueError(f"profile contains unknown tools: {sorted(unknown)}")
        if len(self.allowed_tools) > 64:
            raise ValueError("profile has too many tools")

    @property
    def effective_tasks_extension(self) -> bool:
        needed = {"graph_task_create", "graph_task_inspect"}
        return self.tasks_extension and needed.issubset(self.allowed_tools)

    def manifest(self, *, skill_uris: tuple[str, ...] = ()) -> dict[str, Any]:
        manifest = {
            "protocols": list(SUPPORTED_PROTOCOLS),
            "tools": sorted(self.allowed_tools),
            "resources": self.resources,
            "tasksExtension": self.effective_tasks_extension,
            "skillUris": sorted(skill_uris) if self.resources else [],
            "humanOnlyOperationsExposed": False,
        }
        manifest["sha256"] = canonical_sha256(manifest)
        return manifest


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def receipt(
    manifest_sha256: str, operation: str, task_id: str, generation: int
) -> dict[str, str]:
    material = {
        "generation": generation,
        "manifestSha256": manifest_sha256,
        "operation": operation,
        "taskId": task_id,
    }
    return {
        "manifestSha256": manifest_sha256,
        "receiptSha256": canonical_sha256(material),
    }
