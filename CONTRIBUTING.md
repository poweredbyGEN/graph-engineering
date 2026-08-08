# Contributing

Thanks for helping make graph-engineering more portable and reliable.

## Set up a development checkout

Use Python 3.11 or newer, Git, and a disk-backed temporary directory. With `uv` installed:

```bash
export TMPDIR=/path/to/disk-backed/scratch
uv sync --frozen --extra dev
uv run graph-engineer --version
uv run pytest -q tests
```

Run the complete release verification before opening a pull request:

```bash
uv run python ops/release_gate.py verify
```

That command runs formatting, lint, every deterministic suite, documentation checks, public tree
and history scans, archive inspection, and a clean-wheel installation. It does not publish.

## Change discipline

- Keep nodes bounded and pass data only across real dependencies.
- Put deterministic transformations and checks in code, not agent prompts.
- Add an `# intent:` comment to regression tests and sabotage-check the new assertion: break the
  behavior, prove the test fails, then restore it.
- Preserve backward compatibility for the versioned workflow schema, or document the intentional
  break and migration path.
- Add a concise entry under `Unreleased` in `CHANGELOG.md` for user-visible changes.
- Do not commit API keys, private MCP registries, provider credentials, local paths, or private
  worker configuration. Use `subagents.example.toml` only for credential-free examples.

## Pull requests

Keep a pull request focused, explain the failure mode it prevents, and include the exact commands
used to verify it. CI must pass for the exact commit under review. Maintainers publish releases
from a reviewed, tagged `main` commit according to [docs/RELEASING.md](docs/RELEASING.md).

Use GitHub issues for reproducible bugs and feature proposals. Report vulnerabilities privately as
described in [SECURITY.md](SECURITY.md).
