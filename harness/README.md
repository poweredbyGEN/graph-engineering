# Harness — what the agent can see, touch, and prove

**Build this first.** An agent's reliability is mostly a property of its harness, not of the
model. When an agent "can't do the job", the harness is usually why: a missing capability, no
way to verify its own work, or no way to inspect what happened afterwards.

## What's here

| | |
|---|---|
| [`servers/verify-mcp`](servers/verify-mcp) | An MCP server exposing deterministic verification — `run_tests`, `run_linter`, `run_typecheck`, `git_status`, `list_checks`. **16 tests, sabotage-checked.** |
| [`mcp-2-server-guide.md`](mcp-2-server-guide.md) | How to build an MCP 2.0 server: the spec's stateless request model, per-request capability negotiation, and the extensions worth knowing. |

## Why verification belongs in the harness

The alternative is the agent reporting its own success. That is a self-report, and it is the
single most common source of *"it looked done but wasn't."*

Putting checks behind a tool the agent calls — rather than a claim it makes — means the
result comes from a subprocess it cannot influence. It also means the checks are **read-only
and side-effect free**, so they can be pre-approved and run unattended without a human
clicking through permission prompts.

## Containment

Every path argument is resolved against a configured root and rejected if it escapes:

```python
def _safe_path(path: str) -> Path:
    target = (ROOT / path).resolve() if path else ROOT
    if target != ROOT and ROOT not in target.parents:
        raise ValueError(f"path escapes the configured root: {path!r}")
```

`.resolve()` before the comparison is what makes it hold — otherwise `../../etc` passes a
naive prefix check. That behaviour is pinned by a test; break the containment and the suite
goes red.

## Do you need this?

Not always. If your loop already runs the checks itself (see [`../loops`](../loops)), the
loop is the harness for that job and a separate MCP server buys little.

Reach for a server when **the agent should decide when to verify** — mid-task, before
committing, after a refactor — rather than only at the loop's fixed boundary. That is the
difference between an agent that checks its work and one that gets checked.

## Testing

```bash
cd servers/verify-mcp
uv venv && uv pip install -e ".[dev]"
uv run pytest -q          # 16 tests, no network
```
