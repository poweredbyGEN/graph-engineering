from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from graph_engineering.public_safety import (
    ScanError,
    load_extra_rules,
    main,
    scan_history,
    scan_worktree,
)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Public Safety Test")
    git(repo, "config", "user.email", "test@example.com")
    return repo


def commit(repo: Path, name: str, content: bytes) -> str:
    (repo / name).write_bytes(content)
    git(repo, "add", "--", name)
    git(repo, "commit", "-qm", name)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD:" + name],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def test_tree_scans_tracked_and_untracked_without_echoing_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path)
    commit(repo, "tracked.txt", b"endpoint=db.prod." + b"internal\n")
    secret = "ghp" + "_abcdefghijklmnopqrstuvwxyz123456"
    (repo / "untracked.txt").write_text(f"token={secret}\n")

    assert main(["--repo", str(repo), "--mode", "tree"]) == 1
    output = capsys.readouterr().out
    assert "tracked.txt\tprivate-hostname" in output
    assert "untracked.txt\tgithub-token" in output
    assert secret not in output


def test_history_scans_unique_reachable_blobs_after_secret_is_deleted(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    blob = commit(repo, "old.txt", b"service=" + b"10.23." + b"45.67\n")
    (repo / "old.txt").unlink()
    git(repo, "add", "--", "old.txt")
    git(repo, "commit", "-qm", "delete")

    findings = scan_history(repo)
    assert any(
        item.location.startswith(blob) and item.rule == "private-ipv4"
        for item in findings
    )


@pytest.mark.parametrize(
    ("content", "rule"),
    [
        ("path=/home/" + "alice/projects/widget", "absolute-user-home"),
        (
            'private_repositories = ["billing", "identity"]',
            "internal-repository-inventory",
        ),
        ("-----BEGIN " + "PRIVATE KEY-----", "private-key"),
        ("AKIA" + "ABCDEFGHIJKLMNOP", "aws-access-key"),
        ("password=" + "VeryLongRealishPassword42", "assigned-secret"),
    ],
)
def test_representative_private_material_is_rejected(
    tmp_path: Path, content: str, rule: str
) -> None:
    repo = make_repo(tmp_path)
    (repo / "candidate.txt").write_text(content)
    assert rule in {finding.rule for finding in scan_worktree(repo)}


def test_public_urls_localhost_and_synthetic_placeholders_are_allowed(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    (repo / "public.md").write_text(
        "https://github.com/poweredbyGEN/graph-engineering\n"
        "http://localhost:8000\nhttps://example.com\n"
        "api_key=${GRAPH_API_KEY}\npassword=changeme\n"
    )
    assert scan_worktree(repo) == []


def test_binary_files_are_ignored(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "image.bin").write_bytes(b"\x00ghp" + b"_abcdefghijklmnopqrstuvwxyz123456")
    assert scan_worktree(repo) == []


def test_private_deny_rules_are_loaded_by_env_without_echoing_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path)
    marker = "private-customer-codename"
    (repo / "notes.txt").write_text(marker)
    deny = tmp_path / "deny.tsv"
    deny.write_text("customer\tprivate-customer-[a-z]+\n")
    monkeypatch.setenv("GRAPH_ENGINEERING_DENY_PATTERNS", str(deny))

    assert load_extra_rules()[0].name == "private:customer"
    assert main(["--repo", str(repo), "--mode", "tree"]) == 1
    output = capsys.readouterr().out
    assert "private:customer" in output
    assert marker not in output


def test_limits_fail_closed_instead_of_skipping_content(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "large.txt").write_text("x" * 20)
    with pytest.raises(ScanError, match="exceeds byte limit"):
        scan_worktree(repo, max_item_bytes=10)


def test_no_shell_execution_is_used() -> None:
    source = (
        Path(__file__).parents[1] / "src" / "graph_engineering" / "public_safety.py"
    )
    assert "shell=True" not in source.read_text()
