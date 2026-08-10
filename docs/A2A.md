# A2A remote workers

Graph Engineering uses A2A only at the boundary to an independently operated agent. The local
runtime remains the control plane: it owns the dependency graph, budgets, state, evidence,
worktrees, repair, and integration.

## Choose the right boundary

| Need | Use |
|---|---|
| Run a locally controlled Claude, Codex, Grok, Kimi, GLM, or other CLI | A subprocess profile |
| Give a worker a bounded tool, API, data source, or durable task service | MCP |
| Delegate one typed node to an independently deployed agent | A2A |

MCP and A2A are complementary. An A2A worker may internally use its own MCP tools, but it never
inherits the caller's local MCP registry or authority. A2A is not a second scheduler and a remote
agent cannot mark a graph node accepted.

## Implemented protocol subset

The adapter implements the A2A 1.x HTTP+JSON binding:

- public Agent Card discovery through a private configured URL;
- exactly one same-origin `HTTP+JSON` 1.x interface;
- Bearer authentication declared by the card and supplied by an environment reference;
- `POST /message:send`, `GET /tasks/{id}`, and `POST /tasks/{id}:cancel`;
- bounded polling, body limits, wall-clock deadline, and local cancellation;
- persisted task identity so a retry polls the same task;
- pinned card, interface, protocol, identity, skill, and capability digests.

It deliberately does not implement JSON-RPC, gRPC, streaming, push notifications, input-required
or auth-required multi-turn exchanges, or an A2A server. Redirects, cross-origin interfaces,
legacy protocol cards, undeclared skills, identity drift, and capability drift fail closed.

## Configure a private profile

Put the profile in `~/.config/graph-engineering/config.toml`, never in project source. Replace the
synthetic `.invalid` URL and model label locally; inject only the named token variable at runtime.

```toml
[profiles.remote-review]
adapter = "a2a"
model = "remote-agent-owned"

[profiles.remote-review.capabilities]
read = true
write = false
structured_output = true
worktree = false
resume = true
mcp = false

[profiles.remote-review.a2a]
agent_card_url = "https://review-agent.example.invalid/.well-known/agent-card.json"
auth_env = "GRAPH_REMOTE_REVIEW_TOKEN"
allowed_skills = ["review-diff"]
expected_identity = "review-agent"
```

Run `graph-engineer doctor --profile remote-review --repo "$PWD" --json` before dispatch. The
doctor reports only whether the token reference is populated and the profile is structurally
bounded; it does not print the URL, token, card, or prompt.

## Local evidence still decides acceptance

A read-only remote node must return exactly one JSON artifact matching the workflow output schema.
A writing remote node must return `{result, changeset}` where `changeset` matches the runtime's
canonical change-set schema. The runtime applies that change set to a fresh local attempt worktree,
captures it again under the declared write scope, and runs the normal local deterministic checks.
Only the integration node may combine accepted changes.

The remaining protocol risk is the narrow crash window after a server accepts `SendMessage` and
before its task ID is durably bound locally. The client uses a stable message ID, but safe recovery
also depends on the remote server honoring A2A idempotency. Use A2A nodes for replay-safe work;
keep external or non-idempotent effects behind explicit authorization and reconciliation.

Primary references:

- [A2A v1.0 specification](https://a2a-protocol.org/latest/specification/)
- [A2A and MCP relationship](https://a2a-protocol.org/latest/topics/a2a-and-mcp/)
- [Google ADK graph workflows](https://adk.dev/graphs/)
- [Google Agentic Space Quest codelab](https://codelabs.developers.google.com/way-back-home-level-1/instructions)
