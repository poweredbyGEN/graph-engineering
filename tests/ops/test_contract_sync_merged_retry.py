"""GEN-10539 contract cases: a merged contract-sync consumer retried after a sibling failure.

The retry runs the committed ``ops/contract-sync/contract-sync.sh`` bytes: the
real ``process_consumer`` path, the real global checkpoint gate and the real
``git status``/diff detection. Only the forge is local (an HTTP mock over bare
Git repositories created under ``tmp_path``), so no network, Gitea, Docker or
production state is touched.

The catalog-sheet artifacts in the fixture are parsed and rendered with the
pinned consumer functions (``fixtures/catalog_sheet_pinned.py``, copied from
gen-mcp-server 80b33e41), never by a second TSV reader written for this test.
The fixture generator follows the mirror's stamping rule: ``served_sha`` is the
consumer HEAD at mirror-cut time, so a re-run against a newer main differs only
in declared stamp cells -- the shape of the incident (4db2221 -> 80b33e4).

# intent: a retry that re-processes an already merged consumer must recognize
# the merge from the PR and the exact head/base/input provenance, accept only
# declared ``served_sha`` stamp cells, and otherwise refuse without a write.
"""

from __future__ import annotations

import json
import os
import re
import shutil
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
SHEET_PATH = "src/gen_mcp_server/contracts/catalog-sheet.tsv"
SIDECAR_PATH = "src/gen_mcp_server/contracts/catalog-sheet.source.json"

NOOP_SCRIPT = """\
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--backend-path")
parser.add_argument("--ref")
parser.parse_known_args()
"""

# Deterministic, isolated git for every fixture repository: no system or user
# config (signing prompts, hooks, templates) and fixed commit timestamps.
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_DATE": "2026-10-16T04:20:00 +0000",
    "GIT_COMMITTER_DATE": "2026-10-16T04:20:00 +0000",
    "GIT_TERMINAL_PROMPT": "0",
}


def git(*args: str, cwd: Path | None = None) -> str:
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def write_json(path: Path, selector: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"selector": selector}) + "\n")


# -----------------------------------------------------------------------------
# Local forge: a Gitea-shaped HTTP API over the bare repositories on disk.
# -----------------------------------------------------------------------------


