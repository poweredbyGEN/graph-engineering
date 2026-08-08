"""Progressively disclosed public skill resources for the MCP adapter."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_SKILL_BYTES = 128 * 1024


@dataclass(frozen=True)
class SkillRecord:
    name: str
    version: str
    body: str
    provenance: str
    requirements: tuple[str, ...] = ()
    public: bool = True

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ValueError(
                "skill name must be a lowercase slug no longer than 64 characters"
            )
        if not self.version or len(self.version) > 64:
            raise ValueError("skill version must be 1..64 characters")
        if not self.provenance or len(self.provenance) > 512:
            raise ValueError("skill provenance must be 1..512 characters")
        if len(self.body.encode()) > MAX_SKILL_BYTES:
            raise ValueError(f"skill body exceeds {MAX_SKILL_BYTES} bytes")
        if len(self.requirements) > 32 or any(
            not item or len(item) > 128 for item in self.requirements
        ):
            raise ValueError(
                "skill requirements must contain at most 32 bounded strings"
            )

    @property
    def uri(self) -> str:
        return f"skill://{self.name}/{self.version}"

    @property
    def digest(self) -> str:
        document = {
            "body": self.body,
            "name": self.name,
            "provenance": self.provenance,
            "requirements": self.requirements,
            "version": self.version,
        }
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    def descriptor(self) -> dict[str, object]:
        return {
            "uri": self.uri,
            "name": self.name,
            "title": f"{self.name} {self.version}",
            "description": "Public operating procedure; read only when this skill is selected.",
            "mimeType": "text/markdown",
            "_meta": {
                "com.graph-engineering/digest": self.digest,
                "com.graph-engineering/provenance": self.provenance,
                "com.graph-engineering/requirements": list(self.requirements),
                "com.graph-engineering/version": self.version,
                "com.graph-engineering/authority": "none",
            },
        }


def public_skills(records: tuple[SkillRecord, ...]) -> tuple[SkillRecord, ...]:
    """Never publish a private record, even when a caller passes it accidentally."""
    return tuple(
        sorted(
            (record for record in records if record.public), key=lambda item: item.uri
        )
    )
