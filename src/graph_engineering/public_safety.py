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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DENY_PATTERN_ENV = "GRAPH_ENGINEERING_DENY_PATTERNS"
DEFAULT_MAX_FILES = 25_000
DEFAULT_MAX_BLOBS = 100_000
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_ITEM_BYTES = 16 * 1024 * 1024


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
    parser.add_argument("--mode", choices=("all", "tree", "history"), default="all")
    parser.add_argument("--deny-pattern-file", type=Path)
    args = parser.parse_args(argv)
    try:
        rules = load_extra_rules(args.deny_pattern_file)
        findings: Iterable[Finding]
        if args.mode == "tree":
            findings = scan_worktree(args.repo, rules=rules)
        elif args.mode == "history":
            findings = scan_history(args.repo, rules=rules)
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