class Forge:
    """Mock Gitea.

    Every non-GET request is logged on entry, whatever the route, so a product
    that attempts a write the mock does not implement (``POST /pulls/N/update``,
    an unmapped ``PUT``) still counts as a write.
    """

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.prs: list[dict] = []
        self.mode = "success"
        self.branch_overrides: dict[tuple[str, str], str] = {}
        self.writes: list[dict] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()

    def reset(self, prs: list[dict] | None = None, mode: str = "success") -> None:
        self.prs = prs or []
        self.mode = mode
        self.writes = []
        self.branch_overrides = {}

    def mutations(self, repo: str | None = None) -> list[dict]:
        rows = [row for row in self.writes if row.get("kind") != "unrouted"]
        if repo is not None:
            rows = [row for row in rows if f"/{repo}/" in row.get("path", "")]
        return rows

    def scoped_writes(self, repo: str) -> list[dict]:
        return [row for row in self.writes if f"/{repo}/" in row.get("path", "")]

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

            def _send_raw(self, body: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

            def _route(self) -> str:
                return self.path.split("?")[0]

            def _query(self) -> dict[str, str]:
                if "?" not in self.path:
                    return {}
                return dict(part.split("=", 1) for part in self.path.split("?", 1)[1].split("&") if "=" in part)

            def _record(self, kind: str, payload: dict | None = None) -> None:
                forge.writes.append({
                    "method": self.command, "path": self._route(), "kind": kind,
                    **(payload or {}),
                })

            def _repo(self, path: str) -> str:
                return path.split("/repos/", 1)[1].split("/")[1]

            def _pr_failure(self) -> bool:
                """Apply the unavailable-evidence modes to every PR read path."""
                if forge.mode == "pr403":
                    self._send(403, {"message": "forbidden"})
                    return True
                if forge.mode == "pr_timeout":
                    import time

                    time.sleep(5)
                    return False  # the client aborts; nothing to answer
                if forge.mode == "pr_malformed":
                    self._send_raw(b"{not-json")
                    return True
                return False

            def do_GET(self):
                path = self._route()
                if path == "/__reset":
                    return self._send(204, {})
                if "/pulls" in path:
                    if self._pr_failure():
                        return None
                    if path.endswith("/pulls"):
                        query = self._query()
                        wanted = query.get("state", "all")
                        rows = [
                            pr for pr in forge.prs
                            if pr.get("_repo") == self._repo(path)
                            and (wanted in ("all", "") or pr.get("state") == wanted)
                        ]
                        # The list endpoint takes no ``head=`` filter: a caller
                        # must match the head ref itself, as with real Gitea.
                        return self._send(200, [
                            {k: v for k, v in pr.items() if k != "_repo"} for pr in rows
                        ])
                    tail = path.rstrip("/").rsplit("/", 1)[1]
                    if not tail.isdigit():
                        return self._send(404, {"message": "no such pull"})
                    row = next((pr for pr in forge.prs
                                if pr.get("_repo") == self._repo(path) and int(pr["number"]) == int(tail)), None)
                    if row is None:
                        return self._send(404, {"message": "no such pull"})
                    return self._send(200, {k: v for k, v in row.items() if k != "_repo"})
                if "/branches/" in path:
                    repo = self._repo(path)
                    branch = path.split("/branches/", 1)[1]
                    head = forge.branch_head(repo, branch)
                    if head is None:
                        return self._send(404, {"message": "branch not found"})
                    return self._send(200, {"name": branch, "commit": {"id": head}})
                if "/commits/" in path and path.endswith("/statuses"):
                    mode = forge.mode
                    if mode == "absent":
                        return self._send(200, [])
                    state = "success" if mode in (
                        "success", "behind", "conflict", "pr403", "pr_timeout", "pr_malformed"
                    ) else mode
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
                    self._record("merge", {"number": number, "do": payload.get("Do")})
                    if forge.mode in ("behind", "conflict"):
                        return self._send(405, {"message": "Not possible to fast-forward"})
                    for pr in forge.prs:
                        if int(pr["number"]) == number:
                            pr.update({"state": "closed", "merged": True})
                    return self._send(200, {"merged": True})
                if "/issues/" in path and path.endswith("/comments"):
                    self._record("comment", {"body": payload.get("body")})
                    return self._send(201, {"id": 1})
                if path.endswith("/pulls"):
                    number = 900 + len([r for r in forge.writes if r["kind"] == "create"])
                    head = forge.branch_head(self._repo(path), payload.get("head", ""))
                    self._record("create", {"number": number, "head": payload.get("head")})
                    forge.prs.append({
                        "number": number, "state": "open", "merged": False,
                        "base": {"ref": payload.get("base", "main"), "repo": _identity(self._repo(path))},
                        "head": {"ref": payload.get("head"), "sha": head, "repo": _identity(self._repo(path))},
                        "title": payload.get("title", ""), "body": payload.get("body", ""),
                        "_repo": self._repo(path),
                    })
                    return self._send(201, {"number": number, "head": {"ref": payload.get("head")}})
                # Everything else is an attempt to write that this mock does not
                # implement (for example POST /pulls/N/update?style=rebase).
                self._record("unrouted")
                return self._send(404, {"message": "no route"})

            def do_PATCH(self):
                path = self._route()
                self._payload()
                if "/pulls/" not in path:
                    self._record("unrouted")
                    return self._send(404, {"message": "no route"})
                tail = path.rstrip("/").rsplit("/", 1)[1]
                if not tail.isdigit():
                    self._record("unrouted")
                    return self._send(404, {"message": "no route"})
                number = int(tail)
                self._record("close", {"number": number})
                for pr in forge.prs:
                    if int(pr["number"]) == number:
                        pr["state"] = "closed"
                return self._send(200, {})

            def do_PUT(self):
                self._record("unrouted")
                return self._send(404, {"message": "no route"})

            def do_DELETE(self):
                path = self._route()
                repo = self._repo(path)
                branch = path.split("/branches/", 1)[1]
                self._record("delete", {"branch": branch})
                subprocess.run(
                    ["git", "--git-dir", str(forge.repo_root / f"{repo}.git"), "update-ref", "-d", f"refs/heads/{branch}"],
                    cwd=str(forge.repo_root), check=False, capture_output=True,
                )
                return self._send(204, {})

        return Handler


def _identity(repo: str) -> dict:
    return {"full_name": f"{OWNER}/{repo}", "name": repo, "owner": {"login": OWNER}}


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
        # The watched value the catalog sheet's ordinary enum cell derives from.
        (be / "config" / "model_capabilities.yml").write_text("version: mirror-v1\n")
        self.be_old = self.commit_all(be, "backend v1")
        git("remote", "add", "origin", str(self.be_bare), cwd=be)
        self.push_main(be)
        # B: one watched artifact changes, the ordinary catalog cell input stays put.
        write_json(be / "docs" / "generated" / "user-job-enums.json", 2)
        self.be = self.commit_all(be, "backend v2 user-job-enums")
        self.push_main(be)
        self.be_work = be
        self.be8 = self.be[:8]

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

    def _build_consumer(self, name: str, files: dict[str, str], message: str) -> tuple[Path, Path, str]:
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

    def run_regen(self, work: Path, stamp_mode: str) -> None:
        subprocess.run(
            [sys.executable, "scripts/vendor_vidsheet_action_schema.py",
             "--backend-path", str(self.be_work), "--ref", "origin/main"],
            cwd=work, check=True, capture_output=True, text=True,
            # The generator shells out to git, so it takes the same isolated
            # config and fixed clock as every other fixture git invocation
            # (a user-level core.abbrev would change the stamp width).
            env={**os.environ, **GIT_ENV, "FIXTURE_STAMP_MODE": stamp_mode},
        )

    def mcp_consumer(self, sheet: bool, stamp_mode: str = "consumer-head") -> tuple[Path, Path, str, str]:
        """gen-mcp-server consumer; returns work, bare, main head, stamp at cut time."""
        files = {
            "scripts/vendor_creation_card_branches.py": NOOP_SCRIPT,
            "scripts/vendor_creation_card_matrix.py": NOOP_SCRIPT,
            "scripts/vendor_user_job_enums.py": NOOP_SCRIPT,
            "scripts/sync_model_capabilities.py": NOOP_SCRIPT,
            "scripts/vendor_vidsheet_action_schema.py": (FIXTURES / "catalog_sheet_regen.py").read_text(),
            "scripts/catalog_sheet_pinned.py": (FIXTURES / "catalog_sheet_pinned.py").read_text(),
        }
        work, bare, head = self._build_consumer(CONSUMER, files, "consumer baseline")
        cut_at = ""
        if sheet:
            # The mirror records the consumer HEAD it was cut from, never the
            # backend sha and never the commit that will carry the sheet.
            cut_at = self.consumer_head_short(work)
            self.run_regen(work, stamp_mode)
            head = self.commit_all(work, "vendor catalog sheet")
            self.push_main(work)
        committed = git("--git-dir", str(bare), "ls-tree", "-r", "--name-only", "main",
                        cwd=str(self.root)).splitlines()
        bytecode = [path for path in committed
                    if "__pycache__" in path or path.endswith((".pyc", ".pyo"))]
        if bytecode:
            # A committed cache file is a real changed generated path on retry,
            # so the consumer could never be proven converged on any host.
            raise RuntimeError(f"fixture: consumer main carries generated bytecode: {bytecode}")
        return work, bare, head, cut_at

    @staticmethod
    def consumer_head_short(work: Path) -> str:
        return git("rev-parse", "--short", "HEAD", cwd=work)

    def retain_merged_branch(self, bare: Path, work: Path, head: str) -> str:
        branch = f"auto/contract-sync-{self.be8}"
        git("push", "-q", "origin", f"{head}:refs/heads/{branch}", cwd=work)
        if git("--git-dir", str(bare), "rev-parse", f"refs/heads/{branch}", cwd=str(self.root)) != head:
            raise RuntimeError("fixture: retained branch head does not match the merged head")
        return branch

    def run_sync(self, consumers: str, curl_timeout: int = 2, extra_env: dict | None = None):
        env = os.environ.copy()
        # The orchestration runs git and the consumer generator in the clone, so
        # it inherits the same isolated config and fixed clock as the fixture.
        env.update(GIT_ENV)
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

    def checkpoint(self, name: str = "last-be-sha") -> str:
        try:
            return (self.state / name).read_text().strip()
        except OSError:
            return ""

    def seed_checkpoint(self, sha: str) -> None:
        (self.state / "last-be-sha").write_text(sha + "\n")

    def refs(self, bare: Path) -> str:
        return git("--git-dir", str(bare), "for-each-ref", "--format=%(refname) %(objectname)",
                   cwd=str(self.root))

    def path_bytes(self, bare: Path, relative: str) -> bytes | None:
        run = subprocess.run(
            ["git", "--git-dir", str(bare), "show", f"main:{relative}"],
            cwd=str(self.root), capture_output=True,
        )
        return run.stdout if run.returncode == 0 else None

    def merged_pr(self, repo: str, number: int, branch: str, head: str, *,
                  base_ref: str = "main", merged: bool = True,
                  head_repo: str | None = None, body_sha: str | None = None) -> dict:
        """A Gitea pull as the API returns it, including base/head repo identity."""
        return {
            "number": number,
            "state": "closed",
            "merged": merged,
            "merged_at": "2026-10-16T04:20:00Z" if merged else None,
            "merge_commit_sha": head,
            "title": f"chore: sync backend contracts to gen-backend-v2 {self.be[:8]}",
            "body": f"Synced from gen-backend-v2 {body_sha or self.be}.\n",
            "base": {"ref": base_ref, "sha": head, "repo": _identity(repo)},
            "head": {"ref": branch, "sha": head, "repo": _identity(head_repo or repo)},
            "html_url": f"http://mock.invalid/{repo}/pulls/{number}",
            "_repo": repo,
        }

    def assert_no_writes(self, bare: Path, before_refs: str, repo: str) -> None:
        assert self.refs(bare) == before_refs, "a retry must not push, move or delete a ref"
        scoped = self.forge.scoped_writes(repo)
        assert scoped == [], f"a retry must not send any remote write: {scoped}"


# Infrastructure failures also end in exit 1; a refusal case is only meaningful
# when the run reached the consumer's disposition instead.
REFUSAL_NOISE = (
    "cannot clone", "required tool not found", "regeneration failed",
    "no regeneration command", "unknown FIXTURE", "no route",
    "cannot read the working tree", "cannot stage", "cannot commit",
)


def assert_real_refusal(run) -> None:
    text = run.stdout + run.stderr
    for noise in REFUSAL_NOISE:
        assert noise not in text, f"refusal is an infrastructure error, not a disposition: {noise!r}\n{text}"


@pytest.fixture(autouse=True)
def harness_tools():
    # The orchestration refuses to run without these, and that refusal is an
    # exit 1 that would otherwise read as a legitimate refusal.
    missing = [tool for tool in ("bash", "curl", "git", "cmp") if shutil.which(tool) is None]
    if missing:
        raise RuntimeError(f"the contract-sync harness needs {missing} on PATH")


@pytest.fixture()
def fx(tmp_path):
    fixture = Fixture(tmp_path)
    yield fixture
    fixture.forge.close()


def sheet_bytes(bare: Path) -> bytes:
    return subprocess.run(
        ["git", "--git-dir", str(bare), "show", f"main:{SHEET_PATH}"],
        cwd=str(bare.parent), check=True, capture_output=True,
    ).stdout


def assert_clone_changed_paths(fx: Fixture, expected: set[str]) -> None:
    """The regenerated clone must differ in exactly the ``expected`` paths.

    A cache file, a host-dependent git output or an unobservable clone is a
    fixture fault, not the behaviour under test, so it raises instead of
    asserting: a strict xfail would otherwise absorb it as the expected failure.
    """
    clone = fx.state / CONSUMER
    if not (clone / ".git").exists():
        raise RuntimeError("fixture: the retry never cloned the consumer")
    # NUL-separated so a leading status column or a path with a space parses
    # exactly; the shared git helper strips the leading space of " M path".
    out = subprocess.run(
        ["git", "status", "--porcelain", "-z"], cwd=clone, check=True,
        env={**os.environ, **GIT_ENV}, capture_output=True, text=True,
    ).stdout
    actual = {row[3:] for row in out.split("\0") if row}
    if actual != expected:
        raise RuntimeError(
            f"fixture: the regenerated clone changed {sorted(actual)}, expected {sorted(expected)}"
        )


def retry_case(fx: Fixture, stamp_mode: str = "consumer-head",
               sheet: bool = True) -> tuple[Path, Path, str, str, str]:
    """Consumer main + retained merged branch head, with the provenance checkpoint set."""
    work, bare, head, cut_at = fx.mcp_consumer(sheet, stamp_mode)
    branch = fx.retain_merged_branch(bare, work, head)
    fx.seed_checkpoint(fx.be_old)
    return work, bare, head, branch, cut_at


def merge_pr_for(fx: Fixture, branch: str, head: str, **kwargs) -> dict:
    return fx.merged_pr(CONSUMER, 606, branch, head, **kwargs)


# -----------------------------------------------------------------------------
# T1 -- regeneration is identical, so the retry is a plain no-op.
# -----------------------------------------------------------------------------


def test_t1_merged_consumer_with_identical_regeneration_is_a_no_op(fx):
    # intent: a lost checkpoint replay of an already merged, fully-converged
    # consumer succeeds without a new push, PR, merge or branch deletion.
    # The consumer here is the content-only generator (no volatile stamp cell),
    # so re-running it reproduces the committed sheet byte for byte.
    content = {"FIXTURE_STAMP_MODE": "content"}
    _, bare, head, branch, _ = retry_case(fx, stamp_mode="content")
    fx.forge.reset([merge_pr_for(fx, branch, head)])
    before_refs, before_bytes = fx.refs(bare), sheet_bytes(bare)

    run = fx.run_sync(CONSUMER, extra_env=content)

    assert_clone_changed_paths(fx, set())
    assert run.returncode == 0, run.stdout + run.stderr
    fx.assert_no_writes(bare, before_refs, CONSUMER)
    assert sheet_bytes(bare) == before_bytes
    assert fx.checkpoint() == fx.be, "the consumer reached its terminal success"
    assert fx.checkpoint("last-mcp-sha"), "the MCP source is recorded with the backend source"


# -----------------------------------------------------------------------------
# T2 -- the only regenerated difference is the declared served_sha stamps.
# -----------------------------------------------------------------------------


def test_t2_merged_consumer_with_stamp_only_diff_converges(fx):
    # intent: a retained branch whose PR is merged with this exact head/base and
    # input provenance is already satisfied even when regeneration re-stamps
    # served_sha A->M; the retry must not push, open a PR, merge, delete or edit,
    # and must not rewrite an ordinary cell that repeats the old stamp text.
    _, bare, head, branch, old_stamp = retry_case(fx)
    fx.forge.reset([merge_pr_for(fx, branch, head)])
    before_refs, before_bytes = fx.refs(bare), sheet_bytes(bare)
    new_stamp = head[:7]
    evidence = f"mirror cut at {old_stamp}".encode()
    if not old_stamp or old_stamp.encode() not in before_bytes:
        raise RuntimeError("fixture: the committed sheet does not carry the real old stamp")
    if evidence not in before_bytes:
        raise RuntimeError("fixture: no ordinary cell carries the old stamp text")
    if old_stamp == new_stamp:
        raise RuntimeError("fixture: the replay stamp did not move")

    run = fx.run_sync(CONSUMER)

    assert_clone_changed_paths(fx, {SHEET_PATH})
    assert run.returncode == 0, run.stdout + run.stderr
    fx.assert_no_writes(bare, before_refs, CONSUMER)
    assert sheet_bytes(bare) == before_bytes, "no file or real cell may be rewritten"
    assert evidence in sheet_bytes(bare), "ordinary cell text is not a declared stamp"
    assert fx.checkpoint() == fx.be


# -----------------------------------------------------------------------------
# T3 -- same stamp-only diff, but the PR was closed without merging.
# -----------------------------------------------------------------------------


def test_t3_closed_unmerged_pr_with_stamp_only_diff_refuses(fx):
    # intent: a closed, unmerged PR is not proof; the retry refuses with zero
    # writes even when the regenerated difference is only the declared stamp.
    _, bare, head, branch, _ = retry_case(fx)
    fx.forge.reset([merge_pr_for(fx, branch, head, merged=False)])
    before_refs, before_bytes = fx.refs(bare), sheet_bytes(bare)
    checkpoint = fx.checkpoint()

    run = fx.run_sync(CONSUMER)

    assert_clone_changed_paths(fx, {SHEET_PATH})
    assert run.returncode == 1, run.stdout + run.stderr
    assert_real_refusal(run)
    fx.assert_no_writes(bare, before_refs, CONSUMER)
    assert sheet_bytes(bare) == before_bytes
    assert fx.checkpoint() == checkpoint


# -----------------------------------------------------------------------------
# T4 -- the regeneration changes real content as well as the stamp.
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["enum-cell", "second-artifact"])
def test_t4_real_regenerated_change_refuses(fx, variant):
    # intent: only declared stamp cells are volatile; a changed tool cell or a
    # changed generated artifact keeps the consumer at refusal, preserves every
    # byte and never advances the checkpoint, even though the merge is proven.
    work, bare, head, branch, _ = retry_case(fx)
    # Main advances past the merged head with a real generated-content change, so
    # the merged commit stays an ancestor of main while regeneration now yields
    # more than a stamp difference.
    if variant == "enum-cell":
        sheet = work / SHEET_PATH
        text = sheet.read_text()
        stale = text.replace("mode: version: mirror-v1 | manual", "mode: STALE | manual")
        if stale == text:
            raise RuntimeError("fixture: the derived enum cell was not found to make stale")
        sheet.write_text(stale)
    else:
        sidecar = work / SIDECAR_PATH
        if not sidecar.exists():
            raise RuntimeError("fixture: the generated sidecar artifact is missing")
        sidecar.unlink()
    fx.commit_all(work, f"{variant} after merge")
    fx.push_main(work)
    fx.forge.reset([merge_pr_for(fx, branch, head)])
    before_refs, before_bytes = fx.refs(bare), sheet_bytes(bare)
    checkpoint = fx.checkpoint()

    run = fx.run_sync(CONSUMER)

    expected = {SHEET_PATH} if variant == "enum-cell" else {SHEET_PATH, SIDECAR_PATH}
    assert_clone_changed_paths(fx, expected)
    assert run.returncode == 1, run.stdout + run.stderr
    assert_real_refusal(run)
    fx.assert_no_writes(bare, before_refs, CONSUMER)
    assert sheet_bytes(bare) == before_bytes
    if variant == "second-artifact":
        assert fx.path_bytes(bare, SIDECAR_PATH) is None, "the missing artifact is not re-added remotely"
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
    assert fx.checkpoint("last-mcp-sha"), "the MCP source advances with the backend source"


