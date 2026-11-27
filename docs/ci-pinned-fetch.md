# CI pinned sibling source fetch (CLI v1)

Owner: infra/graph-engineering, epic GEN-13741, ticket GEN-13796. The only fetch implementation is `ops/ci-fetch-pinned`. Consumers download it at an immutable infrastructure commit through the same raw-file distribution as `ops/ci-workers`.

## Diagnosis

First wrong step: CI container → Git smart HTTP transport. Chat PR 1334 pipeline 3528, prepare-tests step 269375 on Helsinki agent 41: `install_mcp_contracts.sh` started at relative second 8 and failed at second 98 with `FAIL: cannot fetch pinned gen-mcp-server 0bf03b48c2422ae0286a70d8653172e43be0dec0 for contracts`, with no Git diagnostic. The enclosing `timeout 90` killed the stalled command. Other workflows in that same pipeline passed; one image-pull also timed out awaiting HTTP headers. The ticket reports five occurrences and Gitea upload-pack HTTP 200 responses. A sampled Gitea server log also shows HTTP 200 upload-pack completions.

This is recurrent CI transport failure, not a product intent or billing defect; no customer chat/request ID is supplied. Product replay is inapplicable to Git. The sim-gen directory supplied to the lane has no `docs/REGRESSION-LIBRARY.md`. Pre-check is the actual old fetch command in disposable step-image containers plus the real Git smart-HTTP fault tests below. No model or paid media calls are needed.

20 fresh shallow fetches per named agent identity were run on 2026-11-27, alternating default address selection and forced IPv4, for MCP 0c5aa5636a6ebbbf809a632785bafb73d74b1c12. Mac agents share the same Docker daemon/bridge; these are fresh step-image containers, not modifications to live CI steps. Helsinki used its existing `python:3.12` step image, Mac used its existing `gbv2-ci:latest` step image (both contain Git). All fetched objects equal the requested pin.

| Agent | Fetches | Failed | Median s | Max s |
|---|---:|---:|---:|---:|
| gen-ci-hel-1 | 20 | 0 | 2.018 | 20.013 |
| mac-studio-agentic | 20 | 0 | 1.016 | 1.325 |
| mac-studio-data | 20 | 0 | 0.974 | 1.115 |
| mac-studio-gen-org | 20 | 0 | 0.961 | 1.063 |
| mac-studio-mcp | 20 | 0 | 1.062 | 1.171 |
| mac-studio-workflow | 20 | 0 | 1.014 | 1.129 |

Both hosts resolved A and AAAA addresses. IPv4 HTTPS returned 200; forced IPv6 failed immediately (curl 7, no viable IPv6 route). The step interfaces have MTU 1500 and all complete pack transfers passed with that MTU. These observations do **not** establish an IPv6 black hole or a path-MTU defect. The intermittent stall was not reproduced in this sample; its packet-level cause remains unconfirmed. Do not force IPv4, change MTU, raise postBuffer, or claim the network fault is fixed on this evidence. The reproducible software defect is that the shared operation has no progress deadline or recovery from a transient transport failure. A later successful fetch does not remove that recurrent failure class.

## Contract

`ci-fetch-pinned <[GEN/|infra/]repo> <40-character lowercase SHA> <destination>` (bare `<repo>` defaults to `GEN/<repo>`) requires `GEN_GITEA_READ_TOKEN` and the existing CI Bash, Git and GNU timeout commands. Success is exit 0 and a detached destination HEAD equal to the requested SHA; stdout names each attempt and ends in `attempt N/3 ok`. The fetch object is checked before checkout and HEAD is checked afterward. No branch, alternate host, archive, model, or stale checkout substitutes for the pin. Each step owns its destination.

Git/libcurl aborts below 1000 bytes/s for 30 seconds. Each fetch also runs under GNU timeout30s with a5s kill grace, covering DNS/TCP/TLS before HTTP progress detection applies. There are at most three fetch attempts, with pauses of 5 and 15 seconds. Three fully stalled attempts finish in about 110 seconds; the30s attempt cap also ends progressing transfers over30s. That explicit limit is required while the deployed Git lacks a connection-only timeout setting. Never claim lowSpeedTime bounds a TLS handshake.

