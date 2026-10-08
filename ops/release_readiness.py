#!/usr/bin/env python3
"""Read-only release evidence collection. Missing evidence is a failed check."""

import argparse
import datetime
import importlib.util
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid

CORE = {"rails", "mcp", "agentic", "dw", "gensembledata"}
DB_FIELDS = (
    "POSTGRESQL_USERNAME",
    "POSTGRESQL_PASSWORD",
    "POSTGRESQL_HOSTNAME",
    "POSTGRESQL_DATABASE",
    "POSTGRESQL_PORT",
)
SHA = re.compile(r"^[0-9a-f]{40}$")


def routable(value):
    try:
        p = urlsplit(value)
        host = p.hostname or ""
        if p.scheme not in {"http", "https"} or not host or host.lower() == "localhost":
            return False
        try:
            addr = ipaddress.ip_address(host)
            return not (addr.is_loopback or addr.is_unspecified)
        except ValueError:
            return True
    except ValueError:
        return False


def compare(data):
    rows = []

    def check(name, ok, detail):
        rows.append({"check": name, "ok": bool(ok), "detail": detail})

    prod, stage = (data.get(k, {}) for k in ("production", "staging"))

    def env(units, key):
        return units.get(key, {}).get("environment", {})

    renderer = [env(units, "rails") for units in (prod, stage)]
    check(
        "renderer",
        all(
            routable(e.get("REMOTION_RENDER_URL", ""))
            and e.get("REMOTION_RENDER_TOKEN") is True
            for e in renderer
        )
        and renderer[0].get("REMOTION_RENDER_URL")
        == renderer[1].get("REMOTION_RENDER_URL"),
        "Reachable-address renderer URL parity and token presence",
    )
    funding = env(stage, "dw").get("AGENT_CORE_STAGING_URL", "")
    check(
        "funding",
        env(stage, "mcp").get("GEN_ENVIRONMENT") == "staging"
        and env(prod, "mcp").get("GEN_ENVIRONMENT") in {None, "", "production"}
        and routable(funding)
        and "staging" in (urlsplit(funding).hostname or ""),
        "Staging MCP selection and explicit staging DW funding URL",
    )
    check(
        "warehouse DB presence",
        all(
            all(env(u, "dw").get(k) is True for k in DB_FIELDS)
            and env(u, "rails").get("WAREHOUSE_DB_PRESENT") is True
            for u in (prod, stage)
        ),
        "DW connection fields and Rails warehouse-role configuration present",
    )
    deployd = data.get("deployd", {})
    release_ok = (
        CORE <= prod.keys() and CORE <= stage.keys() and prod.keys() == stage.keys()
    )
    smoke_ok = release_ok
    sha_failures = []
    smoke_failures = []
    for label, units in (("production", prod), ("staging", stage)):
        for key, unit in units.items():
            sha = unit.get("sha", "")
            expected = deployd.get(unit.get("deploy_target", key), {}).get(
                "last_deployed_sha"
            )
            matched = bool(
                SHA.fullmatch(sha)
                and sha == expected
                and sha
                == (stage if label == "production" else prod).get(key, {}).get("sha")
            )
            release_ok &= matched
            if not matched:
                sha_failures.append(
                    f"{label}/{key}: running={sha[:12] or 'unknown'}, deployd={(expected or 'unknown')[:12]}"
                )
            clean = unit.get("active") is True and unit.get("import_errors") is False
            smoke_ok &= clean
            if not clean:
                smoke_failures.append(f"{label}/{key}")
    check(
        "release SHAs",
        release_ok,
        "; ".join(sha_failures)
        or "Every required unit matches deployd and its counterpart",
    )
    check(
        "deployed-set smoke",
        smoke_ok,
        ", ".join(smoke_failures)
        or "All required units active; bounded journals have no import errors",
    )
    surfaces = [data.get("surfaces", {}).get(k, {}) for k in ("production", "staging")]
    card_ok = uuid_ok = view_ok = True
    uuid_details = []
    for label, surface in zip(("production", "staging"), surfaces):
        cards, planner = surface.get("cards", []), surface.get("planner_types", [])
        mapped = {c.get("card_type") for c in cards if c.get("job_types")}
        mapped |= {j for c in cards for j in c.get("job_types", [])}
        card_ok &= bool(cards and planner and set(planner) <= mapped)
        ids = surface.get("idea_ids", [])
        valid = bool(ids)
        invalid = 0
        for idea_id in ids:
            try:
                invalid += str(uuid.UUID(str(idea_id))) != str(idea_id).lower()
            except (ValueError, TypeError, AttributeError):
                invalid += 1
        valid &= not invalid
        uuid_ok &= valid
        uuid_details.append(
            f"{label}: {invalid} invalid identifier(s) in {len(ids)} saved ideas"
            if invalid
            else f"{label}: {len(ids)} saved UUID(s)"
            if ids
            else f"{label}: "
            + surface.get("idea_observation", "no saved ideas observed")
        )
        docs, served = (
            surface.get("documented_views", []),
            surface.get("served_views", []),
        )
        view_ok &= bool(docs and served and set(docs) <= set(served))
    check(
        "card mapping",
        card_ok,
        "Every deployed planner type has a served generation mapping",
    )
    check(
        "saved idea UUID",
        uuid_ok,
        "; ".join(uuid_details),
    )
    check(
        "discover views",
        view_ok,
        "Documented discover views are a subset of the served enum",
    )
    versions = [s.get("versions", {}) for s in surfaces]
    advertised = [v for group in versions for v in group.values()]
    versions_observed = all("versions" in surface for surface in surfaces)
    version_ok = versions_observed and (
        not advertised
        or (
            all(
                set(g) >= {"mcp", "agentic", "rails", "dw", "gensembledata"}
                for g in versions
            )
            and all(v and v == advertised[0] for v in advertised)
        )
    )
    check(
        "contract versions",
        version_ok,
        "Matching advertised contracts on every client"
        if advertised
        else "GEN-9072 contracts not advertised; optional until present"
        if versions_observed
        else "Contract advertisement could not be observed",
    )
    qa = data.get("qa", {})
    credits = qa.get("credits")
    qa_checks = {
        "signin": qa.get("signin") is True,
        "owner": qa.get("role") == "Owner",
        "create_permission": qa.get("can_create_agent") is True,
        "credits": isinstance(credits, (int, float))
        and not isinstance(credits, bool)
        and math.isfinite(credits)
        and credits > 0,
    }
    qa_fields = {
        "signin": "signin",
        "owner": "role",
        "create_permission": "can_create_agent",
        "credits": "credits",
    }
    check(
        "QA account",
        all(qa_checks.values()),
        qa.get("observation", "HTTP account read succeeded")
        + "; "
        + ", ".join(
            f"{name}={'PASS' if ok else 'FAIL' if qa_fields[name] in qa else 'unobserved'}"
            for name, ok in qa_checks.items()
        ),
    )
    collect = data.get("collect", {})
    code = collect.get("code", "")
    code = code if isinstance(code, str) else ""
    # Only an independently documented validation-only surface is safe: live
    # collect can bill before refusing. Missing validation fails closed (GEN-8926).
    collect_checks = {
        "dry_run": collect.get("dry_run") is True,
        "above_cap": isinstance(collect.get("count"), int)
        and isinstance(collect.get("cap"), int)
        and not isinstance(collect["count"], bool)
        and not isinstance(collect["cap"], bool)
        and collect["count"] > collect["cap"] > 0,
        "typed_refusal": code in collect.get("refusal_codes", [])
        and not any(
            word in code.upper() for word in ("PAYMENT", "FUNDING", "BUSY", "CREDIT")
        ),
    }
    collect_fields = {
        "dry_run": "dry_run",
        "above_cap": "count",
        "typed_refusal": "code",
    }
    check(
        "GEN-8926",
        all(collect_checks.values()),
        collect.get("observation", "HTTP validation read succeeded")
        + "; "
        + ", ".join(
            f"{name}={'PASS' if ok else 'FAIL' if collect_fields[name] in collect else 'unobserved'}"
            for name, ok in collect_checks.items()
        ),
    )
    return rows


