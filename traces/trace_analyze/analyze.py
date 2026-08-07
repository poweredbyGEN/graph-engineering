"""trace-analyze — read run traces and answer questions you'd otherwise guess at.

WHY THIS EXISTS
Both runners emit `--trace` JSON and nothing read it, so the interesting questions stayed
opinions: are our checks any good? is the agent converging or thrashing? do we need a graph?

Each of those has an answer sitting in the traces:

  - A check that has NEVER failed is not evidence. It either tests nothing, or tests something
    that cannot break. Either way it costs time every run and catches nothing.
  - A check that fails and then passes on the next attempt is doing real work — it caught
    something and drove a fix.
  - Repeated identical failures mean the agent is stuck, not working.
  - Lanes that always pass together, or always fail together, are not independent — that is
    the shape of a real dependency, and the first honest argument for a graph.

Reads both trace shapes: the loop's {goal, cwd, attempts[]} and the swarm's [lane, ...].
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CheckStats:
    name: str
    runs: int = 0
    failures: int = 0
    durations: list[float] = field(default_factory=list)
    caught: int = 0        # failed, then passed on a later attempt — it drove a fix
    timeouts: int = 0
    not_found: int = 0     # exit 127: the checker isn't installed

    @property
    def fail_rate(self) -> float:
        return self.failures / self.runs if self.runs else 0.0

    @property
    def median_sec(self) -> float:
        return statistics.median(self.durations) if self.durations else 0.0

    def verdict(self) -> tuple[str, str]:
        """(label, why). The judgement this whole module exists to produce."""
        if self.not_found:
            return "BROKEN", f"exit 127 in {self.not_found} run(s) — the checker isn't installed"
        if self.timeouts:
            return "SLOW", f"timed out {self.timeouts}×; a timeout reads as a failure it didn't earn"
        if self.failures == 0:
            return "NO SIGNAL", f"never failed in {self.runs} runs — costs {self.median_sec:.1f}s and catches nothing"
        if self.caught:
            return "EARNING", f"caught {self.caught} real defect(s) that a later attempt fixed"
        if self.fail_rate == 1.0 and self.runs > 2:
            return "ALWAYS RED", f"failed all {self.runs} runs — likely misconfigured, not finding bugs"
        return "ACTIVE", f"failed {self.failures}/{self.runs} runs"


def load(paths: list[Path]) -> tuple[list[dict], list[dict]]:
    """-> (loop runs, swarm runs). Distinguished by shape, not filename."""
    loops, swarms = [], []
    for p in paths:
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError) as e:
            print(f"  skipping {p}: {e}", file=sys.stderr)
            continue
        if isinstance(d, dict) and "attempts" in d:
            loops.append({**d, "_path": str(p)})
        elif isinstance(d, list) and d and "passed" in d[0]:
            swarms.append({"lanes": d, "_path": str(p)})
        else:
            print(f"  skipping {p}: unrecognized trace shape", file=sys.stderr)
    return loops, swarms


def analyze_checks(loops: list[dict]) -> dict[str, CheckStats]:
    stats: dict[str, CheckStats] = {}
    for run in loops:
        attempts = run.get("attempts", [])
        # A check "caught" something when it failed on attempt N and passed later in the SAME
        # run — evidence it drove a fix rather than merely complaining.
        for i, att in enumerate(attempts):
            for c in att.get("checks", []):
                s = stats.setdefault(c["name"], CheckStats(c["name"]))
                s.runs += 1
                s.durations.append(c.get("duration_sec", 0.0))
                failed = c.get("exit_code", 0) != 0 or c.get("timed_out")
                if c.get("timed_out"):
                    s.timeouts += 1
                if c.get("exit_code") == 127:
                    s.not_found += 1
                if failed:
                    s.failures += 1
                    later_pass = any(
                        lc["name"] == c["name"] and lc.get("exit_code") == 0
                        for la in attempts[i + 1:] for lc in la.get("checks", [])
                    )
                    if later_pass:
                        s.caught += 1
    return stats


def analyze_keep_rate(loops: list[dict], swarms: list[dict]) -> list[str]:
    """Did the loop EARN its cost? The metric that decides whether to keep running it.

    Every attempt sends the whole context back through the model, so a loop that runs ten
    times does not cost ten prompts -- it costs ten prompts that keep getting longer. The
    number that matters is therefore not tokens spent but how many runs you KEPT:

        keep rate = converged runs / total runs

    Below ~50% the loop is costing more than doing the work by hand, and the honest move is
    to fix the goal or the checks rather than raise max_attempts. This is the one question
    check-quality and convergence cannot answer: both grade the machinery, neither asks
    whether running it was worth it.

    Wasted attempts are counted separately because they are the compounding cost -- a run
    that converges on attempt 5 kept its result but paid for four discarded drafts.
    """
    out: list[str] = []
    total = len(loops)
    if total:
        kept, attempts, wasted = 0, 0, 0
        for run in loops:
            atts = run.get("attempts", [])
            if not atts:
                continue
            attempts += len(atts)
            last = atts[-1]
            if bool(last.get("checks")) and all(
                    c.get("exit_code") == 0 and not c.get("timed_out") for c in last["checks"]):
                kept += 1
                wasted += len(atts) - 1   # every attempt before the keeper was discarded
            else:
                wasted += len(atts)       # nothing kept: the whole run is sunk cost
        rate = kept / total if total else 0.0
        out.append(f"  loop runs: {kept}/{total} kept ({rate:.0%})")
        out.append(f"  attempts:  {attempts} total, {wasted} discarded")
        if total < 5:
            out.append(f"  → {total} run(s) is too few to judge; keep collecting.")
        elif rate < 0.5:
            out.append("  → BELOW 50%: the loop is costing more than doing this by hand.")
            out.append("    Fix the goal or the checks — raising max_attempts buys more of")
            out.append("    the same failure, at a longer context each time.")
        else:
            out.append("  → Earning its cost.")

    if swarms:
        lanes = [l for run in swarms for l in run["lanes"]]
        if lanes:
            passed = sum(1 for l in lanes if l.get("passed"))
            confirmed = sum(1 for l in lanes
                            if l.get("passed") and
                            sum(1 for v in l.get("verdicts", []) if v.get("refuted")) * 2
                            <= len(l.get("verdicts", [])))
            out.append(f"  swarm lanes: {passed}/{len(lanes)} passed, "
                       f"{confirmed} survived refutation")
            if passed and confirmed < passed:
                out.append(f"  → {passed - confirmed} lane(s) passed their own checks but were")
                out.append("    refuted. Those are the dangerous ones: green and unconfirmed.")
    return out


def analyze_convergence(loops: list[dict]) -> list[str]:
    """Did runs converge, stall, or exhaust their budget?"""
    out = []
    for run in loops:
        atts = run.get("attempts", [])
        if not atts:
            continue
        last = atts[-1]
        passed = bool(last.get("checks")) and all(
            c.get("exit_code") == 0 and not c.get("timed_out") for c in last["checks"])
        name = Path(run["_path"]).name
        if passed:
            out.append(f"  ✅ {name}: converged in {len(atts)} attempt(s)")
        else:
            # Identical consecutive failure sets = thrashing, not progress.
            sigs = ["|".join(sorted(c["name"] for c in a.get("checks", [])
                                    if c.get("exit_code") != 0)) for a in atts]
            stuck = len(sigs) >= 2 and sigs[-1] == sigs[-2]
            why = "stuck on the same failures" if stuck else "budget exhausted"
            out.append(f"  ❌ {name}: {len(atts)} attempt(s), {why}")
    return out


def analyze_lanes(swarms: list[dict]) -> tuple[list[str], list[str]]:
    """Lane outcomes, plus the co-occurrence that argues for or against a graph."""
    lines, graph = [], []
    outcomes: dict[str, list[bool]] = defaultdict(list)
    refuted = Counter()
    for run in swarms:
        for lane in run["lanes"]:
            outcomes[lane["name"]].append(bool(lane.get("passed")))
            for v in lane.get("verdicts", []):
                if v.get("refuted"):
                    refuted[lane["name"]] += 1

    for name, results in sorted(outcomes.items()):
        p = sum(results)
        flag = f"  {name}: {p}/{len(results)} passed"
        if refuted[name]:
            flag += f" — but refuted {refuted[name]}× by verifiers"
        lines.append(flag)

    # THE FAKE EDGE TEST, computed rather than performed by hand.
    #
    # An edge from A to B is REAL only when B consumes something A produces. Sequence is the
    # order someone wrote the lanes down; dependency is B being unable to start without A's
    # output. Most workflows carry two or three edges that are sequence dressed up as
    # dependency, and each one serializes work that could have run in parallel.
    #
    # Lanes now declare `produces`/`consumes`, so this is answered from the data. Correlated
    # outcomes are still reported below, but only as a HINT -- two lanes hitting one flaky
    # check correlate perfectly with no dependency at all.
    real_edges: list[tuple[str, str, str]] = []
    producers: dict[str, str] = {}
    consumers: dict[str, list[str]] = defaultdict(list)
    declared = False
    for run in swarms:
        for lane in run["lanes"]:
            for p in lane.get("produces") or []:
                producers[p] = lane["name"]
                declared = True
            for c in lane.get("consumes") or []:
                consumers[c].append(lane["name"])
                declared = True
    for artifact, consumer_names in consumers.items():
        src = producers.get(artifact)
        for cn in consumer_names:
            if src and src != cn:
                real_edges.append((src, cn, artifact))

    if declared:
        depended = {a for a, _, _ in real_edges} | {b for _, b, _ in real_edges}
        independent = sorted(set(outcomes) - depended)
        if real_edges:
            graph.append("  REAL dependencies (declared produces → consumes):")
            for a, b, art in sorted(real_edges):
                graph.append(f"    {a} → {b}  (via {art})")
        if independent:
            graph.append(f"  No incoming or outgoing edge — can run in parallel: "
                         f"{', '.join(independent)}")
        if not real_edges:
            graph.append("  Every lane declared a contract and NONE consume another's output")
            graph.append("  — these are independent. Any ordering between them is sequence,")
            graph.append("  not dependency.")

    names = sorted(outcomes)
    runs_seen = max((len(v) for v in outcomes.values()), default=0)
    if runs_seen >= 3:
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                xs, ys = outcomes[a], outcomes[b]
                if len(xs) != len(ys) or len(set(xs)) < 2:
                    continue  # a lane with a constant outcome tells us nothing
                if xs == ys:
                    graph.append(f"  {a} and {b} always share an outcome across {len(xs)} runs "
                                 "— worth checking; could be a shared dependency OR one "
                                 "flaky check both hit")
    return lines, graph


def main() -> int:
    ap = argparse.ArgumentParser(description="Analyze evidence-loop and swarm traces.")
    ap.add_argument("paths", nargs="+", help="trace JSON files or directories")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args()

    files: list[Path] = []
    for raw in a.paths:
        p = Path(raw)
        files.extend(sorted(p.rglob("*.json")) if p.is_dir() else [p])
    if not files:
        print("no trace files found", file=sys.stderr)
        return 1

    loops, swarms = load(files)
    checks = analyze_checks(loops)

    if a.json:
        print(json.dumps({
            "loop_runs": len(loops), "swarm_runs": len(swarms),
            "checks": {n: {"runs": s.runs, "failures": s.failures, "caught": s.caught,
                           "median_sec": round(s.median_sec, 2), "verdict": s.verdict()[0]}
                       for n, s in checks.items()},
        }, indent=2))
        return 0

    print(f"traces: {len(loops)} loop run(s), {len(swarms)} swarm run(s)\n")

    if checks:
        print("CHECK QUALITY — is each check actually evidence?")
        for s in sorted(checks.values(), key=lambda x: -x.failures):
            label, why = s.verdict()
            print(f"  {label:10} {s.name:16} {why}")
        dead = [s.name for s in checks.values() if s.verdict()[0] == "NO SIGNAL"]
        if dead:
            print(f"\n  → {len(dead)} check(s) never caught anything: {', '.join(dead)}")
            print("    Either they test nothing, or nothing they test has broken yet. Try")
            print("    breaking the code they cover on purpose — if they stay green, delete them.")
        print()

    if loops or swarms:
        print("KEEP RATE — did this earn its cost?")
        for line in analyze_keep_rate(loops, swarms):
            print(line)
        print()

    if loops:
        print("CONVERGENCE")
        for line in analyze_convergence(loops):
            print(line)
        print()

    if swarms:
        lines, graph = analyze_lanes(swarms)
        print("LANES")
        for line in lines:
            print(line)
        print()
        print("DO YOU NEED A GRAPH?")
        if graph:
            for g in graph:
                print(g)
            if not any("REAL dependencies" in g for g in graph):
                # Only correlation was found. Say plainly that it is not an edge, and name
                # the test that would settle it -- declaring produces/consumes.
                print("\n  → Correlation is a HINT, not an edge: two lanes hitting one flaky")
                print("    check correlate perfectly with no dependency at all. To settle it,")
                print("    declare `produces`/`consumes` on each lane and re-run — the fake")
                print("    edge test is then computed instead of guessed.")
        else:
            n = max((len(v) for v in defaultdict(list, {}).values()), default=0)
            print("  No coupling detected. Lanes look independent — a graph would add rigidity")
            print("  without buying anything. Keep collecting traces.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
