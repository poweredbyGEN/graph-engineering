# Planning capsule

The canonical entrypoint is `.graph-engineering/PROJECT.md`. A future agent should need only that
file to discover `product-contract.json`, `project.json`, `decisions/`, and `workflows/`. Do not add
a planning database, dashboard, MCP service, or private project brain.

## Discover before asking

Run `graph-engineer assess --repo "$PWD" --json`, then `graph-engineer init --repo "$PWD" --json`.
If the capsule exists, read `PROJECT.md` first and follow its links. Reuse its frozen generation and
matching active run. If it does not exist, init scaffolds a draft capsule.

Treat `init.unresolved` as the question queue. Inspect tracked code and existing decisions for facts,
then ask the human only the questions still listed. Never replace a missing product answer with a
model guess. For UI, API, events, jobs, integrations, tables, stores, migrations, permissions,
compatibility, risks, assumptions, or open decisions, record either bounded items or a specific N/A
reason. `N/A` without a reason is not an answer.

## Complete one generation

Keep the human brief and machine contract aligned. The contract must answer problem, users,
outcomes, in/out scope, journeys, all surfaces and data axes, auth/permissions, invariants,
compatibility, failure/recovery, rollout, rollback, live proof, risks, assumptions/hypotheses with
status and evidence, and open decisions with owners. Acceptance criteria use one of:

- `deterministic`: shell-free argv is required and `human_gate` is false;
- `human_gate`: argv is null and `human_gate` is true.

Append durable choices under `decisions/` and its index; do not silently rewrite an accepted record.
Update the brief and decision-index SHA-256 bindings, then update the canonical product-contract
digest in `project.json` and every derived workflow binding.

## Freeze before fan-out

A model cannot approve its own plan. A named human must explicitly approve the complete answers;
only then set `freeze.status` to `approved` and record `freeze.approved_by`. Run init again. Do not
perform the dependency audit, create worker nodes, or dispatch subagents until init reports no
planning questions and validates every digest.

Any changed answer, brief, or accepted decision invalidates derived work. Increment generation,
return it to draft, obtain fresh human approval, update all digests/bindings, and only then resume
graph design. Never silently mix work from two generations.
