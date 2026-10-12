# GEN-9831 contract cases

Test-author record for Plane GEN-9831 (owner o-sched, parent GEN-9667, epic GEN-9662).
The committed tests live in `tests/ops/test_ci_image_retention.py`; the builder turns
each case green unedited and removes its `xfail` marker as it passes. Cases are the
merge gate.

| Case | Exact input | Exact expected output | Side-effect counts | Fails if |
|---|---|---|---|---|
| T1 | Existing live case-line; refs `limitless-data-ci:py310`, `gbv2-ci:latest`, `day-ci:node22`, `gen-agentic-ci:py311`, `limitless-fe-ci:node24`, `xel-ci-node:node22`, `xel-ci-py:py312`, 100h old, unused | Kept; zero `docker rmi` calls for each | zero real Docker mutations (faked command boundary) | `*-ci:*` / `*-ci-*:*` keep patterns removed |
| T2 | Existing case-line; `ag-cifix-qa:01a11ebb`, `mcp-cifix-qa:b4abc0c5` protected; `foo:latest`, `postgres:16`, `woodpeckerci/plugin-git:2`, `python:3.11` unprotected; 100h old, unused | First two kept (zero rmi); remaining four exactly one rmi each | zero real Docker mutations | every image protected, or `*-qa*` / `*cifix*` patterns removed |
| T3 | Committed script bytes | `bash -n` exits 0; deleting `esac` makes `bash -n` exit nonzero | zero real Docker mutations | missing `esac` |
| T4 | Installed `/usr/local/bin/qa-docker-prune.sh` vs committed `ops/qa-docker-prune/qa-docker-prune.sh` after the sanctioned install | exact sha256 byte match; timer still active; no service restart | none | installed keep-list bytes differ |

## T4 — install contract (terminal verification, not a CI test)

T4 is verified once at the sanctioned installation on qa-deploy-box, never by a CI test
that SSHes real hosts. From the box, with the committed script already copied into place:

```bash
# Byte-match the installed script against the committed source.
sha256sum /usr/local/bin/qa-docker-prune.sh ops/qa-docker-prune/qa-docker-prune.sh
# Expected artifact: two identical hashes, one per path, on the same line.

# The existing hourly timer keeps the same path and stays active; no restart is needed.
systemctl is-active qa-docker-prune.timer
# Expected artifact: "active"
```

Artifact to record on the ticket: the single `sha256sum` output line (two identical
hashes) plus the `systemctl is-active` output of `active`. No process/service restart is
part of this ticket; the timer already runs at the same path.
