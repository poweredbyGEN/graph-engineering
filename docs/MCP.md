# Graph task MCP adapter

The adapter is a portable, least-authority coordination surface over the official Python
MCP SDK. It replaces the old `task-graph-mcp` compatibility wrapper with durable local state
owned by this package.

## Protocol eras

The server intentionally supports both MCP lifecycle eras:

- `2024-11-05` through `2025-11-25` use the legacy `initialize` handshake. The requested
  supported version is echoed; an unknown version negotiates to the latest handshake version.
- `2026-07-28` is stateless. It does not use `initialize`. Every request carries protocol
  version and client capabilities in `_meta`; `server/discover` is optional and supported.

These semantics come from the official SDK rather than a local reimplementation. The
adapter pins the SDK to the compatible `2.0.x` series and removes unused convenience handlers
after server construction so discovery never
advertises prompts, logging, completions, subscriptions, or resource mutation it does not
serve.

Primary references:

- [2025-06-18 lifecycle](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)
- [2026-07-28 release and stateless lifecycle](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
- [2026-07-28 protocol schema](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/schema/2026-07-28/schema.ts)
- [MCP Tasks extension](https://modelcontextprotocol.io/extensions/tasks/overview)
- [official Python SDK](https://github.com/modelcontextprotocol/python-sdk)

## Model-controlled tools

Only these bounded tools are exposed:

- `graph_task_create`
- `graph_task_inspect`
- `graph_task_claim`
- `graph_task_heartbeat`
- `graph_task_complete`
- `graph_task_fail`
- `graph_task_cancel`

There are no model tools for force, bypass, administration, human approval, checkpoint
resolution, skip, or abort. Worker writes require an exact owner, live lease, and generation.
An expired claim is reclaimed with a higher generation, so the old worker cannot commit even
if it wakes up later.

SQLite uses WAL mode and `BEGIN IMMEDIATE` for claims. Identifiers, lease duration, error,
payload, result, skill content, and tool names are bounded. Each tool result contains a
receipt tied to the effective capability-manifest hash.

## Tasks extension and fallback

`io.modelcontextprotocol/tasks` is optional and explicitly advertised. The server returns a
`resultType: task` handle only when the current request declares the extension in
`io.modelcontextprotocol/clientCapabilities`. `tasks/get`, `tasks/update`, and
`tasks/cancel` reject calls without that per-request declaration.

Clients without Tasks use the same durable state through ordinary calls:

1. call `graph_task_create`;
2. retain the returned task ID;
3. poll `graph_task_inspect`;
4. workers claim and complete/fail with their fencing generation.

Start without the extension when validating a client that only supports core MCP:

```bash
graph-engineering-mcp --database ~/.local/state/graph-engineering/tasks.db \
  --disable-tasks-extension
```

## Skills and private configuration

Skills are optional resources with version, digest, provenance, and requirements metadata.
They explicitly grant no authority. Records marked private are filtered before registration.
Provider endpoints, credentials, private MCP registries, internal hosts, and organization
playbooks are not accepted from or emitted by the public server; those stay in user-side
configuration outside this repository.

The effective public capability manifest is available at
`graph-engineering://capabilities/manifest`. Profiles only intersect the built-in surface;
they cannot invent tools.
