from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from graph_engineering.public_safety import (
    ScanError,
    _commit_metadata_rules,
    load_extra_rules,
    main,
    scan_candidate_commit_metadata,
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


def head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def use_safe_commit_identity(repo: Path) -> None:
    git(repo, "config", "user.name", "poweredbyGEN")
    git(repo, "config", "user.email", "poweredbygen@users.noreply.github.com")


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


def test_candidate_metadata_ignores_unsafe_legacy_base_and_accepts_safe_candidate(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    commit(repo, "legacy.txt", b"legacy\n")
    base = head(repo)
    use_safe_commit_identity(repo)
    commit(repo, "candidate.txt", b"candidate\n")

    # intent: a legacy identity on the trusted base must not block every future PR.
    assert scan_candidate_commit_metadata(repo, trusted_base=base) == []


def test_candidate_metadata_rejects_unapproved_person_identity(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    use_safe_commit_identity(repo)
    commit(repo, "base.txt", b"base\n")
    base = head(repo)
    git(repo, "config", "user.name", "Example Person")
    git(repo, "config", "user.email", "person@example.com")
    commit(repo, "candidate.txt", b"candidate\n")

    # intent: deleting private data from blobs cannot hide it in candidate identities.
    assert {
        item.rule for item in scan_candidate_commit_metadata(repo, trusted_base=base)
    } == {
        "commit-email-policy",
        "commit-name-policy",
    }


def test_candidate_metadata_redacts_sensitive_commit_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path)
    use_safe_commit_identity(repo)
    commit(repo, "base.txt", b"base\n")
    base = head(repo)
    (repo / "candidate.txt").write_text("safe blob\n")
    git(repo, "add", "--", "candidate.txt")
    secret = "ghp" + "_abcdefghijklmnopqrstuvwxyz123456"
    git(repo, "commit", "-qm", f"accidental {secret}")

    assert (
        main(
            [
                "--repo",
                str(repo),
                "--mode",
                "candidate-metadata",
                "--candidate-base",
                base,
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "commit-sensitive-pattern-policy" in output
    assert secret not in output
    assert "accidental" not in output


def test_candidate_metadata_rejects_personal_identity_trailers(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    use_safe_commit_identity(repo)
    commit(repo, "base.txt", b"base\n")
    base = head(repo)
    (repo / "candidate.txt").write_text("safe blob\n")
    git(repo, "add", "--", "candidate.txt")
    git(
        repo,
        "commit",
        "-qm",
        "candidate\n\nCo-authored-by: Example Person <person@example.com>",
    )

    # intent: a safe primary identity cannot smuggle a person's identity in trailers.
    assert {
        item.rule for item in scan_candidate_commit_metadata(repo, trusted_base=base)
    } == {"commit-email-policy", "commit-name-policy"}


def test_candidate_metadata_accepts_exact_project_approved_generic_identity(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    use_safe_commit_identity(repo)
    commit(repo, "base.txt", b"base\n")
    base = head(repo)
    git(repo, "config", "user.name", "Release Automation")
    git(repo, "config", "user.email", "release@example.org")
    commit(repo, "candidate.txt", b"candidate\n")

    assert (
        scan_candidate_commit_metadata(
            repo,
            trusted_base=base,
            allowed_names=["Release Automation"],
            allowed_emails=["release@example.org"],
        )
        == []
    )


def test_candidate_metadata_fails_closed_on_count_and_non_ancestor(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    use_safe_commit_identity(repo)
    commit(repo, "base.txt", b"base\n")
    base = head(repo)
    commit(repo, "candidate.txt", b"candidate\n")
    candidate = head(repo)

    with pytest.raises(ScanError, match="commit-count limit"):
        scan_candidate_commit_metadata(repo, trusted_base=base, max_commits=0)
    with pytest.raises(ScanError, match="not an ancestor"):
        scan_candidate_commit_metadata(repo, trusted_base=candidate, candidate=base)


def test_candidate_metadata_rejects_controls_and_oversized_fields() -> None:
    tree = b"0" * 40
    raw = (
        b"tree "
        + tree
        + b"\nauthor poweredbyGEN <poweredbygen@users.noreply.github.com> 1 +0000"
        + b"\ncommitter poweredbyGEN\x1b <poweredbygen@users.noreply.github.com> 1 +0000"
        + b"\n\nmessage"
    )
    assert "commit-control-policy" in _commit_metadata_rules(
        raw,
        allowed_names=frozenset({"poweredbyGEN", "poweredbyGEN\x1b"}),
        allowed_emails=frozenset(),
        rules=(),
        max_commit_bytes=1_000,
        max_message_bytes=1_000,
    )
    assert _commit_metadata_rules(
        raw,
        allowed_names=frozenset({"poweredbyGEN"}),
        allowed_emails=frozenset(),
        rules=(),
        max_commit_bytes=10,
        max_message_bytes=1_000,
    ) == {"commit-size-policy"}