# -----------------------------------------------------------------------------
# T6 -- identity and provenance must match exactly.
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    ["wrong-base", "wrong-repo", "wrong-head", "wrong-input", "head-changed-after-lookup", "not-ancestor"],
)
def test_t6_wrong_identity_or_provenance_refuses(fx, variant):
    # intent: a matching branch spelling is not proof; repo, base, head, input
    # sha and merged ancestry are all part of the evidence.
    work, bare, head, branch, _ = retry_case(fx)
    pr = merge_pr_for(fx, branch, head)
    if variant == "wrong-base":
        pr["base"]["ref"] = "develop"
    elif variant == "wrong-repo":
        # Same branch ref, but the head lives in a fork: not this consumer's merge.
        pr = fx.merged_pr(CONSUMER, 606, branch, head, head_repo="gen-mcp-server-fork")
    elif variant == "wrong-head":
        pr["head"]["sha"] = "1" * 40
    elif variant == "wrong-input":
        # Same branch name, different 40-character input provenance (prefix collision).
        pr["body"] = "Synced from gen-backend-v2 " + "c" * 40 + ".\n"
    elif variant == "head-changed-after-lookup":
        # The branch moves between the PR evidence and the Git ref read. The
        # override is installed after ``fx.forge.reset`` because reset clears
        # ``branch_overrides``; installed before it, the case proves nothing.
        pass
    else:  # not-ancestor: the merged commit is not in current main's history
        git("checkout", "-q", "--detach", git("rev-parse", "main", cwd=work), cwd=work)
        (work / "DIVERGENT").write_text("x\n")
        divergent = fx.commit_all(work, "divergent")
        git("push", "-q", "origin", f"{divergent}:refs/heads/{branch}", cwd=work)
        git("checkout", "-q", "main", cwd=work)
        pr = merge_pr_for(fx, branch, divergent)
        head = divergent
    fx.forge.reset([pr])
    if variant == "head-changed-after-lookup":
        fx.forge.branch_overrides[(CONSUMER, branch)] = "2" * 40
        moved = fx.forge.branch_head(CONSUMER, branch)
        if fx.forge.branch_overrides.get((CONSUMER, branch)) != "2" * 40 or moved != "2" * 40:
            raise RuntimeError(
                "fixture: the moved branch is not in effect after the PR reset "
                f"(override={fx.forge.branch_overrides.get((CONSUMER, branch))!r}, "
                f"branch_head={moved!r}); this case would prove nothing"
            )
    before_refs, before_bytes = fx.refs(bare), sheet_bytes(bare)
    checkpoint = fx.checkpoint()

    run = fx.run_sync(CONSUMER)

    assert_clone_changed_paths(fx, {SHEET_PATH})
    assert run.returncode == 1, run.stdout + run.stderr
    assert_real_refusal(run)
    fx.assert_no_writes(bare, before_refs, CONSUMER)
    assert sheet_bytes(bare) == before_bytes
    assert fx.checkpoint() == checkpoint


