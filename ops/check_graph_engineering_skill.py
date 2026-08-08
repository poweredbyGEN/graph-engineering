#!/usr/bin/env python3
"""Validate the public graph-engineering skill without external dependencies."""

from __future__ import annotations

import re
import sys
from pathlib import Path


NAME_RE = re.compile(r"^[a-z0-9-]{1,64}$")
LINK_RE = re.compile(r"\[[^]]+\]\(([^)]+)\)")


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md must start with YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("SKILL.md frontmatter is not closed")
    values: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError(f"invalid frontmatter line: {line}")
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return values, text[end + 5 :]


def validate(skill_dir: Path) -> list[str]:
    errors: list[str] = []
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        return [f"missing {skill_file}"]

    text = skill_file.read_text(encoding="utf-8")
    try:
        meta, body = _frontmatter(text)
    except ValueError as exc:
        return [str(exc)]

    if set(meta) != {"name", "description"}:
        errors.append("frontmatter must contain exactly name and description")
    name = meta.get("name", "")
    if not NAME_RE.fullmatch(name):
        errors.append("name must contain only lowercase letters, digits, and hyphens")
    if name != skill_dir.name:
        errors.append(f"skill name {name!r} must match directory {skill_dir.name!r}")
    description = meta.get("description", "")
    if len(description) < 80 or "Use when" not in description:
        errors.append("description must explain the capability and concrete trigger conditions")
    if "TODO" in text:
        errors.append("skill contains an unresolved TODO")
    if len(text.splitlines()) > 500:
        errors.append("SKILL.md exceeds the 500-line progressive-disclosure limit")

    for target in LINK_RE.findall(body):
        if "://" in target or target.startswith("#"):
            continue
        resolved = (skill_dir / target.split("#", 1)[0]).resolve()
        try:
            resolved.relative_to(skill_dir.resolve())
        except ValueError:
            errors.append(f"reference escapes skill directory: {target}")
            continue
        if not resolved.is_file():
            errors.append(f"missing referenced file: {target}")

    openai = skill_dir / "agents" / "openai.yaml"
    if not openai.is_file():
        errors.append("missing agents/openai.yaml")
    else:
        interface = openai.read_text(encoding="utf-8")
        for field in ("display_name:", "short_description:", "default_prompt:"):
            if field not in interface:
                errors.append(f"agents/openai.yaml missing {field[:-1]}")
        if f"${name}" not in interface:
            errors.append(f"default_prompt must explicitly mention ${name}")

    return errors


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]) if args else Path(__file__).resolve().parents[1] / "skills" / "graph-engineering"
    errors = validate(root)
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(f"PASS: {root} is a valid graph-engineering skill")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
