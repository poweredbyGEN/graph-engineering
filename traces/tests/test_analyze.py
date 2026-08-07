"""Tests for trace-analyze.

The load-bearing tests are the verdict ones. This module's whole job is telling you a check is
worthless, so a bug that labels a dead check "EARNING" would quietly protect the exact thing
you asked it to find.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trace_analyze.analyze import (  # noqa: E402
    analyze_checks,
    analyze_convergence,
    analyze_keep_rate,
    analyze_lanes,
    load,
)


def chk(name="tests", code=0, dur=1.0, timed_out=False):
    return {"name": name, "cmd": ["x"], "exit_code": code,
            "duration_sec": dur, "output": "", "timed_out": timed_out}


def loop_run(attempts, path="r.json"):
    return {"goal": "g", "cwd": ".", "_path": path,
            "attempts": [{"n": i + 1, "checks": cs, "agent_ran": True, "agent_exit": 0}
                         for i, cs in enumerate(attempts)]}


# --- check verdicts -------------------------------------------------------------------

def test_never_failing_check_is_flagged_no_signal():
    # intent: THE point of this module. A check that has never failed is not evidence — it
    # tests nothing, or tests something that cannot break. Calling it healthy would hide the
    # one finding the user came here for.
    s = analyze_checks([loop_run([[chk(code=0)], [chk(code=0)]])])["tests"]
    assert s.verdict()[0] == "NO SIGNAL"
    assert "never failed" in s.verdict()[1]


def test_check_that_failed_then_passed_is_earning():
    # intent: fail-then-pass is the signature of a check doing real work — it caught something
    # and drove a fix. This must be distinguishable from a check that merely fails a lot.
    s = analyze_checks([loop_run([[chk(code=1)], [chk(code=0)]])])["tests"]
    assert s.verdict()[0] == "EARNING" and s.caught == 1


def test_always_failing_check_is_not_called_earning():
    # intent: a check that never goes green is usually misconfigured, not a great bug-finder.
    # Labelling it EARNING would send someone hunting a defect that isn't there.
    s = analyze_checks([loop_run([[chk(code=1)]] * 3)])["tests"]
    assert s.verdict()[0] == "ALWAYS RED" and s.caught == 0


def test_missing_binary_is_reported_as_broken_not_as_a_bug_find():
    # intent: exit 127 means the checker isn't installed. Counting that as a caught defect
    # would turn a broken environment into a false quality signal.
    s = analyze_checks([loop_run([[chk(code=127)], [chk(code=0)]])])["tests"]
    assert s.verdict()[0] == "BROKEN" and "127" in s.verdict()[1]


def test_timeout_is_surfaced_separately():
    # intent: a timeout is a failure the check didn't earn — the code may be fine. Blending it
    # into the failure count inflates how much the check is catching.
    s = analyze_checks([loop_run([[chk(code=-1, timed_out=True)], [chk(code=0)]])])["tests"]
    assert s.verdict()[0] == "SLOW" and s.timeouts == 1


def test_caught_requires_a_later_pass_in_the_same_run():
    # intent: "caught" means it drove a fix. A failure that never resolves is not a catch, and
    # counting it as one would overstate the check's value.
    s = analyze_checks([loop_run([[chk(code=1)], [chk(code=1)]])])["tests"]
    assert s.caught == 0


def test_multiple_checks_are_tracked_independently():
    # intent: a healthy check must not mask a dead one sitting beside it in the same run.
    stats = analyze_checks([loop_run([[chk("lint", 0), chk("tests", 1)],
                                      [chk("lint", 0), chk("tests", 0)]])])
    assert stats["lint"].verdict()[0] == "NO SIGNAL"
    assert stats["tests"].verdict()[0] == "EARNING"


# --- convergence ----------------------------------------------------------------------

def test_converged_run_is_reported_as_success():
    out = analyze_convergence([loop_run([[chk(code=1)], [chk(code=0)]])])
    assert "✅" in out[0] and "2 attempt" in out[0]


def test_stuck_run_is_distinguished_from_budget_exhaustion():
    # intent: "stuck on the same failures" and "ran out of budget" need different responses —
    # one means the goal or check is wrong, the other means raise max_attempts.
    stuck = analyze_convergence([loop_run([[chk("a", 1)], [chk("a", 1)]])])[0]
    assert "stuck" in stuck
    moving = analyze_convergence([loop_run([[chk("a", 1)], [chk("b", 1)]])])[0]
    assert "budget exhausted" in moving


def test_empty_attempts_do_not_crash():
    # intent: a trace from a run that died before its first check must not take the analyzer
    # down with it — you read traces precisely when something went wrong.
    assert analyze_convergence([loop_run([])]) == []


# --- lanes and the graph question -----------------------------------------------------

def test_lane_pass_counts_are_reported():
    lines, _ = analyze_lanes([{"_path": "s.json", "lanes": [
        {"name": "a", "passed": True, "verdicts": []},
        {"name": "b", "passed": False, "verdicts": []}]}])
    assert any("a: 1/1" in x for x in lines) and any("b: 0/1" in x for x in lines)


def test_refuted_lane_is_flagged_even_though_it_passed():
    # intent: a lane that passed its own checks but was refuted by verifiers is the most
    # dangerous row in the report — it looks green and isn't confirmed.
    lines, _ = analyze_lanes([{"_path": "s.json", "lanes": [
        {"name": "a", "passed": True, "verdicts": [{"refuted": True}, {"refuted": True}]}]}])
    assert "refuted 2" in lines[0]


def test_correlated_lanes_are_surfaced_across_runs():
    # intent: lanes that always share an outcome are worth a human look. Needs >=3 runs and
    # real variation, or coincidence reads as correlation.
    #
    # Asserts that detection FIRES and names both lanes -- deliberately not the exact
    # wording. An earlier version pinned the word "coupled", which both made the test brittle
    # and froze an overclaim: correlated outcomes are a hint, not a proven dependency (two
    # lanes hitting one flaky check correlate perfectly). The real test of an edge is whether
    # A's OUTPUT flows into B, which these traces cannot answer.
    runs = [{"_path": f"{i}.json", "lanes": [
        {"name": "a", "passed": p, "verdicts": []},
        {"name": "b", "passed": p, "verdicts": []}]}
        for i, p in enumerate([True, False, True])]
    _, graph = analyze_lanes(runs)
    assert graph, "correlated lanes across 3 runs should be surfaced"
    assert "a" in graph[0] and "b" in graph[0]


def test_independent_lanes_do_not_trigger_a_graph_recommendation():
    # intent: the mirror. Recommending a graph on independent lanes is the failure mode this
    # whole repo argues against — rigidity bought for nothing.
    runs = [{"_path": f"{i}.json", "lanes": [
        {"name": "a", "passed": p, "verdicts": []},
        {"name": "b", "passed": not p, "verdicts": []}]}
        for i, p in enumerate([True, False, True])]
    _, graph = analyze_lanes(runs)
    assert not graph


def test_constant_outcome_lanes_are_not_called_coupled():
    # intent: two lanes that always pass share an outcome trivially. Calling that coupling
    # would recommend a graph for every healthy swarm.
    runs = [{"_path": f"{i}.json", "lanes": [
        {"name": "a", "passed": True, "verdicts": []},
        {"name": "b", "passed": True, "verdicts": []}]} for i in range(4)]
    _, graph = analyze_lanes(runs)
    assert not graph


# --- loading --------------------------------------------------------------------------

def test_both_trace_shapes_are_recognized(tmp_path):
    # intent: the two runners emit different shapes ({attempts} vs a bare list). Silently
    # skipping one would make half the data invisible with no error.
    (tmp_path / "loop.json").write_text(json.dumps({"goal": "g", "attempts": []}))
    (tmp_path / "swarm.json").write_text(json.dumps([{"name": "a", "passed": True}]))
    loops, swarms = load([tmp_path / "loop.json", tmp_path / "swarm.json"])
    assert len(loops) == 1 and len(swarms) == 1


def test_malformed_trace_is_skipped_not_fatal(tmp_path, capsys):
    # intent: one corrupt file must not lose the whole batch — a killed run often leaves
    # truncated JSON behind, and that is exactly when you want the other traces.
    (tmp_path / "bad.json").write_text("{not json")
    (tmp_path / "good.json").write_text(json.dumps({"goal": "g", "attempts": []}))
    loops, _ = load([tmp_path / "bad.json", tmp_path / "good.json"])
    assert len(loops) == 1
    assert "skipping" in capsys.readouterr().err


# --- keep rate: did this earn its cost? -----------------------------------------------

def test_keep_rate_counts_converged_runs_not_attempts():
    # intent: THE cost question. A run that converges on attempt 3 is ONE kept result that
    # paid for two discarded drafts. Counting attempts as keeps would make a thrashing loop
    # look productive — exactly backwards.
    runs = [loop_run([[chk(code=1)], [chk(code=1)], [chk(code=0)]], "a.json"),
            loop_run([[chk(code=1)], [chk(code=1)]], "b.json")]
    out = "\n".join(analyze_keep_rate(runs, []))
    assert "1/2 kept (50%)" in out
    assert "5 total, 4 discarded" in out   # 3+2 attempts, only the final one kept


def test_a_loop_below_fifty_percent_is_called_out_as_not_worth_running():
    # intent: below ~50% the loop costs more than doing the work by hand. Staying silent
    # there means quietly billing for a loop that is losing money.
    runs = [loop_run([[chk(code=1)]], f"{i}.json") for i in range(5)]
    out = "\n".join(analyze_keep_rate(runs, []))
    assert "BELOW 50%" in out
    assert "raising max_attempts" in out   # names the WRONG fix explicitly


def test_a_healthy_loop_is_reported_as_earning():
    runs = [loop_run([[chk(code=0)]], f"{i}.json") for i in range(6)]
    out = "\n".join(analyze_keep_rate(runs, []))
    assert "6/6 kept (100%)" in out and "Earning its cost" in out


def test_too_few_runs_withholds_a_verdict():
    # intent: a 0% keep rate over 2 runs is noise, not a signal. Declaring a loop
    # uneconomic on two samples would kill loops that are merely new.
    runs = [loop_run([[chk(code=1)]], f"{i}.json") for i in range(2)]
    out = "\n".join(analyze_keep_rate(runs, []))
    assert "too few to judge" in out
    assert "BELOW 50%" not in out


def test_a_passing_but_refuted_lane_is_flagged_as_the_dangerous_case():
    # intent: a lane that passed its own checks but lost the refutation vote is the most
    # dangerous row in any report — it reads green and is not confirmed. Counting it as a
    # keep would launder a guess into a result.
    swarms = [{"_path": "s.json", "lanes": [
        {"name": "a", "passed": True, "verdicts": [{"refuted": True}, {"refuted": True}]},
        {"name": "b", "passed": True, "verdicts": [{"refuted": False}]}]}]
    out = "\n".join(analyze_keep_rate([], swarms))
    assert "2/2 passed, 1 survived refutation" in out
    assert "green and unconfirmed" in out


def test_empty_input_produces_no_keep_rate_claim():
    # intent: never assert an economic verdict with no data behind it.
    assert analyze_keep_rate([], []) == []


# --- the fake edge test, computed from declared contracts ------------------------------

def lane(name, passed=True, produces=None, consumes=None):
    return {"name": name, "passed": passed, "verdicts": [],
            "produces": produces or [], "consumes": consumes or []}


def test_a_real_dependency_is_reported_as_an_edge():
    # intent: THE fake edge test. An edge is real only when B consumes what A produces.
    # This is the thing correlation cannot establish, and the whole reason lanes declare
    # contracts at all.
    runs = [{"_path": "s.json", "lanes": [
        lane("research", produces=["findings.json"]),
        lane("synthesis", consumes=["findings.json"])]}]
    _, graph = analyze_lanes(runs)
    joined = "\n".join(graph)
    assert "research → synthesis" in joined and "findings.json" in joined


def test_declared_but_unconnected_lanes_are_called_parallelizable():
    # intent: the payoff. Lanes that declare contracts and share nothing are independent,
    # whatever order someone wrote them in — that is time being handed away for free.
    runs = [{"_path": "s.json", "lanes": [
        lane("a", produces=["a.json"]), lane("b", produces=["b.json"])]}]
    _, graph = analyze_lanes(runs)
    joined = "\n".join(graph)
    assert "independent" in joined.lower() or "parallel" in joined.lower()


def test_a_lane_consuming_its_own_output_is_not_an_edge_to_itself():
    # intent: a self-edge is not a dependency between lanes, and reporting one would
    # imply an ordering constraint that does not exist.
    runs = [{"_path": "s.json", "lanes": [
        lane("a", produces=["x.json"], consumes=["x.json"])]}]
    _, graph = analyze_lanes(runs)
    assert not any("a → a" in g for g in graph)


def test_correlation_and_real_edges_are_reported_differently():
    # intent: the distinction this whole change exists to draw. Correlated outcomes are a
    # hint; a declared produces→consumes pair is proof. Collapsing them would restore the
    # overclaim that two lanes hitting one flaky check are "coupled".
    correlated = [{"_path": f"{i}.json", "lanes": [
        {"name": "a", "passed": p, "verdicts": []},
        {"name": "b", "passed": p, "verdicts": []}]}
        for i, p in enumerate([True, False, True])]
    _, corr_graph = analyze_lanes(correlated)
    assert corr_graph and "could be" in "\n".join(corr_graph)   # hedged
    real = [{"_path": "s.json", "lanes": [
        lane("a", produces=["x"]), lane("b", consumes=["x"])]}]
    _, real_graph = analyze_lanes(real)
    assert "REAL dependencies" in "\n".join(real_graph)          # asserted
