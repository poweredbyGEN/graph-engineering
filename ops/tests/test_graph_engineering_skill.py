from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_graph_engineering_skill import validate

SOURCE = Path(__file__).resolve().parents[2] / "skills" / "graph-engineering"


def copy_skill(tmp_path: Path) -> Path:
    target = tmp_path / "graph-engineering"
    shutil.copytree(SOURCE, target)
    return target


def test_repository_skill_is_valid():
    # intent: discovery metadata, progressive references, and UI metadata must stay in sync.
    assert validate(SOURCE) == []


def test_unresolved_placeholder_fails(tmp_path):
    # intent: a scaffold that still says TODO is not a usable public skill.
    skill = copy_skill(tmp_path)
    path = skill / "SKILL.md"
    path.write_text(path.read_text() + "\nTODO: finish this\n")
    assert any("TODO" in error for error in validate(skill))


def test_broken_reference_fails(tmp_path):
    # intent: progressive disclosure is only useful when every promised guide exists.
    references = ("patterns.md", "runtime-guide.md", "extending.md")
    for index, reference in enumerate(references):
        skill = tmp_path / f"graph-engineering-{index}"
        shutil.copytree(SOURCE, skill)
        (skill / "references" / reference).unlink()
        assert any(reference in error for error in validate(skill))


def test_default_prompt_must_name_the_skill(tmp_path):
    # intent: explicit invocation must work even when semantic skill discovery does not.
    skill = copy_skill(tmp_path)
    path = skill / "agents" / "openai.yaml"
    path.write_text(path.read_text().replace("$graph-engineering", "graph mode"))
    assert any("explicitly mention" in error for error in validate(skill))
