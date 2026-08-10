from __future__ import annotations

import importlib.util
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest
import yaml

from graph_engineering.config import SubprocessAdapter, parse_agent_config

RELEASE_GATE_PATH = Path(__file__).parents[1] / "ops" / "release_gate.py"
SPEC = importlib.util.spec_from_file_location(
    "graph_engineering_release_gate", RELEASE_GATE_PATH
)
assert SPEC is not None and SPEC.loader is not None
release_gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_gate
SPEC.loader.exec_module(release_gate)
GateError = release_gate.GateError
assert_release_context = release_gate.assert_release_context
assert_installed_capabilities = release_gate.assert_installed_capabilities
assert_version_contract = release_gate.assert_version_contract
refresh_public_history = release_gate.refresh_public_history
refresh_release_tag = release_gate.refresh_release_tag
verify_archives = release_gate.verify_archives


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_release_gates_require_lockfile_consistency() -> None:
    # intent: a stale lock must fail rather than being silently rewritten during packaging.
    commands = [argv for argv, _environment in release_gate._gate_commands()]
    assert ("uv", "lock", "--check") in commands


def test_release_gate_rejects_installed_capability_manifest_drift(capsys) -> None:
    # intent: source tests cannot substitute for the actual wheel's discovery contract.
    from graph_engineering import cli

    assert cli.main(["capabilities", "--json"]) == 0
    output = capsys.readouterr().out
    assert_installed_capabilities(output, "0.1.0a1")
    drifted = output.replace('"worker_smoke":true', '"worker_smoke":false')
    with pytest.raises(GateError, match="worker smoke"):
        assert_installed_capabilities(drifted, "0.1.0a1")


def test_release_version_contract_rejects_stale_runtime_or_changelog(
    tmp_path: Path,
) -> None:
    # intent: a release version cannot drift between metadata, CLI code, and release notes.
    root = tmp_path / "project"
    (root / "src" / "graph_engineering").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nversion = "1.2.3"\nlicense = "MIT"\n'
        'license-files = ["LICENSE", "NOTICE.md"]\n',
        encoding="utf-8",
    )
    (root / "LICENSE").write_text("MIT\n", encoding="utf-8")
    (root / "NOTICE.md").write_text("Notices\n", encoding="utf-8")
    init = root / "src" / "graph_engineering" / "__init__.py"
    init.write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    changelog = root / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## [1.2.3] - 2026-08-08\n", encoding="utf-8")
    assert assert_version_contract(root) == "1.2.3"

    init.write_text('__version__ = "1.2.2"\n', encoding="utf-8")
    with pytest.raises(GateError, match="__version__"):
        assert_version_contract(root)
    init.write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    changelog.write_text("# Changelog\n\n## [1.2.2] - 2026-08-08\n", encoding="utf-8")
    with pytest.raises(GateError, match="CHANGELOG"):
        assert_version_contract(root)


