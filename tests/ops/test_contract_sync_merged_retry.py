"""GEN-10539 contract cases: a merged contract-sync consumer retried after a sibling failure.

The retry runs the committed ``ops/contract-sync/contract-sync.sh`` bytes: the
real ``process_consumer`` path, the real global checkpoint gate and the real
``git status``/diff detection. Only the forge is local (an HTTP mock over bare
Git repositories created under ``tmp_path``), so no network, Gitea, Docker or
production state is touched.

The catalog-sheet artifacts in the fixture are parsed and rendered with the
pinned consumer functions (``fixtures/catalog_sheet_pinned.py``, copied from
gen-mcp-server 80b33e41), never by a second TSV reader written for this test.

# intent: a retry that re-processes an already merged consumer must recognize
# the merge from the PR and the exact head/base/input provenance, accept only
# declared ``served_sha`` stamp cells, and otherwise refuse without a write.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC = REPO_ROOT / "ops" / "contract-sync" / "contract-sync.sh"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
OWNER = "GEN"
TOKEN = "contract-sync-test-token-that-must-never-be-printed"
CONSUMER = "gen-mcp-server"
STAMP_IN_BACKEND = "4db2221"  # ordinary cell text that happens to look like a stamp

NOOP_SCRIPT = """\
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--backend-path")
parser.add_argument("--ref")
parser.parse_known_args()
"""


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def write_json(path: Path, selector: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"selector": selector}) + "\n")


# -----------------------------------------------------------------------------
# Local forge: a Gitea-shaped HTTP API over the bare repositories on disk.
# -----------------------------------------------------------------------------


class Forge:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.prs: list[dict] = []
        self.mode = "success"
        self.branch_overrides: dict[tuple[str, str], str] = {}
        self.log: list[dict] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()

    def reset(self, prs: list[dict] | None = None, mode: str = "success") -> None:
        self.prs = prs or []
        self.mode = mode
        self.log = []

    def mutations(self, repo: str | None = None) -> list[dict]:
        rows = [row for row in self.log if row.get("kind") in {"create", "merge", "comment", "close", "delete"}]
        if repo is not None:
            rows = [row for row in rows if f"/{repo}/" in row.get("path", "")]
        return rows

    def branch_head(self, repo: str, branch: str) -> str | None:
        override = self.branch_overrides.get((repo, branch))
        if override:
            return override
        try:
            return git("--git-dir", str(self.repo_root / f"{repo}.git"), "rev-parse",
                       f"refs/heads/{branch}", cwd=str(self.repo_root))
        except subprocess.CalledProcessError:
            return None

    def _handler(self):
        forge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code: int, payload) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:  # the client timed out on purpose
                    pass

            def _route(self) -> str:
                return self.path.split("?")[0]

            def _query(self) -> dict[str, str]:
                if "?" not in self.path:
                    return {}
                return dict(part.split("=", 1) for part in self.path.split("?", 1)[1].split("&") if "=" in part)

            def do_GET(self):
                path = self._route()
                if path == "/__reset":
                    return self._send(204, {})
                if "/pulls" in path:
                    if self.path.endswith("/pulls") or "/pulls?" in self.path:
                        if forge.mode == "pr403":
                            return self._send(403, {"message": "forbidden"})
                        if forge.mode == "pr_timeout":
                            import time

                            time.sleep(5)
                        if forge.mode == "pr_malformed":
                            raw = b"{not-json"
                            self.send_response(200)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(raw)))
                            self.end_headers()
                            self.wfile.write(raw)
                            return None
                        query = self._query()
                        wanted = query.get("state", "all")
                        rows = [
                            pr for pr in forge.prs
                            if wanted in ("all", "") or pr.get("state") == wanted
                        ]
                        if query.get("head"):
                            rows = [pr for pr in rows if pr.get("head", {}).get("ref") == query["head"]]
                        return self._send(200, rows)
                    number = int(path.rstrip("/").rsplit("/", 1)[1])
                    row = next((pr for pr in forge.prs if int(pr["number"]) == number), None)
                    if row is None:
                        return self._send(404, {"message": "no such pull"})
                    return self._send(200, row)
                if "/branches/" in path:
                    repo = path.split("/repos/", 1)[1].split("/")[1]
                    branch = path.split("/branches/", 1)[1]
                    head = forge.branch_head(repo, branch)
                    if head is None:
                        return self._send(404, {"message": "branch not found"})
                    return self._send(200, {"name": branch, "commit": {"id": head}})
                if "/commits/" in path and path.endswith("/statuses"):
                    mode = forge.mode
                    if mode in ("absent",):
                        return self._send(200, [])
                    state = "success" if mode in ("success", "behind", "conflict", "pr403", "pr_timeout", "pr_malformed") else mode
                    return self._send(200, [
                        {"id": 1, "context": "ci/woodpecker/push/ci", "status": state},
                        {"id": 2, "context": "Development Workflow / Unit Tests (pull_request)", "status": "pending"},
                    ])
                return self._send(404, {"message": "no route"})

            def _payload(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    return json.loads(raw or b"{}")
                except ValueError:
                    return {}

            def do_POST(self):
                path = self._route()
                payload = self._payload()
                if path.endswith("/merge"):
                    number = int(path.split("/pulls/", 1)[1].split("/")[0])
                    forge.log.append({"kind": "merge", "path": path, "number": number, "do": payload.get("Do")})
                    if forge.mode in ("behind", "conflict"):
                        return self._send(405, {"message": "Not possible to fast-forward"})
                    for pr in forge.prs:
                        if int(pr["number"]) == number:
                            pr.update({"state": "closed", "merged": True})
                    return self._send(200, {"merged": True})
                if "/issues/" in path and path.endswith("/comments"):
                    forge.log.append({"kind": "comment", "path": path, "body": payload.get("body")})
                    return self._send(201, {"id": 1})
                if path.endswith("/pulls"):
                    number = 900 + len([r for r in forge.log if r["kind"] == "create"])
                    forge.log.append({
                        "kind": "create", "path": path, "number": number,
                        "head": payload.get("head"), "body": payload.get("body"),
                    })
                    forge.prs.append({
                        "number": number, "state": "open", "merged": False,
                        "base": {"ref": payload.get("base", "main")},
                        "head": {"ref": payload.get("head"), "sha": forge.branch_head(
                            path.split("/repos/", 1)[1].split("/")[1], payload.get("head", ""))},
                        "title": payload.get("title", ""), "body": payload.get("body", ""),
                    })
                    return self._send(201, {"number": number, "head": {"ref": payload.get("head")}})
                return self._send(404, {"message": "no route"})

            def do_PATCH(self):
                path = self._route()
                self._payload()
                number = int(path.split("/pulls/", 1)[1].split("/")[0])
                forge.log.append({"kind": "close", "path": path, "number": number})
                for pr in forge.prs:
                    if int(pr["number"]) == number:
                        pr["state"] = "closed"
                return self._send(200, {})

            def do_DELETE(self):
                path = self._route()
                repo = path.split("/repos/", 1)[1].split("/")[1]
                branch = path.split("/branches/", 1)[1]
                forge.log.append({"kind": "delete", "path": path, "branch": branch})
                subprocess.run(
                    ["git", "--git-dir", str(forge.repo_root / f"{repo}.git"), "update-ref", "-d", f"refs/heads/{branch}"],
                    cwd=str(forge.repo_root), check=False, capture_output=True,
                )
                return self._send(204, {})

        return Handler


# -----------------------------------------------------------------------------
# Fixture repositories
# -----------------------------------------------------------------------------


class Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.git_root = root / "git"
        self.work = root / "work"
        self.state = root / "state"
        self.git_root.mkdir(parents=True)
        self.work.mkdir()
        self.state.mkdir()
        self._build_backend()
        self._build_mcp()
        self.forge = Forge(self.git_root / OWNER)

    # -- helpers -------------------------------------------------------------
    def bare(self, name: str) -> Path:
        owner_dir = self.git_root / OWNER
        owner_dir.mkdir(parents=True, exist_ok=True)
        return owner_dir / f"{name}.git"

    def init_bare(self, name: str) -> Path:
        path = self.bare(name)
        git("init", "-q", "--bare", "--initial-branch=main", str(path), cwd=str(self.root))
        return path

    def commit_all(self, cwd: Path, message: str) -> str:
        git("add", "-A", cwd=cwd)
        git("-c", "user.name=test", "-c", "user.email=test@gen.pro", "commit", "-q", "-m", message, cwd=cwd)
        return git("rev-parse", "HEAD", cwd=cwd)

    def push_main(self, cwd: Path) -> None:
        git("push", "-q", "origin", "main", cwd=cwd)

    def _build_backend(self) -> None:
        self.be_bare = self.init_bare("gen-backend-v2")
        be = self.work / "be"
        git("init", "-q", "--initial-branch=main", str(be))
        for name in ("vidsheet-operations-schema", "vidsheet-semantic-draft-schema", "user-job-enums"):
            write_json(be / "docs" / "generated" / f"{name}.json", 1)
        (be / "config").mkdir()
        (be / "config" / "creation_cards.yml").write_text("version: 1\n")
        (be / "config" / "model_capabilities.yml").write_text(f"version: {STAMP_IN_BACKEND}\n")
        self.be_old = self.commit_all(be, "backend v1")
        git("remote", "add", "origin", str(self.be_bare), cwd=be)
        self.push_main(be)
        # B: one watched artifact changes, the ordinary catalog cell input stays put.
        write_json(be / "docs" / "generated" / "user-job-enums.json", 2)
        self.be = self.commit_all(be, "backend v2 user-job-enums")
        self.push_main(be)
        self.be_work = be
        self.be8 = self.be[:8]
        self.be_old7 = self.be_old[:7]

    def _build_mcp(self) -> None:
        mcp_bare = self.init_bare("mcp-source")
        mcp = self.work / "mcp"
        git("init", "-q", "--initial-branch=main", str(mcp))
        record = mcp / "src" / "gen_mcp_server" / "contracts" / "catalog-record"
        record.mkdir(parents=True)
        (record / "schema-paths.tsv").write_text("gen_fixture_action\tv1\n")
        (record / "contract-paths.tsv").write_text("GET /fixture/v1\n")
        self.mcp = self.commit_all(mcp, "mcp v1")
        git("remote", "add", "origin", str(mcp_bare), cwd=mcp)
        self.push_main(mcp)
        self.mcp_work = mcp

    def _build_consumer(self, name: str, files: dict[str, str], message: str) -> tuple[Path, Path]:
        bare = self.init_bare(name)
        work = self.work / name
        git("init", "-q", "--initial-branch=main", str(work))
        for relative, content in files.items():
            path = work / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        (work / "README.md").write_text("consumer\n")
        head = self.commit_all(work, message)
        git("remote", "add", "origin", str(bare), cwd=work)
        self.push_main(work)
        return work, bare, head

    def mcp_consumer(self, sheet_at: str | None) -> tuple[Path, Path, str]:
        """gen-mcp-server whose vendored catalog sheet is stamped at ``sheet_at``."""
        files = {
            "scripts/vendor_creation_card_branches.py": NOOP_SCRIPT,
            "scripts/vendor_creation_card_matrix.py": NOOP_SCRIPT,
            "scripts/vendor_user_job_enums.py": NOOP_SCRIPT,
            "scripts/sync_model_capabilities.py": NOOP_SCRIPT,
            "scripts/vendor_vidsheet_action_schema.py": (FIXTURES / "catalog_sheet_regen.py").read_text(),
            "scripts/catalog_sheet_pinned.py": (FIXTURES / "catalog_sheet_pinned.py").read_text(),
        }
        work, bare, head = self._build_consumer(CONSUMER, files, "consumer baseline")
        if sheet_at is not None:
            subprocess.run(
                [sys.executable, "scripts/vendor_vidsheet_action_schema.py",
                 "--backend-path", str(self.be_work), "--ref", sheet_at],
                cwd=work, check=True, capture_output=True, text=True,
            )
            head = self.commit_all(work, "vendor catalog sheet")
            self.push_main(work)
        return work, bare, head

    def retain_merged_branch(self, bare: Path, work: Path, head: str) -> str:
        branch = f"auto/contract-sync-{self.be8}"
        git("push", "-q", "origin", f"{head}:refs/heads/{branch}", cwd=work)
        assert git("--git-dir", str(bare), "rev-parse", f"refs/heads/{branch}", cwd=str(self.root)) == head
        return branch

    def run_sync(self, consumers: str, curl_timeout: int = 2, extra_env: dict | None = None):
        env = os.environ.copy()
        env.update({
            "CONTRACT_SYNC_STATE_DIR": str(self.state),
            "CONTRACT_SYNC_GIT_BASE": f"file://{self.git_root}",
            "CONTRACT_SYNC_OWNER": OWNER,
            "CONTRACT_SYNC_BACKEND_URL": f"file://{self.be_bare}",
            "CONTRACT_SYNC_MCP_URL": f"file://{self.bare('mcp-source')}",
            "CONTRACT_SYNC_CONSUMERS": consumers,
            "CONTRACT_SYNC_STATUS_POLL_INTERVAL": "1",
            "CONTRACT_SYNC_STATUS_POLL_TIMEOUT": "2",
            "CONTRACT_SYNC_MAIN_PIPELINE_WAIT": "1",
            "CONTRACT_SYNC_GIT_TIMEOUT": "30",
            "CONTRACT_SYNC_REGEN_TIMEOUT": "30",
            "CONTRACT_SYNC_CURL_TIMEOUT": str(curl_timeout),
            "GEN_GITEA_URL": self.forge.url,
            "GEN_GITEA_TOKEN": TOKEN,
        })
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(SYNC)], cwd=str(self.root), env=env,
            capture_output=True, text=True, timeout=180
        )

    def checkpoint(self) -> str:
        try:
            return (self.state / "last-be-sha").read_text().strip()
        except OSError:
            return ""

    def seed_checkpoint(self, sha: str) -> None:
        (self.state / "last-be-sha").write_text(sha + "\n")

    def refs(self, bare: Path) -> str:
        return git("--git-dir", str(bare), "for-each-ref", "--format=%(refname) %(objectname)",
                   cwd=str(self.root))

    def merged_pr(self, repo: str, number: int, branch: str, head: str,
                  base_ref: str = "main", base_sha: str | None = None,
                  backend_sha: str | None = None, merged: bool = True) -> dict:
        backend_sha = backend_sha or self.be
        return {
            "number": number, "state": "closed" if merged else "closed",
            "merged": merged,
            "merged_at": "2026-10-16T04:20:00Z" if merged else None,
            "merge_commit_sha": head,
            "title": f"chore: sync backend contracts to gen-backend-v2 {backend_sha[:8]}",
            "body": f"Synced from gen-backend-v2 {backend_sha}.\n",
            "base": {"ref": base_ref, "sha": base_sha if base_sha is not None else head},
            "head": {"ref": branch, "sha": head},
            "html_url": f"http://mock.invalid/{repo}/pulls/{number}",
        }

    def assert_no_writes(self, bare: Path, before_refs: str, repo: str) -> None:
        assert self.refs(bare) == before_refs, "a retry must not push, move or delete a ref"
        assert self.forge.mutations(repo) == [], "a retry must not create, merge, comment or close"


@pytest.fixture()
def fx(tmp_path):
    fixture = Fixture(tmp_path)
    yield fixture
    fixture.forge.close()


def sheet_bytes(bare: Path) -> bytes:
    return subprocess.run(
        ["git", "--git-dir", str(bare), "show", "main:src/gen_mcp_server/contracts/catalog-sheet.tsv"],
        cwd=str(bare.parent), check=True, capture_output=True,
    ).stdout


def retry_case(fx: Fixture, *, sheet_at: str | None) -> tuple[Path, Path, str, str]:
    """Consumer main + retained merged branch head, with the provenance checkpoint set."""
    work, bare, head = fx.mcp_consumer(sheet_at)
    branch = fx.retain_merged_branch(bare, work, head)
    fx.seed_checkpoint(fx.be_old)
    return work, bare, head, branch


def merge_pr_for(fx: Fixture, branch: str, head: str, **kwargs) -> dict:
    return fx.merged_pr(CONSUMER, 606, branch, head, **kwargs)


# -----------------------------------------------------------------------------
# T1 -- the merged branch head equals main and regeneration is identical.
# -----------------------------------------------------------------------------


def test_t1_merged_consumer_with_identical_regeneration_is_a_no_op(fx):
    # intent: a lost checkpoint replay of an already merged, fully-converged
    # consumer succeeds without a new push, PR, merge or branch deletion.
    _, bare, head, branch = retry_case(fx, sheet_at=fx.be)  # main is already stamped B
    fx.forge.reset([merge_pr_for(fx, branch, head)])
    before_refs, before_bytes = fx.refs(bare), sheet_bytes(bare)

    run = fx.run_sync(CONSUMER)

    assert run.returncode == 0, run.stdout + run.stderr
    fx.assert_no_writes(bare, before_refs, CONSUMER)
    assert sheet_bytes(bare) == before_bytes
    assert fx.checkpoint() == fx.be, "the consumer reached its terminal success"


# -----------------------------------------------------------------------------
# T2 -- the only regenerated difference is the declared served_sha stamps.
# -----------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, raises=AssertionError)
def test_t2_merged_consumer_with_stamp_only_diff_converges(fx):
    # intent: a retained branch whose PR is merged with this exact head/base and
    # input provenance is already satisfied even when regeneration re-stamps
    # served_sha; the retry must not push, open a PR, merge, delete or edit.
    _, bare, head, branch = retry_case(fx, sheet_at=fx.be_old)  # main stamped A, replay is B
    fx.forge.reset([merge_pr_for(fx, branch, head)])
    before_refs, before_bytes = fx.refs(bare), sheet_bytes(bare)
    assert STAMP_IN_BACKEND.encode() in before_bytes, "ordinary cell text carries the old stamp"
    assert fx.be_old7 != fx.be[:7]

    run = fx.run_sync(CONSUMER)

    assert run.returncode == 0, run.stdout + run.stderr
    fx.assert_no_writes(bare, before_refs, CONSUMER)
    assert sheet_bytes(bare) == before_bytes, "no file or real cell may be rewritten"
    assert fx.checkpoint() == fx.be


# -----------------------------------------------------------------------------
# T3 -- same stamp-only diff, but the PR was closed without merging.
# -----------------------------------------------------------------------------


def test_t3_closed_unmerged_pr_with_stamp_only_diff_refuses(fx):
    # intent: a closed, unmerged PR is not proof; the retry refuses with zero
    # writes even when the regenerated difference is only the declared stamp.
    _, bare, head, branch = retry_case(fx, sheet_at=fx.be_old)
    fx.forge.reset([merge_pr_for(fx, branch, head, merged=False)])
    before_refs, before_bytes = fx.refs(bare), sheet_bytes(bare)
    checkpoint = fx.checkpoint()

    run = fx.run_sync(CONSUMER)

    assert run.returncode == 1, run.stdout + run.stderr
    fx.assert_no_writes(bare, before_refs, CONSUMER)
    assert sheet_bytes(bare) == before_bytes
    assert fx.checkpoint() == checkpoint


# -----------------------------------------------------------------------------
# T4 -- the regeneration changes one real cell as well as the stamp.
# -----------------------------------------------------------------------------


def test_t4_real_regenerated_change_refuses(fx):
    # intent: only declared stamp cells are volatile; a changed tool enum or
    # artifact byte keeps the consumer at refusal with every byte preserved.
    work, bare, head = fx.mcp_consumer(sheet_at=fx.be_old)
    # Make main's ordinary cell stale relative to what the input now produces:
    # the regenerated sheet changes one real Branches cell, not just served_sha.
    sheet = work / "src" / "gen_mcp_server" / "contracts" / "catalog-sheet.tsv"
    sheet.write_text(sheet.read_text().replace(
        f"mode: version: {STAMP_IN_BACKEND} | manual", "mode: STALE | manual"
    ))
    head = fx.commit_all(work, "stale real cell")
    fx.push_main(work)
    branch = fx.retain_merged_branch(bare, work, head)
    fx.seed_checkpoint(fx.be_old)
    fx.forge.reset([merge_pr_for(fx, branch, head)])
    before_refs, before_bytes = fx.refs(bare), sheet_bytes(bare)
    checkpoint = fx.checkpoint()

    run = fx.run_sync(CONSUMER)

    assert run.returncode == 1, run.stdout + run.stderr
    fx.assert_no_writes(bare, before_refs, CONSUMER)
    assert sheet_bytes(bare) == before_bytes
    assert fx.checkpoint() == checkpoint


# -----------------------------------------------------------------------------
# T5 -- an existing open PR stays authoritative through its lifecycle.
# -----------------------------------------------------------------------------


def test_t5_open_pr_is_reused_and_merged_once_when_green(fx):
    # intent: a retry re-checks the existing PR, never creates a second one or
    # pushes again; pending leaves the checkpoint, green merges exactly once.
    bare = fx.init_bare("gen-agentic")
    work = fx.work / "gen-agentic"
    git("init", "-q", "--initial-branch=main", str(work))
    for name in ("vidsheet-operations-schema", "vidsheet-semantic-draft-schema", "user-job-enums"):
        dest = work / "src" / "gen" / "contracts" / f"{name}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        write_json(dest, 1)
    main_head = fx.commit_all(work, "consumer v1")
    git("remote", "add", "origin", str(bare), cwd=work)
    fx.push_main(work)
    # A previous tick pushed this branch and opened PR 9; a clone of main still
    # regenerates the newer artifact, so the retry reaches the open-PR path.
    for name in ("vidsheet-operations-schema", "vidsheet-semantic-draft-schema", "user-job-enums"):
        write_json(work / "src" / "gen" / "contracts" / f"{name}.json", 2)
    branch = f"auto/contract-sync-{fx.be8}"
    head = fx.commit_all(work, "sync branch")
    git("push", "-q", "origin", f"{head}:refs/heads/{branch}", cwd=work)
    git("checkout", "-q", main_head, cwd=work)
    fx.seed_checkpoint(fx.be_old)
    open_pr = fx.merged_pr("gen-agentic", 9, branch, head, merged=False)
    open_pr["state"] = "open"
    fx.forge.reset([open_pr], mode="pending")
    before_refs = fx.refs(bare)

    pending = fx.run_sync("gen-agentic")
    assert pending.returncode == 1, pending.stdout + pending.stderr
    assert fx.forge.mutations("gen-agentic") == []
    assert fx.checkpoint() == fx.be_old, "pending checks are not a terminal disposition"

    fx.forge.mode = "success"
    green = fx.run_sync("gen-agentic")
    assert green.returncode == 0, green.stdout + green.stderr
    assert "re-checking it" in green.stdout
    merges = [row for row in fx.forge.mutations("gen-agentic") if row["kind"] == "merge"]
    assert len(merges) == 1, "the existing merge lifecycle runs once"
    assert not [row for row in fx.forge.mutations("gen-agentic") if row["kind"] == "create"]
    assert fx.refs(bare) == before_refs, "no second push for a retry"
    assert fx.checkpoint() == fx.be


# -----------------------------------------------------------------------------
# T6 -- identity and provenance must match exactly.
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["wrong-base", "wrong-repo", "wrong-head", "wrong-input", "not-ancestor"])
def test_t6_wrong_identity_or_provenance_refuses(fx, variant):
    # intent: a matching branch spelling is not proof; repo, base, head, input
    # sha and merged ancestry are all part of the evidence.
    _, bare, head, branch = retry_case(fx, sheet_at=fx.be_old)
    pr = merge_pr_for(fx, branch, head)
    if variant == "wrong-base":
        pr["base"]["ref"] = "develop"
    elif variant == "wrong-repo":
        pr = fx.merged_pr("gen-mcp-server-fork", 606, branch, head)
    elif variant == "wrong-head":
        pr["head"]["sha"] = "1" * 40
    elif variant == "wrong-input":
        pr["head"]["ref"] = "auto/contract-sync-cccccccc"
        pr["title"] = "chore: sync backend contracts to gen-backend-v2 cccccccc"
        pr["body"] = "Synced from gen-backend-v2 " + "c" * 40 + ".\n"
    else:  # not-ancestor: the merged commit is not in current main's history
        work = fx.work / CONSUMER
        git("checkout", "-q", "--detach", git("rev-parse", "main", cwd=work), cwd=work)
        (work / "DIVERGENT").write_text("x\n")
        divergent = fx.commit_all(work, "divergent")
        git("push", "-q", "origin", f"{divergent}:refs/heads/{branch}", cwd=work)
        git("checkout", "-q", "main", cwd=work)
        pr = merge_pr_for(fx, branch, divergent)
        head = divergent
    fx.forge.reset([pr])
    before_refs, before_bytes = fx.refs(bare), sheet_bytes(bare)
    checkpoint = fx.checkpoint()

    run = fx.run_sync(CONSUMER)

    assert run.returncode == 1, run.stdout + run.stderr
    fx.assert_no_writes(bare, before_refs, CONSUMER)
    assert sheet_bytes(bare) == before_bytes
    assert fx.checkpoint() == checkpoint


# -----------------------------------------------------------------------------
# T7 -- unavailable or malformed evidence is never convergence.
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variant", ["pr-403", "pr-timeout", "pr-malformed", "catalog-dup-header", "catalog-missing-header", "catalog-truncated"]
)
def test_t7_unavailable_or_malformed_evidence_refuses(fx, variant):
    # intent: an API error, a timeout, a malformed PR body or a malformed
    # catalog sheet refuses within the existing bounded harness, writes nothing
    # and never prints the token.
    _, bare, head, branch = retry_case(fx, sheet_at=fx.be_old)
    pr = merge_pr_for(fx, branch, head)
    mode = "success"
    extra_env = None
    if variant == "pr-403":
        mode = "pr403"
    elif variant == "pr-timeout":
        mode = "pr_timeout"
    elif variant == "pr-malformed":
        mode = "pr_malformed"
    else:
        extra_env = {"FIXTURE_CATALOG_MODE": variant.replace("catalog-", "")}
    fx.forge.reset([pr], mode=mode)
    before_refs, before_bytes = fx.refs(bare), sheet_bytes(bare)
    checkpoint = fx.checkpoint()

    run = fx.run_sync(CONSUMER, curl_timeout=1, extra_env=extra_env)

    assert run.returncode == 1, run.stdout + run.stderr
    fx.assert_no_writes(bare, before_refs, CONSUMER)
    assert sheet_bytes(bare) == before_bytes
    assert fx.checkpoint() == checkpoint
    assert TOKEN not in run.stdout + run.stderr + json.dumps(fx.forge.log)


# -----------------------------------------------------------------------------
# T8 -- a sibling failure keeps the global checkpoint put.
# -----------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, raises=AssertionError)
def test_t8_sibling_failure_holds_the_global_checkpoint(fx):
    # intent: the global checkpoint advances only after every consumer is
    # terminal; a sibling failure replays this consumer without duplicate writes.
    _, bare, head, branch = retry_case(fx, sheet_at=fx.be_old)
    fx.forge.reset([merge_pr_for(fx, branch, head)])
    before_refs, before_bytes = fx.refs(bare), sheet_bytes(bare)

    first = fx.run_sync(f"{CONSUMER} gen-agentic")
    assert first.returncode == 1, first.stdout + first.stderr
    assert "cannot clone" in first.stdout, "the sibling consumer failed before any merge"
    assert fx.checkpoint() == fx.be_old, "a sibling failure never advances the checkpoint"
    fx.assert_no_writes(bare, before_refs, CONSUMER)
    assert sheet_bytes(bare) == before_bytes

    # The sibling repo appears; the same input now reaches a terminal success.
    sibling_bare = fx.init_bare("gen-agentic")
    sibling = fx.work / "gen-agentic"
    git("init", "-q", "--initial-branch=main", str(sibling))
    for name in ("vidsheet-operations-schema", "vidsheet-semantic-draft-schema", "user-job-enums"):
        dest = sibling / "src" / "gen" / "contracts" / f"{name}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((fx.be_work / "docs" / "generated" / f"{name}.json").read_bytes())
    fx.commit_all(sibling, "sibling current")
    git("remote", "add", "origin", str(sibling_bare), cwd=sibling)
    fx.push_main(sibling)

    second = fx.run_sync(f"{CONSUMER} gen-agentic")
    assert second.returncode == 0, second.stdout + second.stderr
    assert fx.checkpoint() == fx.be, "the checkpoint advances once, after both consumers"
    assert fx.refs(bare) == before_refs, "no duplicate write to the already merged consumer"
    assert sheet_bytes(bare) == before_bytes
    assert fx.forge.mutations(CONSUMER) == []
