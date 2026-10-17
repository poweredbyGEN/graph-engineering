"""GEN-10539 shallow-ancestry cases (T11/T12) for a retained, already merged contract-sync branch.

The subject under test is the committed ancestry helper ``merged_head_is_in_main``
in ``ops/contract-sync/contract-sync.sh``. The test does not reimplement ancestry
logic: it extracts every top-level shell function from the committed bytes
(``name() {`` through the closing ``}`` at column 0) and runs the helper and the
real ``clone_repo`` in a bounded ``bash`` subprocess. Only the Git repositories
are local, so no network, Gitea, Docker or production state is touched.

Every fixture is real Git. A bare origin holds a nonlinear main history: a
retained ``auto/contract-sync-*`` branch head is merged into ``main`` by a merge
commit, and ``main`` advances past it. The consumer clone is made by the shipped
``clone_repo`` (``--depth 1 --single-branch`` on the consumer branch), which is
the state the natural retry starts from.

Fixture setup proves the defect shape without asking the helper anything: on a
throwaway copy of that clone, the historical two-ref relative deepen
(``fetch --deepen=<depth> origin main branch``) leaves the checked-out commit a
shallow root and cannot see the retained merge head, while a full clone of the
same origin proves the merge head is an ancestor of current main. Setup
violations raise ``RuntimeError`` so a strict-xfail marker cannot mask them.

T11 is the repair case and is the only genuinely red assertion: the shipped
helper must reconstruct current-main history to the configured finite depth and
let a real ``merge-base`` prove the retained merge head. T12 are the refusal and
control cases that already hold and must keep holding: unrelated claimed merge,
unreachable commit, and an ancestor beyond the configured depth all refuse with
zero remote writes, bounded fetches and the original current HEAD.

# intent: a retained branch merged into consumer main must be proven by the
# shared bounded ancestry helper, never by the branch name, API metadata or an
# unbounded fetch; a claimed merge that is unrelated, absent or deeper than the
# configured bound must still refuse.

Set ``CONTRACT_SYNC_SCRIPT_PATH`` to run the same cases against an isolated copy
of the script (sabotage check); the default is the committed path.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(
    os.environ.get(
        "CONTRACT_SYNC_SCRIPT_PATH",
        REPO_ROOT / "ops" / "contract-sync" / "contract-sync.sh",
    )
)

HELPER = "merged_head_is_in_main"
CONSUMER_BRANCH = "main"
SYNC_BRANCH = "auto/contract-sync-6f3c1d2e"
SIBLING_BRANCH = "auto/contract-sync-0a1b2c3d"
DELETED_BRANCH = "auto/contract-sync-deadbeef"
CONFIGURED_DEPTH = 50
GIT_TIMEOUT_SECONDS = "30"
ABSENT_OBJECT = "0123456789abcdef0123456789abcdef01234567"

# Deterministic, isolated git for every fixture: no user or system config
# (signing prompts, hooks, templates) and a fixed commit identity and time.
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "contract sync fixture",
    "GIT_AUTHOR_EMAIL": "fixture@contract-sync.invalid",
    "GIT_COMMITTER_NAME": "contract sync fixture",
    "GIT_COMMITTER_EMAIL": "fixture@contract-sync.invalid",
    "GIT_AUTHOR_DATE": "2026-10-16T06:00:00 +0000",
    "GIT_COMMITTER_DATE": "2026-10-16T06:00:00 +0000",
    "GIT_TERMINAL_PROMPT": "0",
}

DRIVER = """\
#!/usr/bin/env bash
set -uo pipefail
eval "$(cat "$GEN10539_FUNCTIONS")"
action="$1"
shift
case "$action" in
  clone) clone_repo "$@" ;;
  ancestry) merged_head_is_in_main "$@" ;;
  *) echo "unknown driver action: $action" >&2; exit 91 ;;
esac
"""

GIT_SHIM = """\
#!/usr/bin/env bash
for arg in "$@"; do printf '%s\\037' "$arg"; done >>"$GEN10539_GIT_LOG"
printf '\\n' >>"$GEN10539_GIT_LOG"
exec "$GEN10539_REAL_GIT" "$@"
"""

# Records the bound the helper passes to `timeout`, then enforces it when the
# host has coreutils. Hosts without `timeout` (macOS) drop only the duration;
# every fixture Git call is local, so nothing can block there.
TIMEOUT_SHIM = """\
#!/usr/bin/env bash
for arg in "$@"; do printf '%s\\037' "$arg"; done >>"$GEN10539_TIMEOUT_LOG"
printf '\\n' >>"$GEN10539_TIMEOUT_LOG"
if [ -n "${GEN10539_REAL_TIMEOUT:-}" ]; then
  exec "$GEN10539_REAL_TIMEOUT" "$@"
