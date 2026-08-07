# Swarm

**Fan out independent work; let no lane claim success without evidence.**

Parallelism does not improve quality on its own. Running N agents on one problem with no
evidence contract produces N confident wrong answers *faster*. What raises quality is:

1. Each lane must **prove** it finished — its own checks, run by a subprocess it doesn't control.
2. A claim that survives an **adversarial** pass is worth more than one nobody challenged.

So this runner does three things and refuses to skip any: **isolate** each lane, **gate** it on
evidence, then optionally try to **refute** the lanes that passed.

## When a swarm is the right tool

| Use it for | Don't use it for |
|---|---|
| Independent units — 20 repos, 50 tickets, one lane each | One hard problem; more agents just multiplies guesses |
| Diverse lenses on the same diff (correctness / security / perf) | Work with real dependencies between lanes |
| Adversarial verification of a finding | Anything you can't write a check for |

If you cannot write the check, you cannot use the swarm. That's deliberate — a lane with no
checks **fails closed**, because a swarm that reports success without evidence manufactures
confidence at scale.

## Usage

```bash
swarm --config .swarm.toml --repo /path/to/repo      # fan out
swarm --config .swarm.toml --dry-run                 # print the plan, run nothing
swarm --config .swarm.toml --keep-worktrees          # keep passing lanes for review
swarm --config .swarm.toml --trace run.json          # record every lane
```

Exit code is `0` only when every lane cleared the highest bar the config asked for — evidence
if there are no verifiers, refutation-survival if there are. CI can gate on it.

## Config

```toml
base = "main"                     # what each lane branches from
worktree_root = ".swarm-worktrees"

[agent]
# {task} and {name} are substituted per lane.
cmd = ["claude", "-p", "{task}"]

# Evidence. Every lane runs these; ALL must pass. No checks = fail closed.
[[checks]]
name = "tests"
cmd  = ["pytest", "-q"]

[[checks]]
name = "types"
cmd  = ["mypy", "src"]

# Optional adversarial pass over lanes that passed. Non-zero exit = "I found a problem".
[verify]
count = 3
cmd = ["claude", "-p", "Try to REFUTE that lane {lane} is correct. Exit 1 if you find a real problem."]

[[lanes]]
name = "fix-auth-timeout"        # must be a valid git branch name; must be unique
task = "Fix the 30s auth timeout in src/auth.py; add a regression test."

[[lanes]]
name = "fix-null-user"
task = "Handle null user in the session middleware."

[limits]
timeout_sec = 1800
```

## Why it's built this way

**One git worktree per lane.** Two agents editing one checkout is the classic swarm failure —
they clobber each other, and then the evidence is meaningless because you can't tell whose
change broke what. Each lane gets `swarm/<name>` on its own tree.

**Worktree creation is serialized.** `git worktree add` mutates shared state under
`.git/worktrees/` and is **not thread-safe**. Found live: four concurrent lanes produced
`fatal: failed to read .git/worktrees/beta/commondir`, and a lane failed for a reason unrelated
to its work. Creation takes a lock; the lanes themselves still run fully parallel — setup is
milliseconds against minutes of work.

**Evidence decides, not the agent's exit code.** An agent that does nothing and exits 0 must
fail. An agent that exits 3 but left the repo green must pass. The agent is never the authority
on its own success.

**No checks means fail closed.** Nothing can be proven, so nothing is claimed.

**`passed` and `confirmed` are different words.** A lane that passed its checks but faced no
verifiers is `passed`, never `confirmed`. Collapsing the two is exactly how unverified work gets
shipped.

**Verifiers are prompted to refute, not to confirm.** A verifier asked "is this right?" tends to
agree. Majority-refuted kills the claim; a lone dissenter doesn't veto, since adversarial
prompting produces some false positives.

**Failing lanes are cleaned up; passing lanes are kept.** You only want to review branches that
might get merged.

## Testing

```bash
python3 -m pytest tests/ -q     # 21 tests
```

Every test carries an `# intent:` comment. Two are worth knowing about:

- **fail-closed** and **refutation-majority** are sabotage-verified: break them and the tests
  fail.
- The **race test** asserts the *invariant* (creation is serialized) rather than just running
  lanes concurrently. The naive version passed 3/3 with the lock removed — on a small repo the
  calls finish too fast to interleave. A test that can't fail is decoration.
