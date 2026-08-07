"""swarm — fan out independent work, and let no lane claim success without evidence.

WHY THIS EXISTS
Parallelism does not improve quality on its own. Running N agents on one problem with no
evidence contract just produces N confident wrong answers faster. What raises quality is:

  1. each lane must PROVE it finished (its own evidence loop, run by a subprocess it does
     not control), and
  2. a claim that survives an adversarial pass is worth more than one that was never
     challenged.

So this runner does three things and refuses to skip any of them: isolate each lane, gate it
on evidence, then optionally try to REFUTE the lanes that passed.

Isolation is a git worktree per lane. Two agents editing one checkout is the classic swarm
failure — they clobber each other and the evidence becomes meaningless because you cannot tell
whose change broke what.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shutil
import subprocess
import threading
import sys
import time
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class LaneResult:
    name: str
    passed: bool
    exit_code: int
    duration_sec: float
    worktree: str | None = None
    branch: str | None = None
    output: str = ""
    verdicts: list[dict] = field(default_factory=list)  # adversarial refutation attempts

    # Declared data contract, echoed into the trace so the FAKE EDGE TEST can be computed
    # instead of performed by hand. An edge from A to B is real only when B CONSUMES
    # something A PRODUCES. Without these, a trace records only that lanes ran, and the
    # best any analyzer can do is correlate outcomes -- which two lanes hitting one flaky
    # check will do perfectly while having no dependency at all.
    produces: list[str] = field(default_factory=list)
    consumes: list[str] = field(default_factory=list)

    @property
    def confirmed(self) -> bool:
        """Passed evidence AND survived refutation.

        A lane with no verifiers is `passed` but never `confirmed` — the distinction is the
        point. Reporting unverified work as confirmed is how a swarm launders a guess into a
        result.
        """
        if not self.passed or not self.verdicts:
            return False
        return sum(1 for v in self.verdicts if v.get("refuted")) * 2 <= len(self.verdicts)


def _run(cmd: list[str], cwd: Path, timeout: int, env: dict | None = None) -> tuple[int, str]:
    """Run a command; a non-zero exit is data, not an exception."""
    full_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=full_env)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, f"TIMEOUT after {timeout}s: {' '.join(cmd)}"
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]!r}"


# `git worktree add` mutates shared state under .git/worktrees/ and is NOT thread-safe.
# Caught live in e2e: four concurrent lanes produced
#   fatal: failed to read .git/worktrees/beta/commondir: Success
# as one lane read another's half-written metadata. Creation is serialized; the lanes
# themselves still run fully in parallel, and setup is milliseconds against minutes of work.
_WORKTREE_LOCK = threading.Lock()


def make_worktree(repo: Path, lane: str, base: str, root: Path) -> tuple[Path, str] | None:
    """Create an isolated worktree+branch for one lane. Returns None if git refuses.

    Nested under the repo per the worktree convention, not as a sibling directory.
    """
    branch = f"swarm/{lane}"
    wt = root / lane
    with _WORKTREE_LOCK:
        code, out = _run(["git", "worktree", "add", "-b", branch, str(wt), base], repo, 120)
    if code != 0:
        # Most common cause: the branch already exists from a previous run. Surface it rather
        # than silently reusing a stale tree whose contents we cannot vouch for.
        print(f"  [{lane}] worktree failed: {out.strip()[:160]}", file=sys.stderr)
        return None
    return wt, branch


def remove_worktree(repo: Path, wt: Path, branch: str, keep: bool) -> None:
    if keep:
        return
    _run(["git", "worktree", "remove", "--force", str(wt)], repo, 120)
    _run(["git", "branch", "-D", branch], repo, 60)


def run_lane(lane: dict, cfg: dict, repo: Path, wt_root: Path, keep: bool) -> LaneResult:
    """One lane: isolate → run the agent → gate on evidence.

    The lane's own exit code is deliberately NOT the verdict. An agent that exits 0 having done
    nothing must not pass; only the evidence command decides.
    """
    name = lane["name"]
    timeout = int(cfg.get("limits", {}).get("timeout_sec", 1800))
    start = time.monotonic()
    wt, branch, cwd = None, None, repo

    if cfg.get("isolate", True):
        made = make_worktree(repo, name, cfg.get("base", "HEAD"), wt_root)
        if made is None:
            return LaneResult(name, False, 1, time.monotonic() - start,
                              output="could not create an isolated worktree")
        wt, branch = made
        cwd = wt

    out_parts = []
    agent_cmd = [a.replace("{task}", lane["task"]).replace("{name}", name)
                 for a in cfg["agent"]["cmd"]]
    code, out = _run(agent_cmd, cwd, timeout)
    out_parts.append(f"--- agent (exit {code}) ---\n{out[-4000:]}")

    # Evidence decides, not the agent's exit code.
    passed, ev_code = True, 0
    for chk in cfg.get("checks", []):
        c, o = _run(chk["cmd"], cwd, timeout)
        out_parts.append(f"--- check {chk['name']} (exit {c}) ---\n{o[-4000:]}")
        if c != 0:
            passed, ev_code = False, c
    if not cfg.get("checks"):
        # No evidence configured means nothing can be proven. Fail closed: a swarm that
        # reports success without checks is worse than one that reports nothing.
        passed, ev_code = False, 2
        out_parts.append("--- no [[checks]] configured: cannot verify, failing closed ---")

    r = LaneResult(name, passed, ev_code, time.monotonic() - start,
                   str(wt) if wt else None, branch, "\n\n".join(out_parts),
                   produces=list(lane.get("produces", [])),
                   consumes=list(lane.get("consumes", [])))
    if wt and not passed:
        remove_worktree(repo, wt, branch, keep)  # keep only what might be merged
        r.worktree = None
    return r


def verify_lane(result: LaneResult, cfg: dict, repo: Path) -> LaneResult:
    """Adversarial pass: spawn verifiers that TRY TO REFUTE the lane's success.

    Prompted to refute rather than confirm, because a verifier asked "is this right?" tends to
    agree. Majority-refuted kills the claim.

    CONTEXT INDEPENDENCE, and why the DIFF is passed in.
    Each verifier is a separate process, so it does not inherit the worker's conversation --
    a verifier handed the worker's own reasoning is not checking anything, it is agreeing
    with the same chain of thought in a new window.

    Fresh context alone was not enough. Verifiers run in the worker's worktree, so the
    evidence they read is the defendant's own account: a lane that made its checks pass by
    WEAKENING them presents that weakened state as the world, and a verifier reading only
    the mutated tree cannot see what changed. So `{diff}` is substituted into the verifier
    command -- the worker's diff against the base, computed here rather than obtained from
    the worker. A verifier that can see the diff can catch a lane that edited its own tests,
    which is the single most likely way a lane fakes success.
    """
    vcfg = cfg.get("verify")
    if not vcfg or not result.passed:
        return result
    n = int(vcfg.get("count", 2))
    timeout = int(cfg.get("limits", {}).get("timeout_sec", 1800))
    cwd = Path(result.worktree) if result.worktree else repo

    # The worker's diff against the base, computed HERE rather than taken from the worker.
    # Truncated: a verifier prompt that blows past the context window verifies nothing, and
    # a diff that large is itself worth flagging.
    base = cfg.get("base", "HEAD")
    diff = ""
    if result.worktree:
        dc, dout = _run(["git", "diff", f"{base}...HEAD"], Path(result.worktree), 120)
        if dc == 0 and dout.strip():
            diff = dout if len(dout) <= 20_000 else dout[:20_000] + "\n…[diff truncated]…"
        elif dc == 0:
            diff = "(no changes against base — the lane passed without editing anything)"

    def one(i: int) -> dict:
        cmd = [a.replace("{lane}", result.name).replace("{n}", str(i)).replace("{diff}", diff)
               for a in vcfg["cmd"]]
        code, out = _run(cmd, cwd, timeout)
        # Non-zero from a refuter means "I found a problem" — refuted.
        return {"verifier": i, "refuted": code != 0, "exit_code": code, "output": out[-2000:]}

    with cf.ThreadPoolExecutor(max_workers=min(n, 4)) as ex:
        result.verdicts = list(ex.map(one, range(1, n + 1)))
    return result


def load_config(path: Path) -> dict:
    with path.open("rb") as fh:
        cfg = tomllib.load(fh)
    if not cfg.get("lanes"):
        raise SystemExit(f"{path}: no [[lanes]] defined")
    if not (cfg.get("agent") or {}).get("cmd"):
        raise SystemExit(f"{path}: [agent] cmd is required")
    seen = set()
    for lane in cfg["lanes"]:
        n = lane.get("name")
        if not n or not lane.get("task"):
            raise SystemExit(f"{path}: every lane needs a name and a task")
        if n in seen:
            # Duplicate names would collide on branch and worktree path — the exact
            # one-agent-per-unit rule that keeps a swarm from thrashing.
            raise SystemExit(f"{path}: duplicate lane name {n!r}")
        seen.add(n)
        if any(c in n for c in "/\\ .~^:?*["):
            raise SystemExit(f"{path}: lane name {n!r} is not usable as a git branch")
        for key in ("produces", "consumes"):
            if key in lane and not isinstance(lane[key], list):
                raise SystemExit(f"{path}: lane {n!r} {key} must be a list of strings")

    # FAKE EDGE detection at CONFIG time -- cheaper than discovering it from traces, and it
    # catches the case where someone declares a dependency that does not exist.
    produced = {p for lane in cfg["lanes"] for p in lane.get("produces", [])}
    for lane in cfg["lanes"]:
        for c in lane.get("consumes", []):
            if c not in produced:
                raise SystemExit(
                    f"{path}: lane {lane['name']!r} consumes {c!r}, which no lane produces. "
                    f"Either a lane is missing, or this is a FAKE EDGE — a dependency that "
                    f"was assumed rather than real.")
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description="Fan out lanes; gate each on evidence.")
    ap.add_argument("--config", default=".swarm.toml")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--max-parallel", type=int, default=0, help="0 = min(8, cpus-2)")
    ap.add_argument("--keep-worktrees", action="store_true")
    ap.add_argument("--trace")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    repo = Path(a.repo).resolve()
    cfg = load_config(Path(a.config))
    lanes = cfg["lanes"]
    cap = a.max_parallel or min(8, max(1, (os.cpu_count() or 4) - 2))

    if a.dry_run:
        print(f"repo: {repo}\nlanes: {len(lanes)}  parallel: {cap}")
        for lane in lanes:
            print(f"  - {lane['name']}: {lane['task'][:70]}")
        print(f"  checks: {[c['name'] for c in cfg.get('checks', [])] or 'NONE (fails closed)'}")
        print(f"  verify: {(cfg.get('verify') or {}).get('count', 0)} refuters per passing lane")
        return 0

    wt_root = Path(cfg.get("worktree_root", repo / ".swarm-worktrees")).resolve()
    wt_root.mkdir(parents=True, exist_ok=True)
    print(f"swarm: {len(lanes)} lanes, {cap} at a time, repo={repo}\n", flush=True)

    results: list[LaneResult] = []
    with cf.ThreadPoolExecutor(max_workers=cap) as ex:
        futs = {ex.submit(run_lane, lane, cfg, repo, wt_root, a.keep_worktrees): lane
                for lane in lanes}
        for fut in cf.as_completed(futs):
            r = fut.result()
            r = verify_lane(r, cfg, repo)
            results.append(r)
            mark = "CONFIRMED" if r.confirmed else ("PASS" if r.passed else "FAIL")
            extra = ""
            if r.verdicts:
                ref = sum(1 for v in r.verdicts if v["refuted"])
                extra = f" [{len(r.verdicts) - ref}/{len(r.verdicts)} verifiers upheld]"
            print(f"  {mark:9} {r.name} ({r.duration_sec:.0f}s){extra}", flush=True)

    ok = [r for r in results if r.passed]
    conf = [r for r in results if r.confirmed]
    print(f"\n{len(ok)}/{len(results)} passed evidence"
          + (f"; {len(conf)} survived refutation" if cfg.get("verify") else ""))
    for r in results:
        if r.passed and r.worktree:
            print(f"  {r.name}: {r.branch} at {r.worktree}")

    if a.trace:
        Path(a.trace).write_text(json.dumps([asdict(r) for r in results], indent=2) + "\n")
        print(f"trace: {a.trace}")

    if not a.keep_worktrees and wt_root.exists() and not any(wt_root.iterdir()):
        shutil.rmtree(wt_root, ignore_errors=True)

    # Non-zero unless every lane cleared the highest bar the config asked for.
    bar = conf if cfg.get("verify") else ok
    return 0 if len(bar) == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
