# contract-sync — propagate backend contracts without a ticket

gen-backend-v2 owns the generated contracts. Consumers vendor byte-identical copies or
derive their own artifacts from them, and each consumer has a freshness gate that turns red
while its copy is stale. `contract-sync` closes that loop: when a watched backend path
changes on `main`, it runs each consumer's own regeneration command and opens a PR only when
that produced a diff. Nobody files a ticket for a contract change, and nobody notices the
red gate first.

It never edits a generated artifact by hand — the consumer's own generator produces the
bytes — and it never merges a PR that is not green.

## What it does, per run

1. Resolve `main` of `GEN/gen-backend-v2` (`git ls-remote`).
2. Stop if that is the sha recorded in `/var/lib/contract-sync/last-be-sha`.
3. Clone the backend shallow and hash every watched file:
   `docs/generated/**`, `config/creation_cards.yml`, `config/model_capabilities.yml`.
   The list of `<blob-sha> <path>` pairs is stored as
   `/var/lib/contract-sync/last-be-watched.tsv`.
4. If the watched hashes are unchanged, record the new sha and stop — a commit that only
   touched `app/` never clones a consumer.
5. Otherwise, for each consumer: fresh shallow clone, run its regeneration command, and
   look at `git status`. No diff means nothing to do.
6. On a diff: branch `auto/contract-sync-<be-sha8>`, commit
   `chore: sync backend contracts to gen-backend-v2 <be-sha8>`, push, open a PR, poll the
   PR head's statuses, and merge once they are all success.
7. Advance the state only after every consumer reached a terminal disposition.

One line per consumer goes to stdout, which the unit sends to journald.

## Consumers

| Repo | Regeneration | Artifacts |
|---|---|---|
| `gen-mcp-server` | `scripts/vendor_creation_card_branches.py`, `vendor_creation_card_matrix.py`, `vendor_user_job_enums.py`, `vendor_vidsheet_action_schema.py`, `sync_model_capabilities.py`, each with `--backend-path <clone> --ref origin/main` | `src/gen_mcp_server/contracts/`, including the `.source` sidecars those generators write |
| `gen-agentic` | copy from the backend clone | `docs/generated/{vidsheet-operations-schema,vidsheet-semantic-draft-schema,user-job-enums}.json` → `src/gen/contracts/` |
| `limitless-fe` | `node scripts/sync-vidsheet-contract.mjs` with `GEN_BACKEND_PATH=<clone>` (plain copy of the artifact if that script is gone) | `src/schema/vidsheets/contracts/…schema.json` + `.source.json` + `railsContract.generated.ts` |
| `api-docs` | `node scripts/sync-from-backend.mjs --backend <clone>` | skipped with a log line while that script does not exist |

The gen-mcp-server list is the `MCP_REGENERATE` array at the top of the script. The other
`vendor_*.py` scripts are deliberately absent: `vendor_publish_platforms.py` reads
gen-backend-python and `vendor_layer_prompts.py` reads limitless-data, so a backend-v2 clone
cannot run them.

`limitless-fe`'s generator stamps a fresh `vendoredAt` on every run. When the artifact bytes
did not change there is nothing to ship, so those timestamp-only edits are reverted and the
consumer reports "no diff" — otherwise every run would open a PR containing only a clock.

## Adding a consumer

1. Append the repo name to the `CONSUMERS` array.
2. Add a `case` arm in `process_consumer()` and a `regen_<name>()` function next to the
   others. It receives the consumer clone and the backend clone, and must run the repo's own
   generator rather than writing an artifact by hand.
3. If the command can legitimately not exist yet, `return 20` with `SKIP_REASON` set; the
   consumer is then logged as skipped instead of failed.
4. Add the repo to the table above.

Consumers are cloned from `$CONTRACT_SYNC_GIT_BASE/$CONTRACT_SYNC_OWNER/<name>.git`.

## Run it by hand

```bash
# once, with the token in scope
sudo install -m 0600 ops/contract-sync/contract-sync.env.example /etc/contract-sync.env
sudo "${EDITOR:-vi}" /etc/contract-sync.env          # set GEN_GITEA_TOKEN
sudo systemctl start contract-sync.service
journalctl -u contract-sync -n 50

# offline dry run: regenerate and diff locally, plan the PR, never push
sudo DRY_RUN=1 bash ops/contract-sync/contract-sync.sh
```

`DRY_RUN=1` skips push, PR and merge and prints the planned branch, commit message, PR and
merge for each consumer. It still clones, regenerates, diffs and advances the state file, so
a dry run and a real run agree on what they would do.

The offline test builds local bare repositories and runs the same code against them:

```bash
bash ops/contract-sync/test_contract_sync.sh     # fixtures under /mnt/data/tmp
```

## Install

```bash
sudo install -m 0644 ops/contract-sync/contract-sync.service /etc/systemd/system/
sudo install -m 0644 ops/contract-sync/contract-sync.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now contract-sync.timer
```

The unit's `ExecStart` assumes the checkout at `/opt/agent-infra`; edit it (or a drop-in) if
the repo lives elsewhere.

## Disable

```bash
sudo systemctl disable --now contract-sync.timer
```

The state directory is `/var/lib/contract-sync`; deleting `last-be-sha` makes the next run
treat itself as a first run and re-check every consumer.

## Guardrails

- **Green means green.** Only a `success` status counts. A `failure`, `error` or `warning`
  is red; anything unrecognised is treated as pending. Newest status per context wins, by
  row id rather than API order. Red, pending and absent checks all leave the PR open and log
  it.
- **main's pipeline first.** A merge on a repo whose pipeline runs with `concurrency: 1`
  cancels the run for the previous main sha and leaves main with no verdict, which blocks
  every deploy gated on it. The script waits for main's own `ci/woodpecker/push/*` statuses
  to settle before merging; a repo with no such status has nothing to cancel.
- **No force-push, no branch reuse.** A branch for this backend sha that already has an open
  PR is that PR. A branch whose PR was closed is an error, never a re-open. Branches are left
  in place after a merge.
- **The PR is the handoff.** A red or still-running PR is left open with a log line, and the
  state advances so the next tick does not re-open it. A hard failure — clone, regeneration,
  push, or the PR call itself — does not advance the state, so the next tick retries that
  change instead of dropping it.
- **One run at a time.** `flock /var/lib/contract-sync/lock`; an overlapping tick exits
  immediately instead of cloning a second copy of every consumer.
- **The token is never printed.** It appears only in a curl `Authorization` header.
  `test_contract_sync.sh` asserts that statically and with a sentinel value.

## Requirements

`bash`, `git` (with working git.gen.pro credentials — the unit relies on the box's git
credential helper, not on the API token), `python3` with `PyYAML` (for
`sync_model_capabilities.py`), `node`, `curl` and `flock`.