# -----------------------------------------------------------------------------
# T7 -- unavailable or malformed evidence is never convergence.
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    ["pr-403", "pr-timeout", "pr-malformed", "catalog-dup-header", "catalog-missing-header",
     "catalog-truncated-row"],
)
def test_t7_unavailable_or_malformed_evidence_refuses(fx, variant):
    # intent: an API error, a timeout, a malformed PR body or a malformed
    # catalog sheet refuses within the existing bounded harness, writes nothing
    # and never prints the token.
    _, bare, head, branch, _ = retry_case(fx)
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
        extra_env = {"FIXTURE_CATALOG_MODE": variant[len("catalog-"):]}
    fx.forge.reset([pr], mode=mode)
    before_refs, before_bytes = fx.refs(bare), sheet_bytes(bare)
    checkpoint = fx.checkpoint()

    run = fx.run_sync(CONSUMER, curl_timeout=1, extra_env=extra_env)

    assert run.returncode == 1, run.stdout + run.stderr
    assert_real_refusal(run)
    fx.assert_no_writes(bare, before_refs, CONSUMER)
    assert sheet_bytes(bare) == before_bytes
    assert fx.checkpoint() == checkpoint
    if extra_env:
        # The generator exited 0 and wrote the broken artifact into the clone,
        # so the refusal comes from the malformed sheet and not from a crash.
        clone_sheet = fx.state / CONSUMER / SHEET_PATH
        if not clone_sheet.exists():
            raise RuntimeError("fixture: the malformed artifact never reached the consumer clone")
        rows = [line for line in clone_sheet.read_text().split("\n") if line and not line.startswith("#")]
        header = next(line for line in rows if line.startswith("tool_name\t"))
        if variant == "catalog-dup-header":
            assert header.count("served_sha") == 2
        elif variant == "catalog-missing-header":
            assert "served_sha" not in header
        else:
            assert any(row.count("\t") < header.count("\t") for row in rows if not row.startswith("tool_name\t"))
    assert TOKEN not in run.stdout + run.stderr + json.dumps(fx.forge.writes)


