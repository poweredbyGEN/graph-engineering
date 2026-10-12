# CI image retention

The QA box hosts Gitea, Woodpecker, staging and lane tests on one root disk, and an
hourly prune (`/usr/local/bin/qa-docker-prune.sh`, run by `qa-docker-prune.timer`)
reclaims unreferenced Docker objects so a full root disk cannot stop the forge
(GEN-9831). Locally built CI images must never be pruned, and this file is the
committed authority for that rule.

## Why the keep-list must cover CI images

The pre-baked CI images under `/opt/ci-images` (`limitless-data-ci:py310`, `gbv2-ci`,
`day-ci`, `xel-ci-*`, `gen-agentic-ci`, `limitless-fe-ci`, …) are built locally with no
registry, and Woodpecker runs them with `pull: false` (`.woodpecker/ci.yml`). They sit
unused between runs, so age-based removal deletes them once they cross the keep window
(72h, or 24h above 85% disk), and every pipeline of that repo then fails in seconds with
"pull access denied" until a person rebuilds by hand. The prune removed
`limitless-data-ci:py310` several times and pipelines 747 and 749 failed this way
(2026-10-12).

## The rule

The prune's selection loop must keep every tag matching one of the following, in
addition to the existing `*-qa*` and `*cifix*` protections:

```sh
case "$ref" in *-qa*|*cifix*|*-ci:*|*-ci-*:*|*"<none>"*) continue ;; esac
```

`*-ci:*` covers tags whose repository ends in `-ci` (`limitless-data-ci`,
`gbv2-ci`); `*-ci-*:*` covers the hyphenated family (`xel-ci-node`, `xel-ci-py`).
The `<none>` alternative keeps the untagged-image guard intact. This is the one path;
there is no second cleanup path, and no new timer: the existing hourly timer keeps
running at the same path with the keep-list extended.

## Committed source and verification

- Source of truth: `ops/qa-docker-prune/qa-docker-prune.sh`, deployed to
  `/usr/local/bin/qa-docker-prune.sh` on qa-deploy-box after a green PR/CI.
- Contract tests: `tests/ops/test_ci_image_retention.py` (T1–T3) and the T4 install
  byte-check in `tests/ops/CASES.md`.

Out of scope here: the concurrent legacy-builder dangling-layer prune that can delete a
running `docker build`'s untagged step images is a separate, independently ticketed
residual (skip the prune while a `docker build` is running, or move the box to
BuildKit/Buildx).