fi
shift
exec "$@"
"""


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env={**os.environ, **GIT_ENV, **(env or {})},
        check=check,
        capture_output=True,
        text=True,
    )


def git(*args: str, cwd: Path | None = None) -> str:
    return run(["git", *args], cwd=cwd).stdout.strip()


def git_status(*args: str, cwd: Path | None = None) -> int:
    return run(["git", *args], cwd=cwd, check=False).returncode


def extract_functions(script: Path) -> str:
    """Every top-level function of the committed script, verbatim.

    The helper runs from these bytes, so the cases exercise the shipped ancestry
    logic and any helper function it adds; nothing is copied or reimplemented.
    """
    lines = script.read_text(encoding="utf-8").splitlines(keepends=True)
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        if re.match(r"^[a-z_][a-z0-9_]*\(\) \{", lines[index]):
            end = index + 1
            while end < len(lines) and lines[end].rstrip("\n") != "}":
                end += 1
            if end == len(lines):
                raise RuntimeError(f"unterminated function at {script}:{index + 1}")
            blocks.append("".join(lines[index : end + 1]))
            index = end + 1
        else:
            index += 1
    defined = {block.split("(", 1)[0] for block in blocks}
    if HELPER not in defined:
        raise RuntimeError(f"{script} does not define {HELPER}")
    return "\n".join(blocks)


class Harness:
    """Runs the committed shell functions with a recording, isolated PATH."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir(parents=True)
        self.functions = root / "contract-sync-functions.sh"
        self.functions.write_text(extract_functions(SCRIPT), encoding="utf-8")
        self.git_log = root / "git-invocations.log"
        self.timeout_log = root / "timeout-invocations.log"
        self.real_git = shutil.which("git")
        self.real_timeout = shutil.which("timeout") or shutil.which("gtimeout")
        if not self.real_git:
            raise RuntimeError("git is required for the shallow-ancestry cases")
        for name, text in (("git", GIT_SHIM), ("timeout", TIMEOUT_SHIM)):
            shim = self.bin / name
            shim.write_text(text, encoding="utf-8")
            shim.chmod(0o755)
        self.driver = root / "call-contract-sync.sh"
        self.driver.write_text(DRIVER, encoding="utf-8")
        self.driver.chmod(0o755)

    def env(self, *, depth: int, state_dir: Path) -> dict[str, str]:
        return {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "GEN10539_FUNCTIONS": str(self.functions),
            "GEN10539_GIT_LOG": str(self.git_log),
            "GEN10539_TIMEOUT_LOG": str(self.timeout_log),
            "GEN10539_REAL_GIT": self.real_git,
            "GEN10539_REAL_TIMEOUT": self.real_timeout or "",
            "CONSUMER_BRANCH": CONSUMER_BRANCH,
            "GIT_TIMEOUT": GIT_TIMEOUT_SECONDS,
            "MERGE_ANCESTRY_DEPTH": str(depth),
            "STATE_DIR": str(state_dir),
        }

    def clone_consumer(self, origin: Path, dest: Path, *, depth: int) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        proc = run(
            [str(self.driver), "clone", f"file://{origin}", CONSUMER_BRANCH, str(dest)],
            env=self.env(depth=depth, state_dir=dest.parent),
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"clone_repo failed rc={proc.returncode}: {proc.stderr.strip()}"
            )

    def ancestry(
        self, clone: Path, branch: str, sha: str, *, depth: int
    ) -> subprocess.CompletedProcess[str]:
        return run(
            [str(self.driver), "ancestry", str(clone), branch, sha],
            env=self.env(depth=depth, state_dir=clone.parent),
            check=False,
        )

    def invocations(self, log: Path) -> list[list[str]]:
        if not log.exists():
            return []
        return [
            line.split("\037")[:-1]
            for line in log.read_text(encoding="utf-8").splitlines()
            if line
        ]