def _release_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Packaging Test")
    _git(repo, "config", "user.email", "packaging@example.com")
    _git(repo, "add", "--", "pyproject.toml")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _push_release(repo: Path, tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    _git(repo, "branch", "-M", "main")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")
    _git(repo, "push", "-q", "origin", "v1.2.3")


def test_publish_requires_exact_version_tag_and_clean_tree(tmp_path: Path) -> None:
    # intent: no credential or publish flag can bypass the exact-tag release boundary.
    repo = _release_repo(tmp_path)
    with pytest.raises(GateError, match="exactly the release tag"):
        assert_release_context(repo)
    _git(repo, "tag", "v1.2.3")
    assert assert_release_context(repo) == "v1.2.3"
    with pytest.raises(GateError):
        assert_release_context(repo, require_remote=True)
    _push_release(repo, tmp_path)
    refresh_public_history(repo)
    assert refresh_release_tag(repo) == "v1.2.3"
    assert assert_release_context(repo, require_remote=True) == "v1.2.3"
    (repo / "feature.txt").write_text("unreviewed")
    _git(repo, "add", "--", "feature.txt")
    _git(repo, "commit", "-qm", "feature-only")
    _git(repo, "tag", "-f", "v1.2.3")
    with pytest.raises(GateError, match="fetched origin/main"):
        assert_release_context(repo, require_remote=True)
    (repo / "dirty.txt").write_text("not releasable")
    with pytest.raises(GateError, match="not clean"):
        assert_release_context(repo)


def _archives(
    tmp_path: Path,
    *,
    wheel_schema: bool,
    sdist_schema: bool,
    wheel_notice: bool = True,
    sdist_changelog: bool = True,
) -> tuple[Path, Path]:
    wheel = tmp_path / "package.whl"
    sdist = tmp_path / "package.tar.gz"
    entries = (
        "[console_scripts]\n"
        "graph-engineer = graph_engineering.cli:main\n"
        "graph-engineering-mcp = graph_engineering.mcp.__main__:main\n"
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("graph_engineering-1.0.dist-info/entry_points.txt", entries)
        if wheel_schema:
            archive.writestr(
                "graph_engineering/schemas/workflow-v1alpha1.schema.json", "{}"
            )
        archive.writestr("graph_engineering-1.0.dist-info/licenses/LICENSE", "MIT")
        if wheel_notice:
            archive.writestr(
                "graph_engineering-1.0.dist-info/licenses/NOTICE.md", "Notices"
            )
    payload = tmp_path / "workflow-v1alpha1.schema.json"
    payload.write_text("{}")
    with tarfile.open(sdist, "w:gz") as archive:
        if sdist_schema:
            archive.add(
                payload,
                arcname="package/graph_engineering/schemas/workflow-v1alpha1.schema.json",
            )
        root_files = ["LICENSE", "NOTICE.md"]
        if sdist_changelog:
            root_files.append("CHANGELOG.md")
        for filename in root_files:
            root_file = tmp_path / filename
            root_file.write_text(filename, encoding="utf-8")
            archive.add(root_file, arcname=f"package/{filename}")
    return wheel, sdist


def test_distribution_contract_requires_schema_and_console_entry_point(
    tmp_path: Path,
) -> None:
    wheel, sdist = _archives(tmp_path, wheel_schema=True, sdist_schema=True)
    verify_archives(wheel, sdist)

    # intent: losing non-Python package data must fail before a broken release is published.
    wheel, sdist = _archives(tmp_path, wheel_schema=False, sdist_schema=True)
    with pytest.raises(GateError, match="wheel is missing"):
        verify_archives(wheel, sdist)
    wheel, sdist = _archives(tmp_path, wheel_schema=True, sdist_schema=False)
    with pytest.raises(GateError, match="sdist is missing"):
        verify_archives(wheel, sdist)
    wheel, sdist = _archives(
        tmp_path, wheel_schema=True, sdist_schema=True, wheel_notice=False
    )
    with pytest.raises(GateError, match="NOTICE.md"):
        verify_archives(wheel, sdist)
    wheel, sdist = _archives(
        tmp_path, wheel_schema=True, sdist_schema=True, sdist_changelog=False
    )
    with pytest.raises(GateError, match="CHANGELOG.md"):
        verify_archives(wheel, sdist)

    # intent: packaging the runtime without its operator CLI must fail the release gate.
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "graph_engineering-1.0.dist-info/entry_points.txt",
            "[console_scripts]\ngraph-engineering-mcp = graph_engineering.mcp.__main__:main\n",
        )
        archive.writestr(
            "graph_engineering/schemas/workflow-v1alpha1.schema.json", "{}"
        )
        archive.writestr("graph_engineering-1.0.dist-info/licenses/LICENSE", "MIT")
        archive.writestr(
            "graph_engineering-1.0.dist-info/licenses/NOTICE.md", "Notices"
        )
    with pytest.raises(GateError, match="graph-engineer"):
        verify_archives(wheel, sdist)


def test_kimi_and_glm_examples_use_the_implemented_restricted_subprocess_route() -> (
    None
):
    # intent: public setup must not advertise the reserved direct-HTTP executor as working.
    root = Path(__file__).parents[1]
    with (root / "subagents.example.toml").open("rb") as handle:
        config = parse_agent_config(tomllib.load(handle))
    for name in ("kimi-k3", "glm-5.2"):
        profile = config.profiles[name]
        assert isinstance(profile.adapter, SubprocessAdapter)
        assert profile.adapter.argv[:2] == ("opencode", "run")
        assert "graph-worker" in profile.adapter.argv
        assert profile.capabilities.write and profile.capabilities.worktree
        assert not profile.capabilities.mcp

    agent = root / "examples" / "opencode-agents" / "graph-worker.md"
    frontmatter = agent.read_text().split("---", 2)[1]
    permissions = yaml.safe_load(frontmatter)["permission"]
    assert permissions["*"] == "deny"
    assert permissions["external_directory"] == "deny"
    assert permissions["task"] == "deny"
    assert permissions["webfetch"] == "deny"
    assert permissions["bash"] == "deny"


def test_grok_example_denies_ambient_mcp_web_memory_shell_and_subagents() -> None:
    # intent: a developer's global Grok setup must not leak authority into graph workers.
    root = Path(__file__).parents[1]
    with (root / "subagents.example.toml").open("rb") as handle:
        config = parse_agent_config(tomllib.load(handle))
    profile = config.profiles["grok"]
    assert isinstance(profile.adapter, SubprocessAdapter)
    argv = profile.adapter.argv
    assert not profile.capabilities.mcp
    assert argv[argv.index("--tools") + 1] == "read_file,grep,list_dir,search_replace"
    assert argv[argv.index("--deny") + 1] == "MCPTool(*)"
    for flag in ("--disable-web-search", "--no-memory", "--no-subagents"):
        assert flag in argv
    assert argv[argv.index("--sandbox") + 1] == "strict"
    assert "run_terminal_cmd" not in argv[argv.index("--tools") + 1]


def test_claude_and_codex_examples_do_not_inherit_ambient_mcp() -> None:
    # intent: mcp=false must be enforced by the process invocation, not just documentation.
    root = Path(__file__).parents[1]
    with (root / "subagents.example.toml").open("rb") as handle:
        config = parse_agent_config(tomllib.load(handle))

    claude = config.profiles["claude"]
    assert isinstance(claude.adapter, SubprocessAdapter)
    assert not claude.capabilities.mcp
    assert "--strict-mcp-config" in claude.adapter.argv
    assert claude.adapter.argv[claude.adapter.argv.index("--mcp-config") + 1] == (
        '{{"mcpServers":{{}}}}'
    )
    assert "Bash" not in claude.adapter.argv[claude.adapter.argv.index("--tools") + 1]

    codex = config.profiles["codex"]
    assert isinstance(codex.adapter, SubprocessAdapter)
    assert not codex.capabilities.mcp
    assert "--ignore-user-config" in codex.adapter.argv
    assert "--output-schema" not in codex.adapter.argv
