import pytest

from graph_engineering.mcp.skills import SkillRecord, public_skills


def test_private_skills_are_filtered_and_public_digest_is_stable():
    # intent: a private organization playbook must not leak through a public MCP resource list.
    public = SkillRecord("review", "1", "Do the review.", "https://example.test/review")
    private = SkillRecord("internal", "1", "secret host", "private", public=False)
    visible = public_skills((private, public))
    assert visible == (public,)
    assert (
        public.digest
        == SkillRecord(
            "review", "1", "Do the review.", "https://example.test/review"
        ).digest
    )
    assert public.descriptor()["_meta"]["com.graph-engineering/authority"] == "none"


def test_skill_digest_covers_requirements_provenance_and_body():
    base = SkillRecord("review", "1", "body", "source", ("git",))
    changed = SkillRecord("review", "1", "body", "source", ("git", "pytest"))
    assert base.digest != changed.digest


def test_skill_fields_and_size_are_bounded():
    with pytest.raises(ValueError):
        SkillRecord("Not Valid", "1", "body", "source")
    with pytest.raises(ValueError):
        SkillRecord("valid", "1", "x" * (128 * 1024 + 1), "source")
