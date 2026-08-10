# Extending graph engineering

Read this before changing the skill/runtime, adding an agent adapter or topology, or creating an
MCP service. Extend contracts and evidence, not prose promises.

## Invariants

1. Nodes are bounded jobs; edges exist only for real data or constrained-resource flow.
2. Deterministic plumbing stays in code. Models implement or judge; they do not flatten, route,
   deduplicate, claim, or decide their own acceptance.
3. Validate every input/output edge. Unknown fields and unsupported capabilities fail closed.
4. Schedule the ready frontier. Add a barrier only for a complete-set dependency.
5. Isolate parallel writes and give one integration node the shared writer role.
6. Classify effects before retry/resume. Unknown writes are not replay-safe.
7. Persist identity, base SHA, attempts, artifacts, receipts, capabilities, and deadlines. Resume
   does not create a new budget or silently change a worker.
8. Deterministic checks precede independent model review. Combined gates follow integration.
9. Every loop has attempt, no-progress, wall-time, node, and cost ceilings.
10. Skills describe procedures; MCP exposes capabilities; neither grants authority.
11. Public code contains templates and environment-variable names only. Private registries,
    endpoints, credentials, and organization policy remain user-side.
12. Turn every enforceable lesson into a regression and sabotage-check it.
13. Keep the transport layers distinct: MCP exposes capabilities; A2A delegates to an independent
    agent; the graph runtime alone owns control flow and acceptance.

## Change procedure

For a runtime or skill enhancement:

1. State the missing capability and a concrete failure it prevents.
2. Decide whether it belongs in a node contract, edge transform, scheduler, adapter, MCP boundary,
   skill procedure, or deterministic check. Do not solve a runtime invariant with Markdown alone.
3. Extend the schema and validator before execution code. Preserve or explicitly migrate persisted
   state and workflow versions.
4. Add the smallest execution change, with failure/effect classification and bounded cleanup.
5. Add focused contract, lifecycle, crash/resume, and security tests as applicable.
6. Sabotage the protection, prove the test fails, restore it, and rerun the suite.
7. Update the skill reference, public example, CLI help/docs, changelog, and release evidence that
   expose the behavior.
8. Run `graph-engineer validate` on every changed example, the package test suite, public tree and
   history scans, clean archive install, and the repository release gate.
9. Use the normal reviewed PR and exact-SHA CI path. Do not push main or publish from a feature SHA.

## Add an agent adapter or profile

Prefer the existing subprocess adapter when a CLI can accept a bounded prompt and emit a final
structured result. A new adapter must define:

- shell-free argv and allowed placeholders;
- prompt transport (`stdin`, argv, or a protected disk-backed file);
- one authoritative final result channel and strict JSON/schema normalization;
- reduced environment and secret-reference policy;
- working-directory/worktree containment and write-scope enforcement;
- stdout/stderr/body limits, wall timeout, process-group cancellation, and cleanup;
- explicit capabilities: read, write, structured output, worktree, resume, MCP;
- redacted receipts binding run, node, attempt, profile/model, command/schema/output digests, base and
  result SHAs, timing, and exit status.

Test malformed output, progress-event spoofing, trailing garbage, output caps, closed pipes with a
live child, timeout/descendant cleanup, secret non-inheritance, scope escape, schema mismatch, and
resume identity drift. Add public templates without real model IDs, hosts, endpoints, or secrets;
put operational values in private configuration.

## Decide whether to add A2A

Add an A2A profile only when a graph node must cross an independent deployment, organization,
runtime, or ownership boundary. Keep a local subprocess profile for CLIs you already control. Use
MCP when the missing thing is a tool, API, data source, or resource rather than a peer agent.

An A2A client adapter must:

- implement one declared protocol version and binding exactly, including media types and errors;
- fetch and validate the Agent Card, identity, interface, authorization requirement, and allowed
  skills before dispatch;
- keep endpoint, identity, token references, and organization policy in private configuration;
- pin card/interface/protocol/capability digests and persist the remote task ID;
- fence task binding to the exact live local attempt and resume by polling the same task;
- bound request/result bytes, polling, deadline, cancellation, and status transitions;
- normalize exactly one final structured artifact and validate it locally;
- require a canonical changeset for remote code writes, apply it to a local isolated worktree, and
  rerun deterministic local gates;
- prevent remote profiles from inheriting local MCP, approval, integration, merge, or deployment
  authority.

Test redirect/cross-origin drift, legacy or unsupported cards, malformed security requirements,
identity/skill/capability drift, duplicate submission, stale late binding, task loss, cancellation,
timeout, malformed/oversized artifacts, schema mismatch, and out-of-scope changes. Document any
unimplemented binding, streaming, auth-required, or idempotency behavior explicitly.

