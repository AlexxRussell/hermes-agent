"""Regression for NousResearch/hermes-agent#156511: explicit restoration of orphan skills."""

import json
import pytest

from tools.skill_manager_tool import _create_skill, _find_skill, skill_manage


SKILL_MD = (
    "---\nname: probe-skill\ndescription: A synthetic test skill.\n---\n"
    "# Probe skill\n\nRun the probe.\n"
)


@pytest.fixture
def skills_root(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    root = home / "skills"
    root.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return root


def test_orphan_skill_directory_recovers_through_write_file(skills_root):
    orphan = skills_root / "research" / "probe-skill"
    scripts = orphan / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "run.py"
    script.write_text("print('hi')\n")

    assert _find_skill("probe-skill") is None
    # Preserve the create policy: an occupied directory is never adopted implicitly.
    refused = _create_skill("probe-skill", SKILL_MD, category="research")
    assert refused["success"] is False
    assert "file_path='SKILL.md'" in refused["error"]

    blocked = json.loads(skill_manage(
        action="write_file", name="probe-skill",
        file_path="scripts/extra.py", file_content="print('extra')\n"))
    assert blocked["success"] is False

    restored = json.loads(skill_manage(
        action="write_file", name="probe-skill",
        file_path="SKILL.md", file_content=SKILL_MD))
    assert restored["success"] is True, restored
    assert (orphan / "SKILL.md").read_text() == SKILL_MD
    assert script.read_text() == "print('hi')\n"
    assert _find_skill("probe-skill")["path"] == orphan


def test_ledger_rollback_of_a_restore_keeps_the_files_it_found(skills_root):
    from tools import skill_ledger
    orphan = skills_root / "research" / "probe-skill"
    (orphan / "scripts").mkdir(parents=True)
    (orphan / "scripts" / "run.py").write_text("print('hi')\n")
    restored = json.loads(skill_manage(
        action="write_file", name="probe-skill", file_path="SKILL.md", file_content=SKILL_MD))
    assert restored["success"] is True, restored

    entry = skill_ledger.list_entries(skill="probe-skill")[0]
    ok, msg = skill_ledger.rollback_entry(entry["id"])  # `hermes curator rollback <id>`

    assert ok, msg
    # Undoing the restore removes the SKILL.md it wrote, never the files that were already there.
    assert [p.name for p in orphan.rglob("*") if p.is_file()] == ["run.py"]


def test_orphan_skill_recovery_requires_unambiguous_name(skills_root):
    for category in ("research", "development"):
        (skills_root / category / "probe-skill").mkdir(parents=True)
    denied = json.loads(skill_manage(
        action="write_file", name="probe-skill",
        file_path="SKILL.md", file_content=SKILL_MD))
    assert denied["success"] is False
    assert "Multiple orphan directories" in denied["error"]

    resolved = json.loads(skill_manage(
        action="write_file", name="research/probe-skill",
        file_path="SKILL.md", file_content=SKILL_MD))
    assert resolved["success"] is True, resolved
    assert not (skills_root / "development" / "probe-skill" / "SKILL.md").exists()

    # A category folder holding skills is never an orphan: it must not become a skill itself.
    category = json.loads(skill_manage(
        action="write_file", name="research",
        file_path="SKILL.md", file_content=SKILL_MD.replace("probe-skill", "research")))
    assert category["success"] is False
    assert not (skills_root / "research" / "SKILL.md").exists()


