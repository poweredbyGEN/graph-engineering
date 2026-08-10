"""Authoritative, machine-readable runtime capability manifest."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from . import __version__
from .compilation import PROPOSAL_VERSION
from .config import CAPABILITY_NAMES
from .contracts import WORKFLOW_VERSION, workflow_schema
from .forking import FORK_VERSION
from .learning import BENCHMARK_VERSION, LEARNING_PROPOSAL_VERSION
from .lifecycle import (
    CONTEXT_SCHEMA_VERSION,
    EVENT_SCHEMA_VERSION,
    EVENT_STREAM_VERSION,
)
from .project import ASSESSMENT_VERSION, PRODUCT_CONTRACT_VERSION, PROJECT_VERSION
from .session_ux import HANDOFF_VERSION, STATUS_PROJECTION_VERSION
from .usage import USAGE_VERSION
from .watch import WATCH_VERSION

CAPABILITIES_VERSION = "graph-engineering/capabilities/v1"
FRONTEND_EVIDENCE_PATTERN = (
    "probe-real-journey",
    "execute-isolated",
    "assert-deterministically",
    "capture-bounded-evidence",
)


def capability_manifest(cli_commands: Sequence[str]) -> dict[str, Any]:
    """Build claims from packaged schemas and runtime constants, never documentation."""

    schema = workflow_schema()
    join = schema["$defs"]["join"]["properties"]["policy"]["enum"]
    retry = schema["$defs"]["retry"]["properties"]
    return {
        "version": CAPABILITIES_VERSION,
        "package_version": __version__,
        "schema_versions": {
            "workflow": WORKFLOW_VERSION,
            "workflow_proposal": PROPOSAL_VERSION,
            "project": PROJECT_VERSION,
            "product_contract": PRODUCT_CONTRACT_VERSION,
            "assessment": ASSESSMENT_VERSION,
            "lifecycle_event": EVENT_SCHEMA_VERSION,
            "run_context": CONTEXT_SCHEMA_VERSION,
            "handoff": HANDOFF_VERSION,
            "status_projection": STATUS_PROJECTION_VERSION,
            "benchmark": BENCHMARK_VERSION,
            "learning_proposal": LEARNING_PROPOSAL_VERSION,
            "run_fork": FORK_VERSION,
            "run_watch": WATCH_VERSION,
            "usage_stats": USAGE_VERSION,
            "event_stream": EVENT_STREAM_VERSION,
        },
        "cli_commands": sorted(set(cli_commands)),
        "runtime": {
            "join_policies": list(join),
            "retry": {
                "max_attempts": {
                    "minimum": retry["max_attempts"]["minimum"],
                    "maximum": retry["max_attempts"]["maximum"],
                },
                "no_progress_limit": {
                    "minimum": retry["no_progress_limit"]["minimum"],
                    "maximum": retry["no_progress_limit"]["maximum"],
                },
            },
            "ready_queue": True,
            "attempt_fencing": True,
            "durable_resume": True,
            "typed_repair_routes": True,
            "typed_profile_fallback": True,
        },
        "execution": {
            "profile_capabilities": sorted(CAPABILITY_NAMES),
            "schema_validated_artifacts": True,
            "digest_bound_receipts": True,
            "isolated_worktrees": True,
            "single_integration_owner": True,
        },
        "adapters": {
            "subprocess": "available",
            "a2a": "available",
            "openai_compatible": "not_implemented",
        },
        "transports": {
            "mcp": {"available": True, "role": "tools_and_durable_tasks"},
            "a2a": {"available": True, "role": "remote_agent_delegation"},
        },
        "frontend_evidence": {
            "available": True,
            "pattern": list(FRONTEND_EVIDENCE_PATTERN),
        },
        "features": {
            "capability_manifest": True,
            "worker_smoke": True,
            "visual_builder": False,
            "self_generated_workflows": False,
            "reviewed_workflow_compilation": True,
            "outcome_benchmarking": True,
            "reviewed_feedback_learning": True,
            "immutable_run_forks": True,
            "bounded_event_stream": True,
            "live_run_watch": True,
            "usage_telemetry": True,
        },
    }


__all__ = [
    "CAPABILITIES_VERSION",
    "FRONTEND_EVIDENCE_PATTERN",
    "capability_manifest",
]