def get_json(url, token=None):
    headers = {"User-Agent": "gen-release-readiness/1", "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=15
    ) as response:
        return json.loads(response.read(2_000_000))


# Remote code reads only /proc, git metadata, bounded journals and container
# inspection. It never imports application code or prints environment values.
REMOTE = r"""
import json,os,re,subprocess,sys
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
cfg=json.loads(sys.argv[1])
def run(args):
 p=subprocess.run(args,capture_output=True,text=True,timeout=15)
 return p.stdout.strip() if p.returncode==0 else ''
def project(e):
 out={}
 for k in ('GEN_ENVIRONMENT','REMOTION_RENDER_URL','AGENT_CORE_STAGING_URL'):
  if k in e:
   p=urlsplit(e[k])
   out[k]=urlunsplit((p.scheme,p.netloc.split('@')[-1],p.path,'','')) if k!='GEN_ENVIRONMENT' else e[k]
 for k in ('REMOTION_RENDER_TOKEN','POSTGRESQL_USERNAME','POSTGRESQL_PASSWORD','POSTGRESQL_HOSTNAME','POSTGRESQL_DATABASE','POSTGRESQL_PORT'):
  out[k]=bool(e.get(k))
 out['WAREHOUSE_DB_PRESENT']=bool(e.get('WAREHOUSE_DB_PRESENT') or e.get('WAREHOUSE_DATABASE_URL') or all(e.get(k) for k in ('WAREHOUSE_DB_HOST','WAREHOUSE_DB_USER','WAREHOUSE_DB_PASSWORD')))
 return out
out={}
for unit in cfg:
 try:
  name=unit['name'];e={};cwd='';sha='';revisions=[]
  if unit.get('kind')=='docker':
   info=json.loads(run(['docker','inspect',name]))[0]
   e=dict(x.split('=',1) for x in info['Config'].get('Env',[]) if '=' in x)
   active=info['State']['Running']
   sha=e.get('DEPLOY_SHA') or run(['docker','exec',name,'git','rev-parse','HEAD'])
   logs_p=subprocess.run(['docker','logs','--since','15m','--tail','100',name],capture_output=True,text=True,timeout=15)
   logs=logs_p.stdout+logs_p.stderr if logs_p.returncode==0 else None
   if not e.get('AGENT_CORE_STAGING_URL'):
    source=run(['docker','exec',name,'cat','app/execution_funding.py'])
    match=re.search(r'"AGENT_CORE_STAGING_URL",\s*"([^"]+)"',source)
    if match:e['AGENT_CORE_STAGING_URL']=match.group(1)
  else:
   props=dict(x.split('=',1) for x in run(['systemctl','show',name,'-p','MainPID','-p','ActiveState','-p','ControlGroup']).splitlines() if '=' in x)
   active=props.get('ActiveState')=='active' and int(props.get('MainPID','0'))>0
   pid=props.get('MainPID','0')
   pids=[pid]
   cg=Path('/sys/fs/cgroup'+props.get('ControlGroup','')+'/cgroup.procs')
   if cg.is_file(): pids+=cg.read_text().split()
   for p in dict.fromkeys(pids):
    try:
     pe=dict(x.split('=',1) for x in Path('/proc/'+p+'/environ').read_text().split('\0') if '=' in x)
     pc=os.readlink('/proc/'+p+'/cwd')
     # Rails revision files belong to live process cwds, never a checkout or deploy dashboard.
     revision=Path(pc)/'REVISION'
     if unit['label']=='rails' and revision.is_file():
      revisions.append({'pid':p,'cwd':pc,'revision':revision.read_text().strip()})
     if p==pid or 'REMOTION_RENDER_URL' in pe:
      e=pe;cwd=pc
    except OSError: pass
   stamps=[Path(cwd)/'DEPLOY_SHA',Path(cwd)/'REVISION'] if cwd else []
   cmd=Path('/proc/'+pid+'/cmdline').read_text().split('\0') if active else []
   stamps.extend(Path(x).resolve().parent.parent/'DEPLOY_SHA' for x in cmd if x.startswith('/') and '/bin/' in x)
   sha=e.get('DEPLOY_SHA') or next((f.read_text().strip() for f in stamps if f.is_file()),'') or (run(['git','-C',cwd,'rev-parse','HEAD']) if cwd else '')
   if unit['label']=='rails':
    observed={p['revision'] for p in revisions}
    sha=next(iter(observed)) if len(observed)==1 else 'mixed process revisions' if observed else ''
   if not sha and cwd:
    match=re.search(r'/([0-9a-f]{40})(?:/|$)',cwd)
    if match:sha=match.group(1)
   db=Path(cwd)/'config/database.yml' if cwd else None
   if db and db.is_file():
    block=db.read_text().split('data_warehouse:',1)[-1]
    fields=all(re.search(r'^\s+'+k+r':\s*\S',block,re.M) for k in ('host','database','username','password'))
    e['WAREHOUSE_DB_PRESENT']=fields and bool(e.get('GEN_DB_WAREHOUSE_PASSWORD'))
   logs_p=subprocess.run(['journalctl','-u',name,'--since','15 minutes ago','-n','100','--no-pager','-o','cat'],capture_output=True,text=True,timeout=15)
   logs=logs_p.stdout if logs_p.returncode==0 else None
  item={'active':active,'sha':sha,'import_errors':bool(re.search(r'ImportError|ModuleNotFoundError|cannot import name|LoadError',logs)) if logs is not None else None, 'environment':project(e),'deploy_target':unit['deploy_target']}
  if unit['label']=='rails': item['process_revisions']=revisions
  if unit['label']=='agentic' and cwd:
   # The planner's deployed card contract is the input; MCP owns generation mappings (GEN-9073).
   options=Path(cwd)/'src/gen/contracts/creation-card-model-options.json'
   item['planner_types']=sorted(json.loads(options.read_text())['modelOptions'])
  out[unit['label']]=item
 except Exception as exc:
  out[unit['label']]={'active':False,'sha':'','import_errors':True,'environment':{},'deploy_target':unit['deploy_target'],'error':type(exc).__name__}
print(json.dumps(out))
"""


