"""Immutable, schema-checked JSON artifacts for graph runs."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


class ArtifactError(ValueError):
    """An artifact is invalid, missing, or no longer matches its digest."""


@dataclass(frozen=True)
class Artifact:
    digest: str
    path: Path
    value: Any


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ArtifactError(f"artifact is not canonical JSON: {exc}") from exc


class ArtifactStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, value: Any, schema: dict[str, Any]) -> Artifact:
        errors = sorted(
            Draft202012Validator(schema).iter_errors(value),
            key=lambda error: list(error.path),
        )
        if errors:
            raise ArtifactError(
                f"artifact schema validation failed: {errors[0].message}"
            )
        payload = canonical_json(value)
        digest = hashlib.sha256(payload).hexdigest()
        path = self.root / digest[:2] / f"{digest}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{digest}.", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o444)
            try:
                os.link(temporary, path)
                directory = os.open(
                    path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except FileExistsError:
                if path.read_bytes() != payload:
                    raise ArtifactError(
                        f"digest collision or corrupted artifact: {digest}"
                    )
        finally:
            temporary.unlink(missing_ok=True)
        return Artifact(digest=digest, path=path, value=value)

    def get(self, digest: str, schema: dict[str, Any]) -> Artifact:
        path = self.root / digest[:2] / f"{digest}.json"
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ArtifactError(f"artifact {digest} is missing: {exc}") from exc
        actual = hashlib.sha256(payload).hexdigest()
        if actual != digest:
            raise ArtifactError(
                f"artifact digest mismatch: expected {digest}, got {actual}"
            )
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactError(f"artifact {digest} is invalid JSON: {exc}") from exc
        errors = sorted(
            Draft202012Validator(schema).iter_errors(value),
            key=lambda error: list(error.path),
        )
        if errors:
            raise ArtifactError(
                f"artifact schema validation failed: {errors[0].message}"
            )
        return Artifact(digest=digest, path=path, value=value)