All refusals exit 1. Stderr codes: `PINNED_FETCH_INPUT expected repo sha dest` (arity) or `PINNED_FETCH_INPUT expected repo full-sha nonempty-dest` (invalid input); `PINNED_FETCH_AUTH` (missing token, HTTP 401/403, authentication refusal); `PINNED_FETCH_MISSING <repo> <sha>` (missing object); `PINNED_FETCH_MISMATCH` (fetched/checked-out revision differs); `PINNED_FETCH_STALLED <repo> <sha> after 3 attempts` (transport failures exhausted). Auth/missing/mismatch do not retry. Success never depends on stderr text; permanent refusal recognition reads Git's C-locale transport diagnostics, not user text.

## Evidence and published pattern

- [Git HTTP configuration](https://git-scm.com/docs/git-config.html): `http.lowSpeedLimit`/`http.lowSpeedTime` and their environment equivalents implement progress-based aborts. Use the native transport controls instead of a homemade download/watchdog.
- [libcurl connection timeout](https://curl.se/libcurl/c/CURLOPT_CONNECTTIMEOUT.html): connection establishment includes DNS and TLS; its native timeout does not limit established transfers. Git does not expose this option in the CI version, so use the requested per-attempt hard cap rather than inventing an unsupported http.connectTimeout/GIT_HTTP_CONNECT_TIMEOUT setting.
- [GNU timeout](https://www.gnu.org/software/coreutils/manual/html_node/timeout-invocation.html): the duration and kill-after grace bound the whole command process group.
- [libcurl low-speed timer](https://curl.se/libcurl/c/CURLOPT_LOW_SPEED_TIME.html): low-speed detection is disabled by default and aborts a transfer below the selected threshold.
- [Docker bridge networking](https://docs.docker.com/engine/network/drivers/bridge/): bridge IPv6 and MTU are explicit settings; inspect rather than infer them from DNS records.

Lane evidence: `content-flows/lanes/release/cx-13796/{diagnosis-summary.json,*-probe.jsonl,ci-fetch-log.txt}`. The isolated HTTP tests use `git http-backend`, inject first-attempt stall/reset, permanent missing/auth failures and all-attempt stalls. T5 injects a successful wrong fetched commit at the Git process boundary: real Git itself rejects an invalid pack before exit zero. Both checks are necessary; never claim a malicious pack can bypass Git's object validation.

Run `python -m pytest ops/tests -q` (the existing CI contracts and release gate command). The discovered test_ci_fetch_pinned.py adapter invokes real Git cases T1–T6 and T8 in test_ci_fetch_pinned.sh. T8 accepts TCP but never completes TLS, requiring each attempt to abort within35s and exactly3 attempts to yield PINNED_FETCH_STALLED. T7 remains the gen-agentic consumer case. The script can also run individual cases directly. T7 extends gen-agentic's existing `tests/test_ci_mcp_source.py` against this exact helper, including the three independent Hub pins and all existing owner-contract preparation. Consumer CI cannot pass until the infrastructure commit containing the helper is published. The lane keeps both commits local for coordinator review; it never starts remote CI or changes pin values.

## Remaining callers: next releases only

The requested migration changes gen-agentic `scripts/ci/fetch_mcp_source.sh`, including its two `_fetch_hub_contract_pin` invocations. These other 11 script callers keep their existing behavior until their repo's next release:

| Repo | Script |
|---|---|
| gen-agentic | `scripts/ci/fetch_workflow_source.sh` |
| gen-agentic | `scripts/ci/fetch_dw_source.sh` |
| gen-agentic | `scripts/ci/fetch_gengemini_source.sh` |
| gen-agentic | `scripts/ci/fetch_snapshot_readers.sh` |
| gen-agentic | `scripts/ci/install_mcp_contracts.sh` |
| gen-workflow | `scripts/ci/fetch_agentic_source.sh` |
| gen-workflow | `scripts/ci/fetch_dw_source.sh` |
| gen-workflow | `scripts/ci/fetch_gateway_source.sh` |
| gen-workflow | `scripts/ci/fetch_mcp_source.sh` |
| gen-mcp-server | `scripts/ci/fetch_agentic_source.sh` |
| gen-mcp-server | `scripts/ci/fetch_workflow_source.sh` |

Release order: publish the reviewed infrastructure helper first, then the gen-agentic consumer release with its exact helper pin. No production application schema or runtime contract changes. Post-check: repeat the original failed CI fetch entry point from a step container, verify exact SHA and the original affected pipeline; query later CI failures for this class over 24 hours. Any customer-chat replay requirement remains with the application tickets those pipelines ship; this infrastructure ticket supplies no original chat to replay.
