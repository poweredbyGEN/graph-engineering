# verify-mcp

**Deterministic verification tools for coding agents, over MCP 2.0.**

Give an agent an unrestricted shell and every check it runs is an arbitrary-code-execution
decision a human has to approve. This server inverts that: it exposes a small, fixed set of
**read-only** checks — tests, lint, typecheck, git status — that cannot modify your repo, so
you can pre-approve them once and let the agent verify its own work unattended.

The security model in one line: **no tool accepts a command.** The agent picks from an
enumerated list of runners and may only point them at a path inside a configured root. A
confused or compromised model cannot turn `run_tests` into `rm -rf`.

## Tools

| Tool | What it does | Runners |
|---|---|---|
| `list_checks` | Reports the root and which runners are actually installed | — |
| `run_tests` | Runs the test suite, returns exit code + output | pytest, vitest, jest, go, cargo |
| `run_linter` | Runs a linter | ruff, eslint, clippy |
| `run_typecheck` | Runs a static type checker | mypy, pyright, tsc |
| `git_status` | `git status --short` + diffstat (never stages/commits/fetches) | — |

Have the agent call `list_checks` first — it reports what's installed instead of the agent
guessing a stack and getting `unavailable`.

## Install

Requires Python 3.11+. Use a virtualenv: `mcp>=2` is a **breaking change** from 1.x, and
`pip install mcp` now pulls 2.x, so a global install can break existing MCP projects.

```bash
git clone https://github.com/poweredbyGEN/graph-engineering.git
cd graph-engineering/harness/servers/verify-mcp
uv venv && uv pip install -e ".[dev]"
uv run pytest -q          # 16 tests, no network
```

## Wire it into Claude Code

```bash
claude mcp add verify -- /abs/path/to/.venv/bin/verify-mcp
```

Or by hand in `~/.claude.json` / `.mcp.json`:

```json
{
  "mcpServers": {
    "verify": {
      "command": "/abs/path/to/servers/verify-mcp/.venv/bin/verify-mcp",
      "env": { "VERIFY_MCP_ROOT": "/abs/path/to/your/project" }
    }
  }
}
```

Works with any MCP client (Claude Code, Codex, Cursor, custom agents) — nothing here is
Claude-specific.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `VERIFY_MCP_ROOT` | `.` | **The containment boundary.** Every path is resolved inside this; traversal and escaping symlinks are refused. |
| `VERIFY_MCP_TIMEOUT` | `300` | Per-command timeout in seconds. |
| `VERIFY_MCP_TRANSPORT` | `stdio` | `stdio` for local clients, `streamable-http` to run as a network service. |

**Always set `VERIFY_MCP_ROOT` explicitly.** The default is the process working directory,
which is whatever your client happened to launch from.

### Running it as a service

The 2026-07-28 MCP spec is **stateless** — no `initialize` handshake, no session ID, each
request self-contained. So this deploys behind an ordinary load balancer with no sticky
routing:

```bash
VERIFY_MCP_TRANSPORT=streamable-http VERIFY_MCP_ROOT=/srv/repo verify-mcp
```

## Design notes

Four decisions worth keeping if you fork this:

**A failing suite is a result, not an error.** `run_tests` returns `exit_code=1` plus the
output rather than raising. Agents need to *read* failures to fix them.

**A missing binary says `unavailable`.** Never empty output — that reads as "no problems
found", a false green, which is the worst possible failure for a verifier.

**Output truncates from both ends.** Tail-only truncation drops pytest's summary line, where
the pass/fail counts live.

**Runner args are fixed tuples, run with `shell=False`.** Nothing model-supplied is ever
interpolated into a command string. A test asserts no shell metacharacters appear in any
runner's args, so folding a path into `"pytest -q {path}"` fails CI.

## Testing

```bash
uv run pytest -q
```

Every test carries an `# intent:` comment naming the failure it catches. The containment
tests are the load-bearing ones — sabotage-check them (break `_safe_path`, confirm they
fail, restore) before trusting a change. A test that can't fail isn't protection.

## License

MIT
