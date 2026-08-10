#!/usr/bin/env python3
"""Build, inspect, clean-install, and optionally publish a release.

Publishing is deliberately the last operation. It is unreachable unless the worktree is
clean, HEAD has the exact version tag, the clone has complete history, every deterministic
gate passes, and the built archives survive an isolated installation smoke test.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
TAG_PATTERN = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:a|b|rc)?[0-9]*$")
SCHEMA_SUFFIX = "graph_engineering/schemas/workflow-v1alpha1.schema.json"
ENTRY_POINTS = frozenset(
    {
        "graph-engineer = graph_engineering.cli:main",
        "graph-engineering-mcp = graph_engineering.mcp.__main__:main",
    }
)
LICENSE_FILES = frozenset({"LICENSE", "NOTICE.md"})
CHANGELOG_HEADING = re.compile(
    r"^## \[([^]]+)] - ([0-9]{4}-[0-9]{2}-[0-9]{2})$", re.MULTILINE
)
SOURCE_VERSION = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
REQUIRED_CAPABILITY_COMMANDS = frozenset(
    {
        "accept",
        "capabilities",
        "compile",
        "doctor",
        "events",
        "fork",
        "plan",
        "watch",
        "run",
        "status",
        "trace",
        "validate",
    }
)


class GateError(RuntimeError):
    """A release precondition or deterministic gate failed."""


def _run(
    argv: Sequence[str],
    *,
    cwd: Path = ROOT,
    env: Mapping[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(argv),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        text=True,
        capture_output=capture,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() if capture else "see command output above"
        raise GateError(f"command failed ({result.returncode}): {argv[0]}: {detail}")
    return result


def _git(*args: str) -> str:
    return _run(["git", *args], capture=True).stdout.strip()


def _project_version(root: Path = ROOT) -> str:
    with (root / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def assert_version_contract(root: Path = ROOT) -> str:
    """Bind package metadata, runtime output, license files, and changelog together."""

    with (root / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    version = str(project["version"])
    if project.get("license") != "MIT":
        raise GateError("project.license must use the SPDX expression 'MIT'")
    license_files = project.get("license-files")
    if not isinstance(license_files, list) or set(license_files) != LICENSE_FILES:
        raise GateError("project.license-files must contain LICENSE and NOTICE.md")
    missing = sorted(name for name in LICENSE_FILES if not (root / name).is_file())
    if missing:
        raise GateError(f"declared license files are missing: {missing}")

    init_text = (root / "src" / "graph_engineering" / "__init__.py").read_text(
        encoding="utf-8"
    )
    source_match = SOURCE_VERSION.search(init_text)
    if source_match is None or source_match.group(1) != version:
        raise GateError("package __version__ must equal project.version")

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = CHANGELOG_HEADING.findall(changelog)
    if not headings or headings[0][0] != version:
        raise GateError("the newest dated CHANGELOG entry must match project.version")
    return version


def assert_release_context(root: Path = ROOT, *, require_remote: bool = False) -> str:
    """Require one exact version tag, a clean tree, and optionally reviewed remote refs."""

    status = _run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        capture=True,
    ).stdout
    if status:
        raise GateError("release worktree is not clean")
    shallow = _run(
        ["git", "rev-parse", "--is-shallow-repository"], cwd=root, capture=True
    ).stdout.strip()
    if shallow != "false":
        raise GateError("release requires a complete, non-shallow git history")
    version = _project_version(root)
    expected = f"v{version}"
    tags = _run(
        ["git", "tag", "--points-at", "HEAD"], cwd=root, capture=True
    ).stdout.splitlines()
    if tags != [expected] or TAG_PATTERN.fullmatch(expected) is None:
        raise GateError(f"HEAD must have exactly the release tag {expected!r}")
    if require_remote:
        head = _run(
            ["git", "rev-parse", "HEAD^{commit}"], cwd=root, capture=True
        ).stdout.strip()
        main = _run(
            ["git", "rev-parse", "refs/remotes/origin/main^{commit}"],
            cwd=root,
            capture=True,
        ).stdout.strip()
        tag = _run(
            ["git", "rev-parse", "refs/graph-engineering/release-tag^{commit}"],
            cwd=root,
            capture=True,
        ).stdout.strip()
        if head != main or head != tag:
            raise GateError(
                "release HEAD must equal fetched origin/main and its remote version tag"
            )
    return expected


def refresh_public_history(root: Path = ROOT) -> None:
    """Fetch every public branch and tag so a history scan cannot use a stale ref set."""

    shallow = _run(
        ["git", "rev-parse", "--is-shallow-repository"], cwd=root, capture=True
    ).stdout.strip()
    argv = ["git", "fetch", "--quiet", "--force", "--tags"]
    if shallow == "true":
        argv.append("--unshallow")
    argv.extend(("origin", "+refs/heads/*:refs/remotes/origin/*"))
    _run(argv, cwd=root)


def refresh_release_tag(root: Path = ROOT) -> str:
    """Fetch the exact version tag into a dedicated ref; fail if it is only local."""

    expected = f"v{_project_version(root)}"
    _run(
        [
            "git",
            "fetch",
            "--quiet",
            "--force",
            "origin",
            f"+refs/tags/{expected}:refs/graph-engineering/release-tag",
        ],
        cwd=root,
    )
    return expected


def _candidate_metadata_command(candidate_base: str | None) -> tuple[str, ...]:
    command = (
        "uv",
        "run",
        "--frozen",
        "--extra",
        "dev",
        "python",
        "-m",
        "graph_engineering.public_safety",
        "--repo",
        ".",
        "--mode",
        "candidate-metadata",
    )
    if candidate_base is not None:
        return (*command, "--candidate-base", candidate_base)
    return command


def _gate_commands(
    candidate_base: str | None = None,
) -> tuple[tuple[tuple[str, ...], Mapping[str, str]], ...]:
    base = ("uv", "run", "--frozen", "--extra", "dev")
    inherited = os.environ.copy()
    traces = inherited | {"GRAPH_ENGINEERING_PORTABLE_TESTS": "1"}
    return (
        (("uv", "lock", "--check"), inherited),
        (
            (*base, "ruff", "format", "--check", "src", "tests", "ops/release_gate.py"),
            inherited,
        ),
        ((*base, "ruff", "check", "src", "tests", "ops/release_gate.py"), inherited),
        ((*base, "pytest", "-q", "tests"), inherited),
        ((*base, "pytest", "-q", "harness/servers/verify-mcp/tests"), inherited),
        ((*base, "pytest", "-q", "loops/tests"), inherited | {"PYTHONPATH": "loops"}),
        ((*base, "pytest", "-q", "swarm/tests"), inherited | {"PYTHONPATH": "swarm"}),
        ((*base, "pytest", "-q", "traces/tests"), traces | {"PYTHONPATH": "traces"}),
        ((*base, "pytest", "-q", "ops/tests"), inherited),
        ((*base, "python", "ops/check-docs-accurate.py"), inherited),
        (
            (
                *base,
                "python",
                "-m",
                "graph_engineering.public_safety",
                "--repo",
                ".",
                "--mode",
                "tree",
            ),
            inherited,
        ),
        (
            (
                *base,
                "python",
                "-m",
                "graph_engineering.public_safety",
                "--repo",
                ".",
                "--mode",
                "history",
            ),
            inherited,
        ),
        ((_candidate_metadata_command(candidate_base)), inherited),
    )


def run_deterministic_gates(*, candidate_base: str | None = None) -> None:
    assert_version_contract()
    refresh_public_history()
    if _git("rev-parse", "--is-shallow-repository") != "false":
        raise GateError("public-history gate requires a complete, non-shallow clone")
    for argv, env in _gate_commands(candidate_base):
        print("+", " ".join(argv), flush=True)
        _run(argv, env=env)


def _safe_members(names: Sequence[str], archive: str) -> None:
    for name in names:
        path = PurePosixPath(name)
        if "\\" in name or path.is_absolute() or ".." in path.parts:
            raise GateError(f"{archive} contains an unsafe member path")


def verify_archives(wheel: Path, sdist: Path) -> None:
    """Prove runtime, legal, and release-note files are present in built artifacts."""

    with zipfile.ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
        _safe_members(wheel_names, wheel.name)
        if not any(name.endswith(SCHEMA_SUFFIX) for name in wheel_names):
            raise GateError("wheel is missing the packaged workflow schema")
        entry_names = [
            name for name in wheel_names if name.endswith(".dist-info/entry_points.txt")
        ]
        if len(entry_names) != 1:
            raise GateError("wheel must contain exactly one entry_points.txt")
        entry_points = archive.read(entry_names[0]).decode("utf-8")
        missing = sorted(ENTRY_POINTS - set(entry_points.splitlines()))
        if missing:
            raise GateError(f"wheel is missing console entry points: {missing}")
        for filename in LICENSE_FILES:
            if not any(
                name.endswith(f".dist-info/licenses/{filename}") for name in wheel_names
            ):
                raise GateError(f"wheel is missing declared license file {filename}")
    with tarfile.open(sdist, mode="r:gz") as archive:
        members = archive.getmembers()
        sdist_names = [member.name for member in members]
        _safe_members(sdist_names, sdist.name)
        if any(not (member.isfile() or member.isdir()) for member in members):
            raise GateError("sdist contains a link or special archive member")
        if not any(name.endswith(SCHEMA_SUFFIX) for name in sdist_names):
            raise GateError("sdist is missing the packaged workflow schema")
        root_files = {
            path.parts[-1]
            for name in sdist_names
            if len((path := PurePosixPath(name)).parts) == 2
        }
        required_root_files = LICENSE_FILES | {"CHANGELOG.md"}
        missing_root = sorted(required_root_files - root_files)
        if missing_root:
            raise GateError(f"sdist is missing required root files: {missing_root}")


def assert_installed_capabilities(output: str, expected_version: str) -> None:
    """Reject an installed CLI whose advertised contracts drift from its package."""

    try:
        manifest = json.loads(output)
        commands = set(manifest["cli_commands"])
        joins = set(manifest["runtime"]["join_policies"])
        schemas = manifest["schema_versions"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise GateError("installed capability manifest is malformed") from exc
    if manifest.get("version") != "graph-engineering/capabilities/v1":
        raise GateError("installed capability manifest version is unsupported")
    if manifest.get("package_version") != expected_version:
        raise GateError("installed capability manifest package version drifted")
    if not REQUIRED_CAPABILITY_COMMANDS.issubset(commands):
        raise GateError("installed capability manifest omits CLI commands")
    if joins != {"all", "all_settled", "any", "n_of_m", "majority"}:
        raise GateError("installed capability manifest join policies drifted")
    required_schemas = {
        "workflow",
        "workflow_proposal",
        "project",
        "product_contract",
        "assessment",
        "lifecycle_event",
        "run_context",
        "handoff",
        "status_projection",
        "benchmark",
        "learning_proposal",
        "run_fork",
        "run_watch",
        "event_stream",
    }
    if set(schemas) != required_schemas or any(
        not isinstance(value, str) or not value for value in schemas.values()
    ):
        raise GateError("installed capability manifest schema versions drifted")
    if manifest.get("features", {}).get("worker_smoke") is not True:
        raise GateError("installed capability manifest omits worker smoke")
    if manifest.get("features", {}).get("reviewed_workflow_compilation") is not True:
        raise GateError(
            "installed capability manifest omits reviewed workflow compilation"
        )
    if manifest.get("runtime", {}).get("typed_profile_fallback") is not True:
        raise GateError("installed capability manifest omits typed profile fallback")
    if manifest.get("features", {}).get("immutable_run_forks") is not True:
        raise GateError("installed capability manifest omits immutable run forks")
    if manifest.get("features", {}).get("bounded_event_stream") is not True:
        raise GateError("installed capability manifest omits bounded event stream")
    if manifest.get("features", {}).get("live_run_watch") is not True:
        raise GateError("installed capability manifest omits live run watch")


def build_and_smoke(scratch: Path) -> tuple[Path, Path]:
    dist = scratch / "dist"
    dist.mkdir()
    _run(["uv", "build", "--out-dir", str(dist)])
    wheels = list(dist.glob("*.whl"))
    sdists = list(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise GateError("build must produce exactly one wheel and one sdist")
    wheel, sdist = wheels[0], sdists[0]
    verify_archives(wheel, sdist)

    venv = scratch / "clean-venv"
    _run(["uv", "venv", "--python", sys.executable, str(venv)], cwd=scratch)
    python = venv / "bin" / "python"
    mcp_console = venv / "bin" / "graph-engineering-mcp"
    graph_console = venv / "bin" / "graph-engineer"
    _run(["uv", "pip", "install", "--python", str(python), str(wheel)], cwd=scratch)
    clean_env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "VIRTUAL_ENV"}
    }
    _run(
        [
            str(python),
            "-c",
            (
                "from graph_engineering.contracts import workflow_schema; "
                "assert workflow_schema()['$schema'].endswith('2020-12/schema')"
            ),
        ],
        cwd=scratch,
        env=clean_env,
    )
    help_result = _run(
        [str(mcp_console), "--help"], cwd=scratch, env=clean_env, capture=True
    )
    if "--database" not in help_result.stdout:
        raise GateError("installed graph-engineering-mcp help smoke was incomplete")
    help_result = _run(
        [str(graph_console), "--help"], cwd=scratch, env=clean_env, capture=True
    )
    if not all(
        command in help_result.stdout
        for command in ("validate", "doctor", "plan", "run", "status")
    ):
        raise GateError("installed graph-engineer help smoke was incomplete")
    version_result = _run(
        [str(graph_console), "--version"], cwd=scratch, env=clean_env, capture=True
    )
    expected_version = _project_version()
    if version_result.stdout.strip() != f"graph-engineer {expected_version}":
        raise GateError(
            "installed graph-engineer version does not match project.version"
        )
    capability_result = _run(
        [str(graph_console), "capabilities", "--json"],
        cwd=scratch,
        env=clean_env,
        capture=True,
    )
    assert_installed_capabilities(capability_result.stdout, expected_version)
    return wheel, sdist


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("verify", "publish"), nargs="?", default="verify"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="pass --dry-run to uv publish"
    )
    parser.add_argument(
        "--candidate-base",
        help="reviewed ancestor used to scan only candidate commit metadata",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "publish":
            refresh_public_history()
            refresh_release_tag()
            assert_release_context(require_remote=True)
        run_deterministic_gates(candidate_base=args.candidate_base)
        scratch_root = os.environ.get("TMPDIR")
        if not scratch_root:
            raise GateError("TMPDIR must point at disk-backed scratch")
        scratch_path = Path(scratch_root).expanduser().resolve(strict=True)
        with tempfile.TemporaryDirectory(
            prefix="graph-engineering-release-", dir=scratch_path
        ) as temporary:
            wheel, sdist = build_and_smoke(Path(temporary))
            if args.command == "publish":
                refresh_public_history()
                refresh_release_tag()
                tag = assert_release_context(require_remote=True)
                _run(
                    [
                        "uv",
                        "run",
                        "--frozen",
                        "--extra",
                        "dev",
                        "python",
                        "-m",
                        "graph_engineering.public_safety",
                        "--repo",
                        ".",
                        "--mode",
                        "history",
                    ]
                )
                _run(_candidate_metadata_command(args.candidate_base))
                publish = ["uv", "publish"]
                if args.dry_run:
                    publish.append("--dry-run")
                publish.extend((str(wheel), str(sdist)))
                print(f"publishing {tag} after all release gates", flush=True)
                _run(publish)
        print("release gates passed", flush=True)
        return 0
    except (GateError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"release gate failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
