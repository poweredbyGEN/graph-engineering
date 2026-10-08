"""Deterministic release comparisons; HTTP fixtures are observations, not MCP stubs."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "readiness", Path(__file__).parents[1] / "ops/release_readiness.py"
)
MODULE = importlib.util.module_from_spec(SPEC)


def load():
    SPEC.loader.exec_module(MODULE)
    return MODULE


def healthy():
    unit = {
        "active": True,
        "sha": "a" * 40,
        "import_errors": False,
        "environment": {
            "REMOTION_RENDER_URL": "https://renderer.example",
            "REMOTION_RENDER_TOKEN": True,
            "GEN_ENVIRONMENT": "production",
            "POSTGRESQL_USERNAME": True,
            "POSTGRESQL_PASSWORD": True,
            "POSTGRESQL_HOSTNAME": True,
            "POSTGRESQL_DATABASE": True,
            "POSTGRESQL_PORT": True,
            "WAREHOUSE_DB_PRESENT": True,
            "AGENT_CORE_STAGING_URL": "https://funding-staging.example",
        },
    }
    prod = {
        k: copy.deepcopy(unit)
        for k in ("rails", "mcp", "agentic", "dw", "gensembledata")
    }
    stage = copy.deepcopy(prod)
    stage["mcp"]["environment"]["GEN_ENVIRONMENT"] = "staging"
    surface = {
        "cards": [{"card_type": "image", "job_types": ["image_generation"]}],
        "planner_types": ["image_generation"],
        "documented_views": ["card_types"],
        "served_views": ["card_types"],
        "idea_ids": ["5f1ab15b-2b61-4c68-8ca0-8e8ac0f2196b"],
        "versions": {
            k: {"job": 1} for k in ("mcp", "agentic", "rails", "dw", "gensembledata")
        },
    }
    return {
        "production": prod,
        "staging": stage,
        "deployd": {k: {"last_deployed_sha": "a" * 40} for k in prod},
        "surfaces": {
            "production": copy.deepcopy(surface),
            "staging": copy.deepcopy(surface),
        },
        "qa": {
            "signin": True,
            "can_create_agent": True,
            "credits": 10,
            "role": "Owner",
        },
        "collect": {
            "count": 30,
            "cap": 20,
            "code": "ENRICHMENT_LIMIT_EXCEEDED",
            "refusal_codes": ["ENRICHMENT_LIMIT_EXCEEDED"],
            "dry_run": True,
        },
    }


def failures(data):
    return [r["check"] for r in load().compare(data) if not r["ok"]]


def test_complete_observation_passes():
    # intent: complete, compatible live observations satisfy every required category.
    assert failures(healthy()) == []


def test_renderer_loopback_and_missing_token_fail():
    # intent: loopback renderer and absent credentials never pass staging readiness.
    x = healthy()
    x["staging"]["rails"]["environment"]["REMOTION_RENDER_URL"] = (
        "http://127.0.0.1:8500"
    )
    x["staging"]["rails"]["environment"]["REMOTION_RENDER_TOKEN"] = False
    assert "renderer" in failures(x)


def test_process_sha_overrides_green_deployd():
    # intent: deployd success cannot hide a stale running release unit.
    x = healthy()
    x["staging"]["mcp"]["sha"] = "b" * 40
    assert "release SHAs" in failures(x)


def test_missing_unit_inactive_worker_or_import_failure_fail():
    for mutation in ("missing", "inactive", "import"):
        x = healthy()
        if mutation == "missing":
            del x["staging"]["gensembledata"]
        elif mutation == "inactive":
            x["staging"]["agentic"]["active"] = False
        else:
            x["staging"]["mcp"]["import_errors"] = True
        assert failures(x)


def test_funding_and_db_presence_fail_closed():
    x = healthy()
    x["staging"]["mcp"]["environment"]["GEN_ENVIRONMENT"] = "production"
    x["staging"]["dw"]["environment"]["AGENT_CORE_STAGING_URL"] = (
        "https://funding.example"
    )
    x["staging"]["dw"]["environment"]["POSTGRESQL_PASSWORD"] = False
    assert {"funding", "warehouse DB presence"} <= set(failures(x))


def test_contract_drift_uuid_and_views_fail():
    x = healthy()
    s = x["surfaces"]["staging"]
    s["planner_types"].append("unmapped")
    s["idea_ids"] = ["1"]
    s["documented_views"].append("help")
    s["versions"]["agentic"]["job"] = 2
    assert {
        "card mapping",
        "saved idea UUID",
        "discover views",
        "contract versions",
    } <= set(failures(x))


def test_unknown_and_empty_evidence_are_failures():
    x = healthy()
    x["surfaces"]["staging"] = {}
    x["qa"] = {}
    x["collect"] = {}
    assert {
        "card mapping",
        "saved idea UUID",
        "discover views",
        "QA account",
        "GEN-8926",
    } <= set(failures(x))


def test_optional_versions_absent_pass_only_when_absent_everywhere():
    x = healthy()
    for s in x["surfaces"].values():
        s["versions"] = {}
    assert "contract versions" not in failures(x)
    x["surfaces"]["staging"]["versions"] = {"mcp": {"job": 1}}
    assert "contract versions" in failures(x)


def test_busy_payment_and_unsafe_collect_never_pass():
    for code in ("BUSY", "PAYMENT_REQUIRED", "unexpected"):
        x = healthy()
        x["collect"]["code"] = code
        assert "GEN-8926" in failures(x)
    x = healthy()
    x["collect"]["dry_run"] = False
    assert "GEN-8926" in failures(x)


def test_fake_http_and_secret_redaction():
    # intent: external HTTP failures expose neither response bodies nor credentials.
    m = load()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, *args):
            return json.dumps({**healthy(), "token": "secret-value"}).encode()

    with patch.object(m.urllib.request, "urlopen", return_value=Response()):
        assert all(
            row["ok"]
            for row in m.compare(m.get_json("https://qa.example", "secret-value"))
        )
    assert "secret-value" not in json.dumps(m.compare(healthy()))


def test_nonfinite_credits_and_untyped_refusal_fail():
    for value in (float("inf"), float("nan"), True, "10", 0):
        x = healthy()
        x["qa"]["credits"] = value
        assert "QA account" in failures(x)
    x = healthy()
    x["collect"]["code"] = {"code": "ENRICHMENT_LIMIT_EXCEEDED"}
    assert "GEN-8926" in failures(x)


def test_unobserved_ideas_are_not_reported_as_invalid_uuids():
    # intent: no scoped read cannot establish that saved identifiers are malformed.
    x = healthy()
    x["surfaces"]["staging"].pop("idea_ids")
    x["surfaces"]["staging"]["idea_observation"] = "not requested: missing agent_id"
    row = next(r for r in load().compare(x) if r["check"] == "saved idea UUID")
    assert not row["ok"]
    assert "staging: not requested: missing agent_id" in row["detail"]
    x["surfaces"]["staging"]["idea_ids"] = ["1"]
    row = next(r for r in load().compare(x) if r["check"] == "saved idea UUID")
    assert "staging: 1 invalid identifier" in row["detail"]


def test_account_and_admission_report_each_failed_subcheck():
    # intent: missing or refused reads expose subchecks without HTTP bodies or credentials.
    x = healthy()
    x["qa"] = {"observation": "HTTP 403"}
    x["collect"] = {"observation": "not requested: missing validation-only endpoint"}
    rows = {r["check"]: r for r in load().compare(x)}
    assert "HTTP 403" in rows["QA account"]["detail"]
    for field in (
        "signin=unobserved",
        "owner=unobserved",
        "create_permission=unobserved",
        "credits=unobserved",
    ):
        assert field in rows["QA account"]["detail"]
    for field in (
        "dry_run=unobserved",
        "above_cap=unobserved",
        "typed_refusal=unobserved",
    ):
        assert field in rows["GEN-8926"]["detail"]


def test_missing_private_configuration_is_not_an_observation_exception():
    # intent: absent QA/validation settings fail readiness without a probe KeyError.
    m = load()
    with patch.object(m, "remote_status", return_value={}):
        data = m.collect({"deployd": {}, "mcp": {}})
    assert not any(e.startswith(("qa:", "collect:")) for e in data["errors"])
    assert (
        data["qa"]["observation"] == "not requested: missing QA account configuration"
    )
    assert (
        data["collect"]["observation"]
        == "not requested: missing validation-only endpoint"
    )


def test_http_refusal_preserves_status_without_body():
    # intent: an account's actual refusal is distinguishable from missing configuration.
    m = load()
    config = {"deployd": {}, "mcp": {}, "qa": {"read_url": "https://qa.example"}}
    error = m.urllib.error.HTTPError(
        "https://qa.example", 403, "secret-value", {}, None
    )
    with (
        patch.object(m, "remote_status", return_value={}),
        patch.object(m.urllib.request, "urlopen", side_effect=error),
    ):
        data = m.collect(config)
    assert data["qa"] == {
        "observation": "HTTP 403: read refused; body withheld",
        "signin": False,
    }
    assert "secret-value" not in json.dumps(data)


def test_rails_revision_is_read_from_live_process_cwd(tmp_path, monkeypatch, capsys):
    # intent: an inherited deploy stamp cannot override the live Rails release REVISION.
    m = load()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEPLOY_SHA", "b" * 40)
    (tmp_path / "REVISION").write_text("a" * 40)
    unit = {
        "label": "rails",
        "name": "passenger.service",
        "deploy_target": "gbv2-staging",
    }

    def platform_read(args, **kwargs):
        output = (
            f"MainPID={os.getpid()}\nActiveState=active\nControlGroup=/absent-readiness-test\n"
            if args[0] == "systemctl"
            else ""
        )
        return subprocess.CompletedProcess(args, 0, output, "")

    with (
        patch.object(m.subprocess, "run", side_effect=platform_read),
        patch.object(sys, "argv", ["probe", json.dumps([unit])]),
    ):
        exec(m.REMOTE, {})
    row = json.loads(capsys.readouterr().out)["rails"]
    assert row["sha"] == "a" * 40
    assert row["process_revisions"] == [
        {"pid": str(os.getpid()), "cwd": str(tmp_path), "revision": "a" * 40}
    ]
