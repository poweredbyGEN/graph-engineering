# Release readiness

`bin/gen-release-readiness --config private-inventory.json --report readiness.json`
collects live evidence over SSH and the hosted MCP's existing HTTP transport.
It prints a Markdown table and exits 1 for failures or unobservable requirements.
It does not deploy, restart, import application code, create agents, or collect
paid data. Python 3.11, SSH access, and a pinned MCP source checkout are required.

Keep the inventory outside version control: hosts, account IDs and endpoint
locations are site configuration. Tokens are read through named environment
variables, never through command arguments. The JSON report contains only check
results, error classes, runtime SHAs, Rails process revision paths/PIDs, and
activity/import-error booleans. Missing account/validation configuration reports
each subcheck as unobserved; it is not an observation exception. HTTP refusals
retain their status without exposing response bodies.

The inventory has these keys:

- `deployd`: `{ "host": "operator@qa-host", "url": "http://localhost:9666/status" }`.
- `mcp_source`: a pinned MCP checkout containing `scripts/verify_hosted_tools.py`.
- `production` and `staging`: arrays of `{ "host": "operator@host", "units": [...] }`.
  Each unit names `label`, `name`, `deploy_target`, and optionally `kind: "docker"`.
  Core labels are `rails`, `mcp`, `agentic`, `dw`, and `gensembledata`. Include every
  serving replica and worker as additional labels, using the same labels in both
  environments. Shared services can appear in both inventories. `{sha}` in a unit
  name expands to the deployd target's `last_deployed_sha`.
  Rails must map to this Rails repository's environment target (for example,
  `gbv2-production`/`gbv2-staging`), not another repository's deploy target.
- `mcp`: `production` and `staging` settings, each with `url`, `token_env`,
  `documented_views`, optional existing `agent_id`, and optional `contract_endpoints`
  mapping every client name to its existing GET contract-version endpoint.
  Saved-idea reads require both the existing agent scope and its bearer token.
  An unperformed read is reported separately from malformed identifiers or an
  empty saved-idea list. Served structured content takes precedence over prose.
- `qa`: preferably the existing Rails `organizations_url`, a scoped
  `organization_id`, and `token_env`. An authenticated organization read proves
  the QA session, owner permission to create an agent, and `available_credit`.
  A fresh interactive/password sign-in is outside this read-only probe.
  Alternatively, an existing read-only account-readiness API's `read_url` and `token_env`.
  It must prove `signin: true`, `can_create_agent: true`, `role: "Owner"`, and
  positive numeric `credits` for the staging workspace. Without that surface or
  QA credentials the check fails. A token existing on disk is not sign-in proof.
- `collect`: an existing validation-only API's `read_url`, optional `token_env`,
  `validation_only: true`, and independently documented `refusal_codes`. It must
  return `count`, the effective staging `cap`, typed `code`, and `dry_run: true`.
  Payment, funding, busy and credit errors never satisfy this check. Without a
  provider-free validation API this check fails; the probe never invokes collect.

The collect validator and QA credentials are integration prerequisites; no endpoints are created
by this tool. Do not point them at paid or mutating APIs. Unknown evidence stays
red, including missing saved ideas, missing advertised contract versions on any
client, or unobservable process provenance. GEN-9072 versions are optional only
while no observed client advertises them.

Rails provenance comes from the `REVISION` files under live service-process cwds;
conflicting process revisions fail closed. Other runtime provenance comes from
`/proc` environment, immutable interpreter deploy stamps, or running cwd Git HEAD. Activity and the last
100 journal lines from the previous 15 minutes prove startup smoke, not full
application health. Warehouse checks verify configuration presence, not SQL
connectivity or database grants. Renderer checks verify URL parity and a
non-loopback address, not render completion. No paid canary is permitted.

Tests: `tests/test_release_readiness.py` (comparison observations and fake HTTP).
Run only those affected tests through the site's bounded QA runner. Sabotage the
SHA comparison and typed-refusal check to verify both guards fail.
