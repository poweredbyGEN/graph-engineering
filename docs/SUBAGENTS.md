# Portable subagent configuration

Graph Engineering identifies workers by a profile name, not by vendor. A profile declares
an adapter, model, and six explicit capabilities: `read`, `write`, `structured_output`,
`worktree`, `resume`, and `mcp`. The scheduler can therefore reject an incompatible worker
before executing a node.

Copy `subagents.example.toml` to `~/.config/graph-engineering/config.toml`, make it private,
and replace the model aliases. Keep endpoint and credential **values** in your secret manager;
configuration contains environment-variable names only. The example includes Claude, Codex,
Grok, Kimi K3, and GLM 5.2, but names are arbitrary and other agents use the same contract.

Configuration precedence is:

1. built-in safe defaults;
2. the private user file `~/.config/graph-engineering/config.toml`;
3. checked-in `.graph-engineering.toml` routing selection;
4. ignored `.graph-engineering.local.toml` private overrides;
5. `GRAPH_ENGINEERING_PROFILE`, `GRAPH_ENGINEERING_POOL`, or `GRAPH_ENGINEERING_TIER`;
6. an explicit runtime/CLI selector.

Only one selector may be active at each layer. CLI wins over environment, which wins over the
configured default. Stable-hash pools use the pool name and node routing key, so retries and
resumes choose the same profile without asking an LLM to route.

Pool and tier routing happens when a graph is compiled. Each persisted agent node stores the
concrete `profile` name that compilation selected. Resume therefore uses the same worker unless
an explicit new run is compiled; it does not silently re-route partial work.

## Trust boundary

The checked-in project file may contain only `version` and `[routing]`. It cannot define an
executable, endpoint, environment reference, or model. This prevents a cloned repository from
turning configuration discovery into command execution. Adapter commands are argv arrays and
are never passed through a shell. Only documented placeholders are accepted; the executable
itself cannot be templated.

Subprocess profiles inherit only environment variables named in `env_allowlist` when the
runtime launches them. OpenAI-compatible profiles use `endpoint_env`, `api_key_env`, and an
optional `organization_env`; literal secrets are invalid configuration.

Direct API profiles normally provide judgment rather than repository access. Declare `read`,
`write`, `worktree`, or `mcp` only when the surrounding adapter actually implements that
capability. Names are not promises; capability checks are the enforcement.

The public Claude, Codex, and Grok profiles are least-authority writing workers. Each invocation
disables ambient MCP discovery; Claude and Grok have explicit read/edit tool allowlists without a
terminal, Grok also disables web, memory, and nested agents under its strict OS sandbox, and Codex
uses its workspace-write sandbox. Output contracts are embedded in the prompt and validated at the
adapter boundary; the public Codex profile deliberately avoids the provider's narrower native
schema subset until a deterministic schema compiler is available. Deterministic checks belong to
the orchestrator. If a read-only graph node genuinely needs MCP, define a separate private profile
with an explicit server/tool policy and set `mcp = true` only for that profile.

## Kimi K3 and GLM 5.2 through OpenCode

The runtime currently executes `subprocess` profiles. Its `openai-compatible` profile shape is
reserved but deliberately fails preflight until a direct HTTP executor is implemented. Do not
configure a direct profile and assume it works.

OpenCode provides a supported subprocess route for providers it has configured. Install the
repository's model-neutral restricted agent and select it from the profile argv:

```bash
install -d -m 0700 ~/.config/opencode/agents
install -m 0600 examples/opencode-agents/graph-worker.md \
  ~/.config/opencode/agents/graph-worker.md
opencode run --pure --agent graph-worker --model provider/model \
  --format json --dir "$PWD" "Return a one-line status only."
```

Then copy the `kimi-k3` or `glm-5.2` profile in `subagents.example.toml` and replace only its
model alias. Provider authentication remains in the user's OpenCode/provider configuration;
the public graph config contains no endpoint or credential. The template denies external
directories, network tools, MCP tools, skills, nested agents, and all shell commands. Deterministic
checks run in the orchestrator rather than in this model process; an OpenCode permission file is
not an operating-system sandbox. The graph contract requires a dedicated worktree for writing
nodes, and `PortableRuntime` supplies that boundary, captures the change set, and rejects changes
outside the declared scope before fan-in.

`mcp = false` is intentional for these example profiles: the restricted worker denies MCP tools.
Create a different private agent policy if a node genuinely needs MCP, and declare the capability
only after that policy and server allowlist are verified.

OpenCode's current Markdown-agent and permission syntax is documented at
<https://opencode.ai/docs/agents/> and <https://opencode.ai/docs/permissions/>.