def remote(host, units):
    command = "python3 -c " + shlex.quote(REMOTE) + " " + shlex.quote(json.dumps(units))
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, command],
        capture_output=True,
        text=True,
        timeout=max(30, len(units) * 35),
    )
    if proc.returncode:
        raise RuntimeError("SSH observation failed")
    return json.loads(proc.stdout)


def load_transport(source):
    # Reuse the hosted MCP's existing stdlib handshake and SSE parser rather
    # than creating a second MCP transport in the release harness.
    spec = importlib.util.spec_from_file_location(
        "hosted_verify", Path(source) / "scripts/verify_hosted_tools.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def served_surface(config, units, transport):
    url, token = (
        config["url"],
        os.environ.get(config.get("token_env", ""), "deploy-probe"),
    )
    session = None

    def rpc(method, params):
        nonlocal session
        headers, body = transport._rpc(
            url,
            token,
            session,
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            15,
        )
        session = headers.get("mcp-session-id", session)
        return transport._result(body, method)

    info = rpc(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "release-readiness", "version": "1"},
        },
    )
    transport._rpc(
        url,
        token,
        session,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        15,
    )
    tools = rpc("tools/list", {}).get("tools", [])
    discover = next(t for t in tools if t.get("name") == "gen_discover")
    schema = discover["inputSchema"]["properties"]["view"]
    views = set(schema.get("enum", []))
    for variant in schema.get("anyOf", []):
        views.update(variant.get("enum", []))

    def read(domain, view):
        result = rpc(
            "tools/call",
            {
                "name": "gen_discover",
                "arguments": {
                    "domain": domain,
                    "view": view,
                    **(
                        {"agent_id": config["agent_id"]}
                        if config.get("agent_id")
                        else {}
                    ),
                },
            },
        )
        if result.get("isError"):
            raise RuntimeError("Discovery refused")
        payload = result.get("structuredContent", {})
        if isinstance(payload.get("result"), str):
            return json.loads(payload["result"])
        if payload:
            return payload
        for item in result.get("content", []):
            if item.get("type") == "text":
                return json.loads(item["text"])
        return payload

    surface = {
        "served_views": sorted(views),
        "documented_views": config.get("documented_views", []),
        "planner_types": units.get("agentic", {}).get("planner_types", []),
        "versions": {},
    }
    surface["version_sha"] = transport.deployed_sha(info["serverInfo"]["version"])
    try:
        surface["cards"] = read("vidsheet", "card_types").get("cards", [])
        for card in surface["cards"]:
            card["job_types"] = card.get("job_types") or (
                [card["job_type"]] if card.get("job_type") else []
            )
    except Exception:
        surface["cards"] = []
    surface["idea_observation"] = "not requested: missing agent_id"
    if config.get("agent_id") and not os.environ.get(config.get("token_env", "")):
        surface["idea_observation"] = "not requested: missing scoped bearer token"
    elif config.get("agent_id"):
        try:
            ideas = read("content", "ideas")
            saved = ideas.get("ideas", [])
            if isinstance(saved, dict):
                saved = saved.get("ideas", [])
            surface["idea_ids"] = [i.get("id") or i.get("idea_id") for i in saved[:5]]
            surface["idea_observation"] = "read returned no saved ideas"
        except Exception as exc:
            surface["idea_ids"] = []
            surface["idea_observation"] = "read failed: " + type(exc).__name__
    for client, endpoint in config.get("contract_endpoints", {}).items():
        surface["versions"][client] = get_json(endpoint).get("contract_versions", {})
    advertised = info.get("serverInfo", {}).get("contract_versions") or info.get(
        "_meta", {}
    ).get("contract_versions")
    if advertised:
        surface["versions"]["mcp"] = advertised
    return surface