# -----------------------------------------------------------------------------
# T8 -- a sibling failure keeps the global checkpoint put.
# -----------------------------------------------------------------------------


def test_t8_sibling_failure_holds_the_global_checkpoint(fx):
    # intent: the global checkpoint advances only after every consumer is
    # terminal; a sibling failure replays this consumer without duplicate writes.
    _, bare, head, branch, _ = retry_case(fx)
    fx.forge.reset([merge_pr_for(fx, branch, head)])
    before_refs, before_bytes = fx.refs(bare), sheet_bytes(bare)

    first = fx.run_sync(f"{CONSUMER} gen-agentic")
    assert first.returncode == 1, first.stdout + first.stderr
    assert "cannot clone" in first.stdout, "the sibling consumer failed before any merge"
    # The merged consumer itself reached success before the sibling failed.
    assert re.search(rf"{CONSUMER}: ERROR", first.stdout) is None, first.stdout
    assert re.search(rf"{CONSUMER}: ", first.stdout) is not None, first.stdout
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
    assert fx.checkpoint("last-mcp-sha"), "both source checkpoints advance together"
    assert fx.refs(bare) == before_refs, "no duplicate write to the already merged consumer"
    assert sheet_bytes(bare) == before_bytes
    assert fx.forge.mutations(CONSUMER) == []