Primary A2A reference: <https://a2a-protocol.org/latest/specification/>.

## Decide whether to add an MCP service

Add a server only when it provides a capability the agent lacks or a meaningful security/durability
boundary. Good candidates include bounded access to a remote system, durable task coordination,
read-only verification, or a narrow action that can be safer than shell access.

Do not add a server merely to wrap a CLI already available to the agent, duplicate an existing
graph owner, expose a broad shell/filesystem/database surface, or advertise tools a profile is not
authorized to call. Tool listings consume routing attention even when unused.

Before implementation define:

- exact tools/resources and why each earns its surface;
- input/output JSON schemas, byte/count/time limits, idempotency/effect class, and error taxonomy;
- project/user scope, trust tier, authentication secret references, and data-retention policy;
- client profiles allowed to see it and required protocol/extension capabilities;
- fallback behavior when the client lacks an extension;
- whether writes need leases, generations/fencing, receipts, approval, or reconciliation.

## Build a current MCP service

“MCP 2.0” is not a label. It is the official 2.x SDK plus per-request protocol/capability behavior.
Use a separate locked environment and the official SDK. New services should support the stateless
`2026-07-28` lifecycle; serve legacy `initialize` clients only when required by actual hosts.

For `2026-07-28`:

- accept the protocol version and client capabilities on every request;
- implement or allow SDK-provided `server/discover`, but do not require discovery;
- compute the intersection of server support, live client capabilities, project authorization, and
  tool-level requirements before dispatch;
- advertise only registered operations; removed/unsupported methods return method-not-found;
- do not depend on sessions, server-initiated sampling/roots, or deprecated logging behavior;
- use Streamable HTTP for remote servers and stdio for local single-user tools; do not start new
  legacy HTTP+SSE deployments;
- enforce request/body/result limits and transport security before model-controlled code runs.

Tasks are the `io.modelcontextprotocol/tasks` extension, not blanket core authority. Return a task
handle only when the client advertises the extension and the exact tool permits it. Otherwise use a
normal tool call plus the service's own durable polling state. Skills-over-MCP is optional discovery,
not authority: filter private skills before listing, bind version/digest/provenance/requirements,
and never let skill content expand the authorized tool set.

Test both lifecycle eras you claim, discovery/capability accuracy, extension absence/presence,
unknown methods, schema/size/time ceilings, cancellation, authentication scope, secret redaction,
duplicate claim races, stale-worker fencing, restart/recovery, effect-safe replay, and least-authority
tool enumeration. Run the tests against the installed artifact, not only the source tree.

## Register and authorize a new service

Keep the private registry outside repositories. Record server ID, command or endpoint reference,
owner/project scope, secret reference, trust tier, allowed tools/resources, supported protocol
range, and last negotiated capabilities. Give workers ephemeral scoped connection material rather
than copying the whole interactive-host registry.

Register through each host's supported user configuration, then run its doctor/get command and a
bounded discovery smoke. Set a worker profile's `mcp = true` only after its tool allowlist and
negotiated capability intersection are proven. Persist the profile ID, server IDs, protocol
versions, capability hashes, and skill digests in the node receipt so a retry cannot silently gain
authority.

If a remote A2A worker uses MCP internally, that is the remote operator's capability boundary. Do
not copy the caller's MCP registry into its request. The local graph receipt pins only the allowed
A2A skill and negotiated remote capability identity; local evidence still decides acceptance.

Primary specifications and implementation references:

- <https://blog.modelcontextprotocol.io/posts/2026-07-28/>
- <https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/schema/2026-07-28/schema.ts>
- <https://github.com/modelcontextprotocol/python-sdk>
- <https://modelcontextprotocol.io/extensions/tasks/overview>

## Revisit upstreams periodically

Prior art is a snapshot, not a permanent verdict. Before each minor runtime release, before a major
MCP/adapter redesign, and at least every 90 days while the project is active:

1. Read the pinned revisions in the public `NOTICE.md` and `docs/ROADMAP.md` provenance tables.
2. Fetch each upstream default branch and compare it with the recorded revision.
3. Inspect changes to contracts, scheduler/recovery behavior, conformance tests, security model,
   licenses, releases, and MCP protocol support. Do not judge evolution from stars or README claims.
4. Re-run clean-install tests and dependency/security audits for candidates that materially changed.
5. Update the pinned revision and adoption/rejection rationale when evidence changes. Preserve the
   old rationale in history; do not silently rewrite provenance.
6. Port only a bounded behavior with attribution and a local regression/sabotage test. Never merge
   whole skill folders, runtimes, or git histories merely because an upstream evolved.

The recurring question is not “what can we copy?” It is “did an upstream now prove a safer,
faster, or more testable contract than ours, and can we demonstrate that locally?”