def collect(config):
    data = {"surfaces": {}, "errors": []}
    try:
        target = config["deployd"]
        data["deployd"] = remote_status(target)
    except Exception as exc:
        data["deployd"] = {}
        data["errors"].append("deployd: " + type(exc).__name__)
    for environment in ("production", "staging"):
        data[environment] = {}
        for target in config.get(environment, []):
            units = []
            for unit in target["units"]:
                unit = dict(unit)
                sha = (
                    data["deployd"]
                    .get(unit["deploy_target"], {})
                    .get("last_deployed_sha", "unknown")
                )
                unit["name"] = unit["name"].replace("{sha}", sha)
                units.append(unit)
            try:
                data[environment].update(remote(target["host"], units))
            except Exception as exc:
                data["errors"].append(environment + " inventory: " + type(exc).__name__)
        try:
            surface = served_surface(
                config["mcp"][environment],
                data[environment],
                load_transport(config["mcp_source"]),
            )
            data["surfaces"][environment] = surface
            observed = surface["version_sha"]
            if not data[environment].get("mcp", {}).get("sha", "").startswith(observed):
                data[environment].setdefault("mcp", {})["sha"] = "served SHA mismatch"
        except Exception as exc:
            data["errors"].append(environment + " discovery: " + type(exc).__name__)
    # Both optional surfaces must be existing GET-only validation/read APIs.
    # No write-capable MCP tool or POST /collect is ever called by this probe.
    for key in ("qa", "collect"):
        settings = config.get(key, {})
        data[key] = {
            "observation": "not requested: missing QA account configuration"
            if key == "qa"
            else "not requested: missing validation-only endpoint"
        }
        if not settings:
            continue
        try:
            if key == "qa" and settings.get("organizations_url"):
                token = os.environ.get(settings.get("token_env", ""))
                if not token or not settings.get("organization_id"):
                    data[key]["observation"] = (
                        "not requested: missing scoped QA token or organization_id"
                    )
                    continue
                result = get_json(settings["organizations_url"], token)
                organizations = (
                    result
                    if isinstance(result, list)
                    else result.get("organizations", [])
                )
                workspace = next(
                    (
                        row
                        for row in organizations
                        if str(row.get("id")) == str(settings["organization_id"])
                    ),
                    None,
                )
                if workspace is None:
                    data[key] = {
                        "signin": True,
                        "observation": "HTTP 200: scoped workspace absent from organizations",
                    }
                    continue
                role = workspace.get("user_role")
                credits = workspace.get("available_credit")
                if isinstance(credits, str):
                    try:
                        credits = float(credits)
                    except ValueError:
                        credits = None
                data[key] = {
                    "observation": "HTTP 200: scoped organization returned",
                    "signin": True,
                    "role": "Owner" if role == "owner" else role,
                    "can_create_agent": role == "owner",
                    "credits": credits,
                }
                continue
            if key == "collect" and settings.get("validation_only") is not True:
                data[key]["observation"] = (
                    "not requested: endpoint not proven validation-only"
                )
                continue
            if not settings.get("read_url"):
                continue
            result = get_json(
                settings["read_url"], os.environ.get(settings.get("token_env", ""))
            )
            allowed = (
                ("signin", "can_create_agent", "credits", "role")
                if key == "qa"
                else ("count", "cap", "code", "dry_run")
            )
            data[key] = {field: result.get(field) for field in allowed}
            data[key]["observation"] = (
                "HTTP 200: projected account response"
                if key == "qa"
                else "HTTP 200: projected validation response"
            )
            if key == "collect":
                data[key]["refusal_codes"] = settings.get("refusal_codes", [])
        except urllib.error.HTTPError as exc:
            data[key] = {"observation": f"HTTP {exc.code}: read refused; body withheld"}
            if key == "qa":
                data[key]["signin"] = False
        except Exception as exc:
            data[key] = {"observation": "read failed: " + type(exc).__name__}
            data["errors"].append(key + ": " + type(exc).__name__)
    return data


