# Releasing the Python package

`graph-engineering` publishes a wheel and source distribution. A release is allowed only from a
clean, complete clone whose `HEAD` has exactly the tag matching `project.version` in
`pyproject.toml`. The release program reruns every deterministic suite, Ruff, documentation
accuracy, the public working-tree scan, the full reachable-history scan, archive inspection, and
an isolated wheel install before `uv publish` becomes reachable.

The gate also examines commit metadata in `reviewed-base..HEAD`. It accepts only generic public
project names and GitHub noreply email addresses unless another exact generic identity is approved
at the scanner boundary. It never prints a name, email, or commit message. On a pull request or
push, Woodpecker supplies the target/pre-push base. On merged `main`, where `origin/main..HEAD`
would be empty, the releaser must pass the prior reviewed commit or release tag explicitly.

The isolated install must run `graph-engineer capabilities --json` and validate its package
version, command surface, schema-version set, join policies, and worker-smoke availability. Source
imports or help text alone are not sufficient evidence because wheel packaging can drift.

## Prepare and verify

1. Update `project.version` and `graph_engineering.__version__`, then add the same version as the
   newest dated entry in `CHANGELOG.md`. The release gate rejects drift among these three sources.
   Put the changes in a reviewed pull request and let every Woodpecker pipeline pass for the exact
   commit.
2. From a full clone of merged `main`, set disk-backed scratch and run the same release gate:

   ```bash
   export TMPDIR=/path/to/disk-backed/scratch
   uv sync --frozen --extra dev
   uv run python ops/release_gate.py verify --candidate-base <prior-reviewed-sha-or-tag>
   ```

3. Review the generated-package evidence in the output. The temporary artifacts are deleted after
   verification; this avoids accidentally publishing an older `dist/` directory.
4. Create the exact version tag only after the merged commit and gates are verified. Push the tag
   through the repository's normal reviewed release process.

## Publish

First prove the publication boundary without uploading:

```bash
uv run python ops/release_gate.py publish --dry-run --candidate-base <previous-release-tag>
```

Then run the same command without `--dry-run` in the authorized release environment. Authentication
is provided to `uv` through its supported environment or trusted-publishing mechanism; never put a
token, endpoint, or private index in this repository.

The publisher checks the tag and clean tree both before the gates and immediately before upload.
It rejects shallow history, so the history safety scan cannot silently examine only a checkout
fragment. It passes explicit freshly-built archive paths to `uv publish`; no stale glob is used.

Before creating the final squash commit, set a repository-local generic identity. For this public
project the exact safe configuration is:

```bash
git config --local user.name poweredbyGEN
git config --local user.email poweredbygen@users.noreply.github.com
```

Then create the squash normally and run the candidate metadata scan against the PR base. Both the
author and committer must use that identity; rewriting only the visible author is insufficient.
Never amend or rewrite already-public legacy history merely to satisfy this candidate gate.

This repository does not create tags or releases automatically. Packaging CI proves that a commit
is releasable, while the exact tag remains the explicit publication authorization. Woodpecker's
release plugin requires a forge API token to create durable release assets, so there is deliberately
no credential-free job that pretends its ephemeral build output is a published artifact. Configure
an authorized release environment before adding that upload step; never expose it to pull-request
builds.
