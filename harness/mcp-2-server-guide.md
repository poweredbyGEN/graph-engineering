# Building an MCP 2.0 server

Verified against `mcp==2.0.0` (Python SDK, released 2026-07-28) on 2026-08-07.

## The breaking change nobody warns you about

`pip install mcp` **now installs 2.x**, and v2 is not source-compatible with 1.x. If anything
on your machine already imports `mcp`, a bare install silently upgrades it and breaks it.

From the SDK's own README:

> Since `pip install mcp` now installs 2.x, keep a `<2` upper bound on your requirement (for
> example `mcp>=1.28,<2`) until you've migrated.

**Always build in a virtualenv.** v1.x continues on the [`v1.x` branch](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)
with security patches only.

Check what you have before installing anything:

```bash
python3 -c "import importlib.metadata as m; print(m.version('mcp'))"
```

## What changed in the 2026-07-28 spec

The base protocol is now, verbatim from the spec:

> JSON-RPC message format · **Stateless, self-contained requests** · Per-request capability
> negotiation

No `initialize` handshake, no session ID. Every request carries its own context. That's why v2
servers deploy behind an ordinary load balancer or on serverless with no sticky routing —
which was painful under v1.

Extensions are now formal and opt-in:

- **Tasks** — async long-running work with "polling, mid-flight input, and durable handles"
- **MCP Apps** — interactive UI rendered inline (charts, forms, players)
- **Skills over MCP** — structured agent-workflow instructions discovered through MCP

## Minimum viable server

```python
from mcp.server import MCPServer

mcp = MCPServer("my-tools", instructions="What this server is for, in one sentence.")

@mcp.tool()
def run_tests(path: str = "") -> str:
    """Run the test suite and return exit code plus output.

    Args:
        path: subdirectory to run in; empty means the repo root.
    """
    ...

if __name__ == "__main__":
    mcp.run()  # stdio; or mcp.run(transport="streamable-http")
```

That's the whole API surface for a basic server. Three things to know:

- `@mcp.tool()` returns the **plain function** — call it directly in tests, no `.fn` wrapper.
- The **docstring is the model's routing signal.** It's what the model reads to decide whether
  to call your tool. A thin docstring means a tool that never gets used.
- `mcp.run()` defaults to `stdio`, which is how local clients (Claude Code, Cursor) launch it.

### Testing it

```python
import asyncio
tools = asyncio.run(mcp.list_tools())   # names + descriptions the model will see
```

Interactive debugging: `npx @modelcontextprotocol/inspector`

## Design rules that matter

Learned building [`verify-mcp`](servers/verify-mcp); each is enforced by a test there.

**1. Never accept a command string.** The single most important rule. Enumerate the options
and look them up in a fixed table:

```python
RUNNERS = {"pytest": ("pytest", "-q"), "vitest": ("npx", "--no-install", "vitest", "run")}

def run_tests(runner: str = "pytest") -> str:
    if runner not in RUNNERS:
        return f"unknown runner {runner!r}; choose one of: {', '.join(RUNNERS)}"
    subprocess.run(RUNNERS[runner], shell=False, ...)   # fixed tuple, nothing interpolated
```

A tool taking `command: str` is a remote shell with extra steps. Prompt injection anywhere in
the agent's context then becomes arbitrary execution.

**2. Contain paths by resolving, then comparing.**

```python
target = (ROOT / path).resolve()
if target != ROOT and ROOT not in target.parents:
    raise ValueError("path escapes the configured root")
```

A `str.startswith()` check looks equivalent and **passes a symlink pointing outside the root**.
Resolve first, always.

**3. Failure is a result, not an exception.** Return `exit_code=1` plus the output. The model
needs to read failures to fix them; raising just loses the information.

**4. Never return empty output for a missing tool.** Say `unavailable: <binary> not installed`.
Empty output reads as "no problems found" — a false green, the worst failure mode for a
verification tool.

**5. Truncate from both ends.** Tail-only truncation drops pytest's summary line, which is
where the pass/fail counts live. Keep head *and* tail:

```python
half = MAX_OUTPUT // 2
out = f"{out[:half]}\n…[{len(out) - MAX_OUTPUT} chars truncated]…\n{out[-half:]}"
```

**6. Ship a discovery tool.** Something like `list_checks` that reports configuration and which
backends are actually installed. Otherwise the model guesses your stack and burns turns on
`unavailable`.

**7. Keep the tool surface small.** 5–12 well-described tools beat 40 vague ones. Every tool's
name and description sit in context for the whole session; a bloated listing degrades routing
for *every* tool, not just the unused ones.

## Official servers worth using before writing your own

| Server | Use for |
|---|---|
| [`servers/filesystem`](https://github.com/modelcontextprotocol/servers) | Scoped file read/write — always allow-list project dirs only |
| [`servers/git`](https://github.com/modelcontextprotocol/servers) | Local git operations |
| [GitHub MCP](https://github.com/github/github-mcp-server) | PRs, issues, Actions, code search (first-party) |
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) | Browser automation, UI verification, screenshots |

The `modelcontextprotocol/servers` repo (89k+ stars) is the reference set. **Write a custom
server for your project's test/lint/build commands** — that's consistently higher leverage
than adding another generic one, because it's the part nobody else can write for you.

## Sources

- Spec: <https://modelcontextprotocol.io/specification/2026-07-28>
- Python SDK: <https://github.com/modelcontextprotocol/python-sdk> (v2.0.0)
- TypeScript SDK: <https://github.com/modelcontextprotocol/typescript-sdk> (`@modelcontextprotocol/server@2.0.0`)
- Inspector: <https://github.com/modelcontextprotocol/inspector>