def remote_status(target):
    p = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            target["host"],
            "curl -fsS --max-time 15 " + shlex.quote(target["url"]),
        ],
        capture_output=True,
        text=True,
        timeout=25,
    )
    if p.returncode:
        raise RuntimeError("Deployd unavailable")
    raw = json.loads(p.stdout)
    return {
        k: {"last_deployed_sha": v.get("last_deployed_sha")} for k, v in raw.items()
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        required=True,
        type=Path,
        help="Private host inventory and read-only API configuration",
    )
    parser.add_argument("--report", type=Path, default=Path("readiness.json"))
    args = parser.parse_args()
    try:
        data = collect(json.loads(args.config.read_text()))
        rows = compare(data)
        rows.append(
            {
                "check": "observation errors",
                "ok": not data["errors"],
                "detail": "; ".join(data["errors"]) or "None",
            }
        )
        # Report only projected booleans, SHAs, safe URLs and comparison results;
        # never serialize arbitrary HTTP bodies, environment blocks or exceptions.
        report = {
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "ok": all(r["ok"] for r in rows),
            "checks": rows,
            "units": {
                e: {
                    k: {
                        f: v.get(f)
                        for f in ("sha", "active", "import_errors", "process_revisions")
                        if f in v
                    }
                    for k, v in data[e].items()
                }
                for e in ("production", "staging")
            },
        }
    except Exception as exc:
        report = {
            "ok": False,
            "checks": [
                {"check": "configuration", "ok": False, "detail": type(exc).__name__}
            ],
        }
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print("| Check | Result | Evidence |\n|---|---|---|")
    for row in report["checks"]:
        print(
            f"| {row['check']} | {'PASS' if row['ok'] else 'FAIL'} | {row['detail']} |"
        )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