# -----------------------------------------------------------------------------
# Real-Git fixtures: nonlinear merged history, published to a bare origin.
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    harness: Harness
    origin: Path
    full: Path
    clone: Path
    branch: str
    claimed: str
    current_head: str
    depth: int
    origin_refs: str
    clone_state: tuple[str, str, str]

    def origin_now(self) -> str:
        return git("--git-dir", str(self.origin), "for-each-ref")

    def clone_now(self) -> tuple[str, str, str]:
        return (
            git("rev-parse", "HEAD", cwd=self.clone),
            git("rev-parse", f"refs/heads/{CONSUMER_BRANCH}", cwd=self.clone),
            git("rev-parse", f"refs/remotes/origin/{CONSUMER_BRANCH}", cwd=self.clone),
        )


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    git("add", "--", name, cwd=repo)
    git("commit", "-q", "-m", name, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


def init_source(root: Path) -> Path:
    source = root / "source"
    source.mkdir()
    git("init", "-q", "-b", CONSUMER_BRANCH, cwd=source)
    return source


def publish(root: Path, source: Path) -> tuple[Path, Path]:
    origin = root / "origin.git"
    git("clone", "-q", "--bare", str(source), str(origin))
    full = root / "full"
    git("clone", "-q", str(origin), str(full))
    return origin, full


def _merge(source: Path, branch: str) -> str:
    git("checkout", "-q", CONSUMER_BRANCH, cwd=source)
    git("merge", "-q", "--no-ff", "-m", f"Merge {branch}", branch, cwd=source)
    return git("rev-parse", "HEAD", cwd=source)


def merged_source(
    root: Path, *, before: int = 3, side: int = 2, after: int = 2, sibling: bool = False
) -> tuple[Path, str, str]:
    """main -> sides at the retained branch -> merge -> further main commits."""
    source = init_source(root)
    for index in range(before):
        commit(source, f"base-{index}.txt")
    fork = git("rev-parse", "HEAD", cwd=source)
    git("checkout", "-q", "-b", SYNC_BRANCH, fork, cwd=source)
    for index in range(side):
        commit(source, f"sync-{index}.txt")
    merged_head = git("rev-parse", "HEAD", cwd=source)
    _merge(source, SYNC_BRANCH)
    if sibling:
        # A second retained branch merges on top, so the claimed head sits in the
        # middle of current main's history behind a merge parent, not at its tip.
        git("checkout", "-q", "-b", SIBLING_BRANCH, "HEAD", cwd=source)
        commit(source, "sibling-0.txt")
        _merge(source, SIBLING_BRANCH)
    for index in range(after):
        commit(source, f"after-{index}.txt")
    return source, merged_head, git("rev-parse", "HEAD", cwd=source)


def unmerged_source(root: Path, *, delete_branch: bool) -> tuple[Path, str]:
    """A side branch that never merged, retained or deleted before publication."""
    source = init_source(root)
    for index in range(3):
        commit(source, f"base-{index}.txt")
    fork = git("rev-parse", "HEAD", cwd=source)
    branch = DELETED_BRANCH if delete_branch else SYNC_BRANCH
    git("checkout", "-q", "-b", branch, fork, cwd=source)
    commit(source, "side-0.txt")
    claimed = commit(source, "side-1.txt")
    git("checkout", "-q", CONSUMER_BRANCH, cwd=source)
    commit(source, "after-0.txt")
    if delete_branch:
        git("branch", "-q", "-D", branch, cwd=source)
    return source, claimed


def shallow_case(
    root: Path,
    *,
    before: int = 3,
    after: int = 2,
    sibling: bool = False,
    depth: int = CONFIGURED_DEPTH,
    branch: str = SYNC_BRANCH,
    claimed: str | None = None,
    deleted_branch: bool = False,
    unmerged: bool = False,
) -> Case:
    """Build the DAG, publish it, clone it the way ``clone_repo`` does.

    Raises ``RuntimeError`` on any violation of the intended fixture shape, so a
    strict-xfail marker can never hide a broken setup.
    """
    harness = Harness(root / "harness")
    if unmerged:
        source, merged_head = unmerged_source(root, delete_branch=deleted_branch)
    else:
        source, merged_head, _ = merged_source(
            root, before=before, after=after, sibling=sibling
        )
    origin, full = publish(root, source)
    current_head = git("rev-parse", f"refs/heads/{CONSUMER_BRANCH}", cwd=full)

    clone = root / "consumer"
    harness.clone_consumer(origin, clone, depth=depth)

    head = git("rev-parse", "HEAD", cwd=clone)
    shallow_file = clone / ".git" / "shallow"
    if head != current_head:
        raise RuntimeError(f"clone HEAD {head} is not current main {current_head}")
    if not shallow_file.exists():
        raise RuntimeError("clone_repo did not produce a shallow clone")
    if shallow_file.read_text(encoding="utf-8").split() != [current_head]:
        raise RuntimeError(
            "depth-1 clone did not stop at current main: "
            f"{shallow_file.read_text(encoding='utf-8').split()}"
        )
    if git("rev-list", "--count", "HEAD", cwd=clone) != "1":
        raise RuntimeError("depth-1 clone carries more than the checked-out commit")

    claimed = claimed or merged_head
    if not unmerged:
        if git_status("cat-file", "-e", f"{claimed}^{{commit}}", cwd=full):
            # The absent-object claim is expected not to resolve; T12 asserts that.
            pass
        elif git_status("merge-base", "--is-ancestor", claimed, current_head, cwd=full):
            raise RuntimeError(
                f"{claimed} is not an ancestor of {current_head} in full"
            )
        else:
            distance = int(
                git("rev-list", "--count", f"{claimed}..{current_head}", cwd=full)
            )
            expected_within = depth >= distance
            if expected_within and distance > depth:
                raise RuntimeError(
                    "accepted fixture is not within the configured depth"
                )
            if not expected_within and distance <= depth:
                raise RuntimeError(
                    "beyond-depth fixture is within the configured depth"
                )

    assert_historic_two_ref_deepen_cannot_prove(
        root, origin, clone, branch, depth, claimed
    )

    return Case(
        harness=harness,
        origin=origin,
        full=full,
        clone=clone,
        branch=branch,
        claimed=claimed,
        current_head=current_head,
        depth=depth,
        origin_refs=git("--git-dir", str(origin), "for-each-ref"),
        clone_state=(
            git("rev-parse", "HEAD", cwd=clone),
            git("rev-parse", f"refs/heads/{CONSUMER_BRANCH}", cwd=clone),
            git("rev-parse", f"refs/remotes/origin/{CONSUMER_BRANCH}", cwd=clone),
        ),
    )


def assert_historic_two_ref_deepen_cannot_prove(
    root: Path,
    origin: Path,
    clone: Path,
    branch: str,
    depth: int,
    claimed: str,
) -> str:
    """Record what the historical two-ref relative deepen does with this fixture.

    The fixture shape assertions (depth-1 clone, current HEAD, full-clone ancestry,
    within/beyond depth) already ran. This probe only reports whether the historical
    command can prove the claim: git keeps the checked-out commit a shallow root when
    a second ref is fetched (the production defect), while older git releases deepen
    through it. The helper's own verdict is asserted in the test body, so a git
    release that already proves the historical command is a recorded fact about the
    fixture, never a fixture violation.

    A fixture that deleted the branch before publication cannot be probed at all:
    the historical command cannot resolve the ref, which is the shape under test.
    """
    if git_status(
        "--git-dir", str(origin), "show-ref", "--verify", f"refs/heads/{branch}"
    ):
        return "no-retained-branch"
    probe = root / "probe"
    shutil.copytree(clone, probe)
    proc = run(
        [
            "git",
            "-C",
            str(probe),
            "fetch",
            "--quiet",
            f"--deepen={depth}",
            "origin",
            f"+refs/heads/{CONSUMER_BRANCH}:refs/remotes/origin/{CONSUMER_BRANCH}",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        ],
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"two-ref relative deepen failed: {proc.stderr.strip()}")
    observed = (
        "proved"
        if git_status("merge-base", "--is-ancestor", claimed, "HEAD", cwd=probe) == 0
        else "shallow-root"
    )
    (root / "historic-deepen").write_text(f"{observed}\n", encoding="utf-8")
    return observed


# -----------------------------------------------------------------------------
# Observations on what the helper did.
# -----------------------------------------------------------------------------


def fetch_arguments(harness: Harness) -> list[list[str]]:
    return [args for args in harness.invocations(harness.git_log) if "fetch" in args]


def timeout_arguments(harness: Harness) -> list[list[str]]:
    return harness.invocations(harness.timeout_log)


def assert_bounded_and_side_effect_free(
    case: Case, proc: subprocess.CompletedProcess
) -> None:
    """Bounded fetch, exiting bounds, no remote writes, unchanged local head."""
    fetches = fetch_arguments(case.harness)
    assert fetches, f"helper did not fetch main history: {proc.stderr.strip()}"
    assert len(fetches) <= 3, f"helper fetched {len(fetches)} times: {fetches}"
    for args in fetches:
        assert "--unshallow" not in args, f"unbounded unshallow: {args}"
        bounds = [
            int(match.group(1))
            for arg in args
            if (match := re.fullmatch(r"--(?:depth|deepen)=(\d+)", arg))
        ]
        assert bounds, f"fetch without a depth bound: {args}"
        assert all(0 < bound <= case.depth + 1 for bound in bounds), (
            f"fetch bound outside the configured depth: {args}"
        )
    assert not [
        args
        for args in case.harness.invocations(case.harness.git_log)
        if "push" in args
    ], "helper attempted a remote write"
    assert case.origin_now() == case.origin_refs, "origin refs changed"
    assert case.clone_now() == case.clone_state, "local HEAD or tracking refs changed"
    boots = [
        args
        for args in timeout_arguments(case.harness)
        if args and args[0] == GIT_TIMEOUT_SECONDS and "fetch" in args
    ]
    assert boots, "helper did not run its fetch under the configured git timeout"


# -----------------------------------------------------------------------------
# T11: the merged head must be proven through bounded current-main history.
# -----------------------------------------------------------------------------


def test_t11_merged_head_is_proven_through_bounded_main_history(tmp_path: Path) -> None:
    """Nonlinear DAG, retained merged branch, depth-1 consumer clone, depth reaches it."""
    case = shallow_case(tmp_path)

    proc = case.harness.ancestry(
        case.clone, case.branch, case.claimed, depth=case.depth
    )

    assert proc.returncode == 0, (
        f"bounded ancestry reconstruction refused a real merged head: "
        f"rc={proc.returncode} stderr={proc.stderr.strip()}"
    )
    assert not git_status(
        "merge-base", "--is-ancestor", case.claimed, "HEAD", cwd=case.clone
    ), "helper accepted without leaving provable ancestry in the clone"
    assert_bounded_and_side_effect_free(case, proc)


def test_t11_sibling_control_proves_within_depth_merged_head(tmp_path: Path) -> None:
    """T12 positive control: a second retained merge inside the configured depth."""
    case = shallow_case(tmp_path, sibling=True)

    proc = case.harness.ancestry(
        case.clone, case.branch, case.claimed, depth=case.depth
    )

    assert proc.returncode == 0, (
        f"within-depth merged head refused: rc={proc.returncode} "
        f"stderr={proc.stderr.strip()}"
    )
    assert_bounded_and_side_effect_free(case, proc)


# -----------------------------------------------------------------------------
# T12: refusals that must hold before and after the repair.
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "depth"),
    [
        pytest.param("unrelated", CONFIGURED_DEPTH, id="unrelated-claim"),
        pytest.param("unreachable", CONFIGURED_DEPTH, id="deleted-branch-commit"),
        pytest.param("absent-object", CONFIGURED_DEPTH, id="absent-object"),
        pytest.param("beyond-depth", 4, id="beyond-configured-depth"),
    ],
)
def test_t12_unprovable_claimed_ancestry_refuses(
    tmp_path: Path, kind: str, depth: int
) -> None:
    """Each unprovable claim returns 1 with no write, no local mutation, bounded fetch."""
    if kind == "unrelated":
        case = shallow_case(tmp_path, unmerged=True)
    elif kind == "unreachable":
        case = shallow_case(tmp_path, unmerged=True, deleted_branch=True)
    elif kind == "absent-object":
        case = shallow_case(tmp_path, claimed=ABSENT_OBJECT)
    else:
        case = shallow_case(tmp_path, after=6, depth=depth)

    if kind == "absent-object" and not git_status(
        "cat-file", "-e", f"{case.claimed}^{{commit}}", cwd=case.full
    ):
        raise RuntimeError("absent-object fixture resolves in the full clone")
    if kind in {"unrelated", "unreachable"} and not git_status(
        "merge-base",
        "--is-ancestor",
        case.claimed,
        case.current_head,
        cwd=case.full,
    ):
        raise RuntimeError("refusal fixture claims a real ancestor")

    proc = case.harness.ancestry(
        case.clone, case.branch, case.claimed, depth=case.depth
    )

    assert proc.returncode == 1, (
        f"{kind} claim was not refused: rc={proc.returncode} "
        f"stdout={proc.stdout.strip()} stderr={proc.stderr.strip()}"
    )
    assert_bounded_and_side_effect_free(case, proc)
    assert git_status(
        "merge-base", "--is-ancestor", case.claimed, "HEAD", cwd=case.clone
    ), f"{kind} refusal left the claimed merge provable in the clone"
