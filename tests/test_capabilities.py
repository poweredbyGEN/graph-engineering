from __future__ import annotations

import json

from graph_engineering import __version__, cli
from graph_engineering.capabilities import CAPABILITIES_VERSION
from graph_engineering.compilation import PROPOSAL_VERSION
from graph_engineering.contracts import WORKFLOW_VERSION, workflow_schema
from graph_engineering.lifecycle import CONTEXT_SCHEMA_VERSION, EVENT_SCHEMA_VERSION
from graph_engineering.project import (
    ASSESSMENT_VERSION,
    PRODUCT_CONTRACT_VERSION,
    PROJECT_VERSION,
)
from graph_engineering.session_ux import HANDOFF_VERSION, STATUS_PROJECTION_VERSION


def test_capability_manifest_matches_parser_schema_and_runtime_constants(capsys):
    # intent: feature discovery must fail in tests when runtime contracts drift.
    assert cli.main(["capabilities", "--json"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    parser = cli._parser()
    schema = workflow_schema()

    assert manifest["version"] == CAPABILITIES_VERSION
    assert manifest["package_version"] == __version__
    assert manifest["cli_commands"] == cli._parser_commands(parser)
    assert (
        manifest["runtime"]["join_policies"]
        == schema["$defs"]["join"]["properties"]["policy"]["enum"]
    )
    assert manifest["runtime"]["retry"]["max_attempts"] == {
        "minimum": schema["$defs"]["retry"]["properties"]["max_attempts"]["minimum"],
        "maximum": schema["$defs"]["retry"]["properties"]["max_attempts"]["maximum"],
    }
    assert manifest["schema_versions"] == {
        "workflow": WORKFLOW_VERSION,
        "workflow_proposal": PROPOSAL_VERSION,
        "project": PROJECT_VERSION,
        "product_contract": PRODUCT_CONTRACT_VERSION,
        "assessment": ASSESSMENT_VERSION,
        "lifecycle_event": EVENT_SCHEMA_VERSION,
        "run_context": CONTEXT_SCHEMA_VERSION,
        "handoff": HANDOFF_VERSION,
        "status_projection": STATUS_PROJECTION_VERSION,
    }
    assert manifest["features"]["worker_smoke"] is True
    assert manifest["features"]["visual_builder"] is False
    assert manifest["features"]["reviewed_workflow_compilation"] is True
    assert manifest["features"]["self_generated_workflows"] is False
    assert manifest["runtime"]["typed_profile_fallback"] is True
    assert manifest["transports"]["mcp"]["available"] is True
    assert manifest["transports"]["a2a"]["available"] is True
