"""Deterministic public-safety scans for a repository and its reachable history.

The scanner reports only locations and rule identifiers.  It deliberately never
echoes matched text, because the match itself may be a credential.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import subprocess
import sys
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DENY_PATTERN_ENV = "GRAPH_ENGINEERING_DENY_PATTERNS"
DEFAULT_MAX_FILES = 25_000
DEFAULT_MAX_BLOBS = 100_000
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_ITEM_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_COMMITS = 2_048
DEFAULT_MAX_COMMIT_BYTES = 256 * 1024
DEFAULT_MAX_COMMIT_MESSAGE_BYTES = 64 * 1024
DEFAULT_MAX_IDENTITY_NAME_BYTES = 128
DEFAULT_MAX_IDENTITY_EMAIL_BYTES = 254
TRUSTED_COMMIT_BASE_ENV = "GRAPH_ENGINEERING_TRUSTED_COMMIT_BASE"

# These are public project/service identities, not people. Operators may add an exact
# project-approved generic identity at the CLI boundary; no personal identity is inferred.
DEFAULT_ALLOWED_COMMIT_NAMES = frozenset(
    {"poweredbyGEN", "Graph Engineering", "github-actions[bot]", "dependabot[bot]"}
)
_GITHUB_NOREPLY_EMAIL = re.compile(
    r"(?i)^[a-z0-9][a-z0-9._+-]*@users\.noreply\.github\.com$"
)
_MESSAGE_EMAIL = re.compile(
    r"(?i)(?<![a-z0-9._+-])([a-z0-9][a-z0-9._+-]*@[a-z0-9.-]+\.[a-z]{2,})(?![a-z0-9.-])"
)
_IDENTITY_TRAILER = re.compile(
    r"(?im)^(?:co-authored-by|signed-off-by|reviewed-by|tested-by|reported-by|helped-by):"
    r"\s*(.+?)\s*<([^<>]+)>\s*$"
)
_ZERO_SHA = re.compile(r"^0+$")


class ScanError(RuntimeError):
    """The scan could not prove that it examined the requested scope."""


@dataclass(frozen=True, order=True)
class Finding:
    source: str
    location: str
    rule: str


@dataclass(frozen=True)
class PatternRule:
    name: str
    expression: re.Pattern[str]


_BUILTIN_RULES = (
    PatternRule(
        "private-hostname",
        re.compile(
            r"(?i)(?<![a-z0-9.-])(?:[a-z0-9-]+\.)+(?:internal|intranet|lan)(?![a-z0-9.-])"
        ),
    ),
    PatternRule(
        "absolute-user-home",
        re.compile(
            r"(?<![a-z0-9_])(?:/"
            + "root"
            + r"/|/home/[a-z0-9._-]+/|/Users/[a-z0-9._-]+/)",
            re.IGNORECASE,
        ),
    ),
    PatternRule(
        "internal-repository-inventory",
        re.compile(
            r"(?im)^\s*(?:internal|private)[_-]?repos(?:itories)?\s*[:=]\s*[\[(]"
        ),
    ),
    PatternRule(
        "private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
    ),
    PatternRule(
        "aws-access-key",
        re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    ),
    PatternRule(
        "github-token",
        re.compile(
            r"(?<![A-Za-z0-9_])gh(?:p|o|u|s|r)_[A-Za-z0-9]{20,}(?![A-Za-z0-9_])"
        ),
    ),
    PatternRule(
        "gitlab-token",
        re.compile(r"(?<![A-Za-z0-9_-])glpat-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    ),
    PatternRule(
        "slack-token",
        re.compile(r"(?<![A-Za-z0-9-])xox[baprs]-[A-Za-z0-9-]{20,}(?![A-Za-z0-9-])"),
    ),
    PatternRule(
        "assigned-secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password)\b"
            r"\s*[:=]\s*['\"]?(?!\$\{|\$|<|example|changeme|redacted|test|dummy|your[_-])"
            r"[A-Za-z0-9+/_.=-]{20,}"
        ),
    ),
)

_PRIVATE_IPV4_CANDIDATE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


def _git(repo: Path, args: Sequence[str], *, input_bytes: bytes | None = None) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=input_bytes,
            capture_output=True,
            check=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScanError(f"git command failed: {' '.join(args[:2])}") from exc
    return completed.stdout


def _resolve_commit(repo: Path, revision: str) -> str:
    if not revision or "\x00" in revision or "\n" in revision or "\r" in revision:
        raise ScanError("candidate revision is invalid")
    output = _git(
        repo,
        ["rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"],
    ).strip()
    try:
        return output.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise ScanError("candidate revision did not resolve to an object ID") from exc


def resolve_trusted_candidate_base(repo: Path, explicit: str | None = None) -> str:
    """Resolve a reviewed base without treating all legacy history as a candidate.

    An explicit argument wins, followed by the dedicated release environment variable,
    Woodpecker's pull-request target, and Woodpecker's pre-push SHA. Outside CI a feature
    checkout may use ``origin/main`` only when it differs from ``HEAD``. A merged checkout
    must name its prior reviewed base explicitly, which prevents an empty range from
    masquerading as release evidence.
    """

    if explicit:
        return explicit
    if configured := os.environ.get(TRUSTED_COMMIT_BASE_ENV):
        return configured
    if target := os.environ.get("CI_COMMIT_TARGET_BRANCH"):
        return f"origin/{target}"
    before = os.environ.get("CI_COMMIT_BEFORE_SHA", "")
    if before and not _ZERO_SHA.fullmatch(before):
        return before
    try:
        main = _resolve_commit(repo, "origin/main")
        head = _resolve_commit(repo, "HEAD")
    except ScanError as exc:
        raise ScanError("trusted candidate base is required") from exc
    if main != head:
        return "origin/main"
    raise ScanError("trusted candidate base is required")


def load_extra_rules(path: Path | None = None) -> tuple[PatternRule, ...]:
    """Load ``rule<TAB>regex`` lines from a private, operator-owned file."""

    configured = path or (
        Path(value) if (value := os.environ.get(DEFAULT_DENY_PATTERN_ENV)) else None
    )
    if configured is None:
        return ()
    try:
        lines = configured.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ScanError(f"cannot read deny-pattern file: {configured}") from exc

    rules: list[PatternRule] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            name, expression = line.split("\t", 1)
        except ValueError as exc:
            raise ScanError(f"invalid deny-pattern record at line {number}") from exc
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", name):
            raise ScanError(f"invalid deny-pattern rule name at line {number}")
        try:
            rules.append(PatternRule(f"private:{name}", re.compile(expression)))
        except re.error as exc:
            raise ScanError(f"invalid deny-pattern regex at line {number}") from exc
    return tuple(rules)


def _is_probably_binary(content: bytes) -> bool:
    return b"\x00" in content[:8192]


def _scan_content(
    content: bytes, *, source: str, location: str, rules: Sequence[PatternRule]
) -> list[Finding]:
    if _is_probably_binary(content):
        return []
    text = content.decode("utf-8", errors="replace")
    found = {rule.name for rule in rules if rule.expression.search(text)}
    for candidate in _PRIVATE_IPV4_CANDIDATE.findall(text):
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if (
            address.version == 4
            and address.is_private
            and not address.is_loopback
            and not address.is_link_local
        ):
            found.add("private-ipv4")
            break
    return [Finding(source, location, name) for name in sorted(found)]


def _check_item_size(size: int, location: str, max_item_bytes: int) -> None:
    if size > max_item_bytes:
        raise ScanError(f"scan item exceeds byte limit: {location}")


def scan_worktree(
    repo: Path,
    *,
    rules: Sequence[PatternRule] = (),
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_item_bytes: int = DEFAULT_MAX_ITEM_BYTES,
) -> list[Finding]:
    """Scan tracked and non-ignored untracked regular files in stable order."""

    repo = repo.resolve()
    names = _git(
        repo, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    ).split(b"\0")
    paths = sorted(
        name.decode("utf-8", errors="surrogateescape") for name in names if name
    )
    if len(paths) > max_files:
        raise ScanError("working tree exceeds file-count limit")

    findings: list[Finding] = []
    total = 0
    active_rules = (*_BUILTIN_RULES, *rules)
    for relative in paths:
        path = repo / relative
        if path.is_symlink() or not path.is_file():
            continue
        try:
            size = path.stat().st_size
            _check_item_size(size, relative, max_item_bytes)
            total += size
            if total > max_bytes:
                raise ScanError("working tree exceeds total-byte limit")
            content = path.read_bytes()
        except OSError as exc:
            raise ScanError(f"cannot read working-tree file: {relative}") from exc
        findings.extend(
            _scan_content(content, source="tree", location=relative, rules=active_rules)
        )
    return sorted(set(findings))


def _reachable_objects(repo: Path, max_blobs: int) -> list[tuple[str, str]]:
    records = _git(repo, ["rev-list", "--objects", "--all"]).splitlines()
    object_paths: dict[str, str] = {}
    for record in records:
        object_id, separator, path = record.partition(b" ")
        oid = object_id.decode("ascii", errors="strict")
        object_paths.setdefault(
            oid, path.decode("utf-8", errors="replace") if separator else ""
        )

    if not object_paths:
        return []
    check_input = b"".join(f"{oid}\n".encode("ascii") for oid in object_paths)
    metadata = _git(
        repo,
        ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
        input_bytes=check_input,
    )
    blobs: list[tuple[str, str]] = []
    for line in metadata.splitlines():
        oid_bytes, kind, _size = line.split(b" ", 2)
        if kind == b"blob":
            oid = oid_bytes.decode("ascii")
            blobs.append((oid, object_paths.get(oid, "")))
    blobs.sort()
    if len(blobs) > max_blobs:
        raise ScanError("reachable history exceeds blob-count limit")
    return blobs


def scan_history(
    repo: Path,
    *,
    rules: Sequence[PatternRule] = (),
    max_blobs: int = DEFAULT_MAX_BLOBS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_item_bytes: int = DEFAULT_MAX_ITEM_BYTES,
) -> list[Finding]:
    """Scan every unique blob reachable from every local ref."""

    repo = repo.resolve()
    active_rules = (*_BUILTIN_RULES, *rules)
    findings: list[Finding] = []
    total = 0
    for oid, historical_path in _reachable_objects(repo, max_blobs):
        size_text = _git(repo, ["cat-file", "-s", oid]).strip()
        try:
            size = int(size_text)
        except ValueError as exc:
            raise ScanError(f"invalid blob metadata: {oid}") from exc
        _check_item_size(size, oid, max_item_bytes)
        total += size
        if total > max_bytes:
            raise ScanError("reachable history exceeds total-byte limit")
        content = _git(repo, ["cat-file", "blob", oid])
        location = f"{oid}:{historical_path}" if historical_path else oid
        findings.extend(
            _scan_content(
                content, source="history", location=location, rules=active_rules
            )
        )
    return sorted(set(findings))


def _has_disallowed_control(text: str, *, allow_layout: bool = False) -> bool:
    allowed = {"\n", "\t"} if allow_layout else set()
    return any(
        character not in allowed
        and unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in text
    )


def _parse_commit_identity(line: bytes, header: bytes) -> tuple[str, str] | None:
    if not line.startswith(header + b" "):
        return None
    value = line[len(header) + 1 :]
    match = re.fullmatch(rb"(.+) <([^<>]+)> [0-9]+ [+-][0-9]{4}", value)
    if match is None:
        raise ScanError("candidate commit identity is malformed")
    try:
        name = match.group(1).decode("utf-8", errors="strict")
        email = match.group(2).decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise ScanError("candidate commit identity encoding is invalid") from exc
    return name, email


def _commit_metadata_rules(
    content: bytes,
    *,
    allowed_names: frozenset[str],
    allowed_emails: frozenset[str],
    rules: Sequence[PatternRule],
    max_commit_bytes: int,
    max_message_bytes: int,
) -> set[str]:
    if len(content) > max_commit_bytes:
        return {"commit-size-policy"}
    headers, separator, message_bytes = content.partition(b"\n\n")
    if not separator:
        return {"commit-format-policy"}
    if len(message_bytes) > max_message_bytes:
        return {"commit-message-size-policy"}
    try:
        message = message_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return {"commit-message-encoding-policy"}

    identities: dict[bytes, tuple[str, str]] = {}
    for header in (b"author", b"committer"):
        matches = [
            identity
            for line in headers.splitlines()
            if (identity := _parse_commit_identity(line, header)) is not None
        ]
        if len(matches) != 1:
            return {"commit-format-policy"}
        identities[header] = matches[0]

    found: set[str] = set()
    for name, email in identities.values():
        if len(name.encode("utf-8")) > DEFAULT_MAX_IDENTITY_NAME_BYTES:
            found.add("commit-identity-size-policy")
        if len(email.encode("ascii")) > DEFAULT_MAX_IDENTITY_EMAIL_BYTES:
            found.add("commit-identity-size-policy")
        if _has_disallowed_control(name) or _has_disallowed_control(email):
            found.add("commit-control-policy")
        if name not in allowed_names:
            found.add("commit-name-policy")
        normalized_email = email.casefold()
        if (
            normalized_email not in allowed_emails
            and not _GITHUB_NOREPLY_EMAIL.fullmatch(email)
        ):
            found.add("commit-email-policy")
    if _has_disallowed_control(message, allow_layout=True):
        found.add("commit-control-policy")
    for message_email in _MESSAGE_EMAIL.findall(message):
        normalized = message_email.casefold()
        if normalized not in allowed_emails and not _GITHUB_NOREPLY_EMAIL.fullmatch(
            message_email
        ):
            found.add("commit-email-policy")
    for trailer in _IDENTITY_TRAILER.finditer(message):
        if trailer.group(1) not in allowed_names:
            found.add("commit-name-policy")

    combined = "\n".join(
        [
            *(part for identity in identities.values() for part in identity),
            message,
        ]
    ).encode("utf-8")
    if _scan_content(
        combined,
        source="commit",
        location="redacted",
        rules=(*_BUILTIN_RULES, *rules),
    ):
        found.add("commit-sensitive-pattern-policy")
    return found


def scan_candidate_commit_metadata(
    repo: Path,
    *,
    trusted_base: str,
    candidate: str = "HEAD",
    allowed_names: Sequence[str] = (),
    allowed_emails: Sequence[str] = (),
    rules: Sequence[PatternRule] = (),
    max_commits: int = DEFAULT_MAX_COMMITS,
    max_commit_bytes: int = DEFAULT_MAX_COMMIT_BYTES,
    max_message_bytes: int = DEFAULT_MAX_COMMIT_MESSAGE_BYTES,
) -> list[Finding]:
    """Scan only commits introduced after an explicit reviewed base.

    Findings deliberately expose only a short object ID and stable rule ID. The base
    must be an ancestor of the candidate so a misleading range cannot hide commits.
    """

    repo = repo.resolve()
    base_oid = _resolve_commit(repo, trusted_base)
    candidate_oid = _resolve_commit(repo, candidate)
    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "merge-base",
                "--is-ancestor",
                base_oid,
                candidate_oid,
            ],
            capture_output=True,
            check=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScanError("trusted candidate base is not an ancestor") from exc
    records = _git(repo, ["rev-list", "--reverse", f"{base_oid}..{candidate_oid}"])
    commit_ids = records.decode("ascii", errors="strict").splitlines()
    if len(commit_ids) > max_commits:
        raise ScanError("candidate range exceeds commit-count limit")

    approved_names = frozenset({*DEFAULT_ALLOWED_COMMIT_NAMES, *allowed_names})
    approved_emails = frozenset(email.casefold() for email in allowed_emails)
    if any(
        not value or _has_disallowed_control(value)
        for value in (*approved_names, *approved_emails)
    ):
        raise ScanError("commit identity policy is invalid")

    findings: list[Finding] = []
    for oid in commit_ids:
        size_bytes = _git(repo, ["cat-file", "-s", oid]).strip()
        try:
            size = int(size_bytes)
        except ValueError as exc:
            raise ScanError("candidate commit size is invalid") from exc
        short_oid = oid[:12]
        if size > max_commit_bytes:
            findings.append(Finding("commit", short_oid, "commit-size-policy"))
            continue
        content = _git(repo, ["cat-file", "commit", oid])
        for rule in sorted(
            _commit_metadata_rules(
                content,
                allowed_names=approved_names,
                allowed_emails=approved_emails,
                rules=rules,
                max_commit_bytes=max_commit_bytes,
                max_message_bytes=max_message_bytes,
            )
        ):
            findings.append(Finding("commit", short_oid, rule))
    return sorted(set(findings))


def scan_repository(
    repo: Path, *, deny_pattern_file: Path | None = None
) -> list[Finding]:
    rules = load_extra_rules(deny_pattern_file)
    return sorted({*scan_worktree(repo, rules=rules), *scan_history(repo, rules=rules)})


def _format_finding(finding: Finding) -> str:
    return f"{finding.source}\t{finding.location}\t{finding.rule}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan a public repository without printing matched content"
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--mode",
        choices=("all", "tree", "history", "candidate-metadata"),
        default="all",
    )
    parser.add_argument("--deny-pattern-file", type=Path)
    parser.add_argument("--candidate-base")
    parser.add_argument("--candidate", default="HEAD")
    parser.add_argument("--allowed-commit-name", action="append", default=[])
    parser.add_argument("--allowed-commit-email", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        rules = load_extra_rules(args.deny_pattern_file)
        findings: Iterable[Finding]
        if args.mode == "tree":
            findings = scan_worktree(args.repo, rules=rules)
        elif args.mode == "history":
            findings = scan_history(args.repo, rules=rules)
        elif args.mode == "candidate-metadata":
            base = resolve_trusted_candidate_base(args.repo, args.candidate_base)
            findings = scan_candidate_commit_metadata(
                args.repo,
                trusted_base=base,
                candidate=args.candidate,
                allowed_names=args.allowed_commit_name,
                allowed_emails=args.allowed_commit_email,
                rules=rules,
            )
        else:
            findings = (
                *scan_worktree(args.repo, rules=rules),
                *scan_history(args.repo, rules=rules),
            )
        unique = sorted(set(findings))
    except ScanError as exc:
        print(f"public-safety scan incomplete: {exc}", file=sys.stderr)
        return 2
    for finding in unique:
        print(_format_finding(finding))
    return 1 if unique else 0


if __name__ == "__main__":
    raise SystemExit(main())
