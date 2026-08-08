"""Isolated git worktrees and transferable change-set artifacts."""

from __future__ import annotations

import base64
import binascii
import fcntl
import fnmatch
import hashlib
import os
import re
import subprocess
import tempfile
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class WorktreeError(RuntimeError):
    """A worktree or integration operation could not be proven safe."""


@dataclass(frozen=True)
class Worktree:
    run_id: str
    node_id: str
    path: Path
    branch: str
    base_sha: str


@dataclass(frozen=True)
class ChangeSet:
    base_sha: str
    patch_b64: str
    untracked_b64: Mapping[str, str]
    changed_paths: tuple[str, ...]
    digest: str

    @property
    def patch(self) -> bytes:
        try:
            return base64.b64decode(self.patch_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise WorktreeError("invalid tracked patch payload") from exc


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_THREAD_LOCK = threading.Lock()


def _run(
    argv: Sequence[str],
    *,
    cwd: Path,
    input_bytes: bytes | None = None,
    timeout: int = 120,
) -> bytes:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            input=input_bytes,
            capture_output=True,
            check=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace")[-1000:]
        raise WorktreeError(
            f"command failed: {argv[0]} {argv[1] if len(argv) > 1 else ''}: {stderr}"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorktreeError(f"command failed: {argv[0]}") from exc
    return completed.stdout


def _git(cwd: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return _run(("git", *args), cwd=cwd, input_bytes=input_bytes)


def _safe_name(value: str, field: str) -> str:
    if not _NAME.fullmatch(value):
        raise WorktreeError(f"{field} is not a portable worktree name")
    return value


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or ".git" in path.parts:
        raise WorktreeError(f"unsafe repository path: {value!r}")
    normalized = path.as_posix()
    if normalized in {".", ""}:
        raise WorktreeError(f"unsafe repository path: {value!r}")
    return normalized


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _change_digest(
    base_sha: str,
    patch: bytes,
    untracked: Mapping[str, str],
    changed_paths: Sequence[str],
) -> str:
    digest = hashlib.sha256()
    digest.update(base_sha.encode("ascii"))
    digest.update(b"\0" + patch)
    for relative in changed_paths:
        digest.update(b"\0path\0" + relative.encode("utf-8"))
    for relative, encoded in sorted(untracked.items()):
        digest.update(
            b"\0file\0" + relative.encode("utf-8") + b"\0" + encoded.encode("ascii")
        )
    return digest.hexdigest()


def _patch_paths(target: Path, patch: bytes) -> set[str]:
    if not patch:
        return set()
    output = _git(target, "apply", "--numstat", "-z", "--binary", input_bytes=patch)
    paths: set[str] = set()
    for record in output.split(b"\0"):
        if not record:
            continue
        fields = record.split(b"\t", 2)
        if len(fields) != 3:
            raise WorktreeError("git returned malformed patch metadata")
        paths.add(_safe_relative(fields[2].decode("utf-8", errors="surrogateescape")))
    return paths


@contextmanager
def _repository_lock(common_git_dir: Path) -> Iterator[None]:
    common_git_dir.mkdir(parents=True, exist_ok=True)
    lock_path = common_git_dir / "graph-engineering-worktrees.lock"
    with _THREAD_LOCK, lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class WorktreeManager:
    """Create isolated writers and move their diffs across an explicit edge."""

    def __init__(
        self,
        repo: str | Path,
        root: str | Path | None = None,
        *,
        max_patch_bytes: int = 32 * 1024 * 1024,
        max_untracked_bytes: int = 32 * 1024 * 1024,
    ):
        candidate = Path(repo).resolve()
        top = Path(
            _git(candidate, "rev-parse", "--show-toplevel").decode().strip()
        ).resolve()
        if candidate != top:
            raise WorktreeError(f"repo must be its git top-level: {top}")
        self.repo = top
        default_root = top / ".claude" / "worktrees" / "graph-runs"
        self.root = Path(root).resolve() if root is not None else default_root
        if not _within(self.root, self.repo):
            raise WorktreeError("worktree root must be nested under the repository")
        common = _git(top, "rev-parse", "--git-common-dir").decode().strip()
        self.common_git_dir = (
            (top / common).resolve() if not Path(common).is_absolute() else Path(common)
        )
        self.max_patch_bytes = max_patch_bytes
        self.max_untracked_bytes = max_untracked_bytes

    def resolve_base(self, base: str) -> str:
        try:
            value = (
                _git(
                    self.repo,
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    f"{base}^{{commit}}",
                )
                .decode()
                .strip()
            )
        except WorktreeError as exc:
            raise WorktreeError(f"invalid base revision: {base!r}") from exc
        if not re.fullmatch(r"[0-9a-f]{40,64}", value):
            raise WorktreeError("git returned an invalid base object id")
        return value

    def create(self, run_id: str, node_id: str, *, base: str) -> Worktree:
        run_id = _safe_name(run_id, "run_id")
        node_id = _safe_name(node_id, "node_id")
        base_sha = self.resolve_base(base)
        path = (self.root / run_id / node_id).resolve()
        if not _within(path, self.root):
            raise WorktreeError("resolved worktree path escaped its root")
        branch = f"graph/{run_id}/{node_id}"
        path.parent.mkdir(parents=True, exist_ok=True)
        with _repository_lock(self.common_git_dir):
            if path.exists():
                raise WorktreeError(f"worktree path already exists: {path}")
            branch_exists = (
                subprocess.run(
                    ("git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"),
                    cwd=self.repo,
                    check=False,
                    timeout=30,
                ).returncode
                == 0
            )
            if branch_exists:
                raise WorktreeError(f"worktree branch already exists: {branch}")
            _git(self.repo, "worktree", "add", "-b", branch, str(path), base_sha)
        actual = _git(path, "rev-parse", "HEAD").decode().strip()
        if actual != base_sha:
            raise WorktreeError(f"created worktree at {actual}, expected {base_sha}")
        return Worktree(run_id, node_id, path, branch, base_sha)

    def capture(self, worktree: Worktree, *, write_scope: Sequence[str]) -> ChangeSet:
        if not _within(worktree.path, self.root):
            raise WorktreeError("worktree is outside the configured root")
        actual_base = (
            _git(worktree.path, "merge-base", worktree.base_sha, "HEAD")
            .decode()
            .strip()
        )
        if actual_base != worktree.base_sha:
            raise WorktreeError(
                "worktree history no longer descends from its declared base"
            )

        patch = _git(
            worktree.path,
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            worktree.base_sha,
            "--",
        )
        if len(patch) > self.max_patch_bytes:
            raise WorktreeError("tracked patch exceeds byte limit")
        tracked = {
            _safe_relative(value.decode("utf-8", errors="surrogateescape"))
            for value in _git(
                worktree.path, "diff", "--name-only", "-z", worktree.base_sha, "--"
            ).split(b"\0")
            if value
        }
        untracked_paths = sorted(
            _safe_relative(value.decode("utf-8", errors="surrogateescape"))
            for value in _git(
                worktree.path, "ls-files", "--others", "--exclude-standard", "-z"
            ).split(b"\0")
            if value
        )
        untracked: dict[str, str] = {}
        total_untracked = 0
        for relative in untracked_paths:
            source = worktree.path / relative
            if (
                source.is_symlink()
                or not source.is_file()
                or not _within(source, worktree.path)
            ):
                raise WorktreeError(
                    f"untracked output is not a regular in-tree file: {relative}"
                )
            payload = source.read_bytes()
            total_untracked += len(payload)
            if total_untracked > self.max_untracked_bytes:
                raise WorktreeError("untracked outputs exceed byte limit")
            untracked[relative] = base64.b64encode(payload).decode("ascii")

        changed = tuple(sorted(tracked | set(untracked)))
        if not write_scope and changed:
            raise WorktreeError("node changed files without a declared write scope")
        outside = [
            path
            for path in changed
            if not any(fnmatch.fnmatchcase(path, pattern) for pattern in write_scope)
        ]
        if outside:
            raise WorktreeError(f"changes escaped write scope: {outside}")

        digest = _change_digest(worktree.base_sha, patch, untracked, changed)
        return ChangeSet(
            base_sha=worktree.base_sha,
            patch_b64=base64.b64encode(patch).decode("ascii"),
            untracked_b64=untracked,
            changed_paths=changed,
            digest=digest,
        )

    def apply(
        self, target: str | Path, change: ChangeSet, *, write_scope: Sequence[str]
    ) -> None:
        target_path = Path(target).resolve()
        if not _within(target_path, self.root):
            raise WorktreeError(
                "integration target is outside the configured worktree root"
            )
        if len(change.patch_b64) > 4 * ((self.max_patch_bytes + 2) // 3):
            raise WorktreeError("tracked patch exceeds byte limit")
        patch = change.patch
        if len(patch) > self.max_patch_bytes:
            raise WorktreeError("tracked patch exceeds byte limit")
        target_base = (
            _git(target_path, "merge-base", change.base_sha, "HEAD").decode().strip()
        )
        if target_base != change.base_sha:
            raise WorktreeError(
                "integration target does not descend from the change-set base"
            )

        decoded: dict[str, bytes] = {}
        total = 0
        for raw_path, encoded in change.untracked_b64.items():
            relative = _safe_relative(raw_path)
            remaining = self.max_untracked_bytes - total
            if len(encoded) > 4 * ((remaining + 2) // 3):
                raise WorktreeError("untracked outputs exceed byte limit")
            try:
                payload = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise WorktreeError(f"invalid untracked payload: {relative}") from exc
            total += len(payload)
            if total > self.max_untracked_bytes:
                raise WorktreeError("untracked outputs exceed byte limit")
            destination = target_path / relative
            if not _within(destination, target_path):
                raise WorktreeError(f"untracked output escaped target: {relative}")
            if destination.exists() and (
                not destination.is_file() or destination.read_bytes() != payload
            ):
                raise WorktreeError(
                    f"untracked output conflicts with target: {relative}"
                )
            decoded[relative] = payload

        actual_paths = tuple(sorted(_patch_paths(target_path, patch) | set(decoded)))
        declared_paths = tuple(_safe_relative(path) for path in change.changed_paths)
        if declared_paths != actual_paths:
            raise WorktreeError("change-set paths do not match its payload")
        if change.digest != _change_digest(
            change.base_sha, patch, change.untracked_b64, declared_paths
        ):
            raise WorktreeError("change-set digest mismatch")
        outside = [
            path
            for path in actual_paths
            if not any(fnmatch.fnmatchcase(path, pattern) for pattern in write_scope)
        ]
        if outside:
            raise WorktreeError(f"change-set escaped write scope: {outside}")

        if patch:
            _git(
                target_path,
                "apply",
                "--check",
                "--binary",
                "--whitespace=nowarn",
                input_bytes=patch,
            )
            _git(
                target_path,
                "apply",
                "--binary",
                "--whitespace=nowarn",
                input_bytes=patch,
            )

        for relative, payload in decoded.items():
            destination = target_path / relative
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", dir=destination.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
                directory = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                temporary.unlink(missing_ok=True)
