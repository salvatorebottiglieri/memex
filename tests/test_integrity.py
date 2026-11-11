"""Unit tests for the pure vault-wide locator integrity check."""
from __future__ import annotations

from pathlib import Path

from memex.integrity import (
    Finding,
    check_integrity,
    find_dangling_provenance,
    find_dead_source,
    find_missing_content,
)


def test_healthy_vault_has_no_findings(tmp_path: Path) -> None:
    content = tmp_path / "note.md"
    content.write_text("# Note\n", encoding="utf-8")

    findings = check_integrity(
        locations=[("n1", str(content))],
        provenance=[("n2", "n1")],
        sources=[("n1", False)],
    )

    assert findings == []


def test_missing_content_path_is_reported(tmp_path: Path) -> None:
    missing = tmp_path / "gone.md"

    findings = find_missing_content([("n1", str(missing))])

    assert findings == [
        Finding("n1", "missing_content", f"content_path does not exist: {missing}")
    ]


def test_null_content_path_is_not_a_finding() -> None:
    """URL nodes have no content file; that is a valid shape, not a defect."""
    assert find_missing_content([("url-1", None)]) == []
    assert find_missing_content([("url-1", "")]) == []


def test_directory_content_path_is_not_a_file(tmp_path: Path) -> None:
    directory = tmp_path / "notes"
    directory.mkdir()

    findings = find_missing_content([("n1", str(directory))])

    assert len(findings) == 1
    assert findings[0].code == "content_not_a_file"
    assert "not a regular file" in findings[0].detail


def test_unreadable_content_path_is_classified_not_raised(monkeypatch, tmp_path: Path) -> None:
    original_stat = Path.stat

    def fake_stat(self, *args, **kwargs):
        if str(self) == "/secret/note.md":
            raise PermissionError("permission denied")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)

    findings = find_missing_content([("n1", "/secret/note.md")])

    assert len(findings) == 1
    assert findings[0].code == "content_unreadable"
    assert "permission denied" in findings[0].detail


def test_dangling_provenance_target_is_reported() -> None:
    locations = [("n1", None), ("n2", None)]

    findings = find_dangling_provenance(
        provenance=[("n2", "ghost"), ("n2", "n1")],
        locations=locations,
    )

    assert findings == [
        Finding(
            "n2",
            "dangling_provenance",
            "derived_from target ghost does not exist",
        )
    ]


def test_dead_source_is_reported() -> None:
    findings = find_dead_source([("url-1", True), ("url-2", False)])

    assert findings == [
        Finding("url-1", "dead_source", "source is marked failed")
    ]


def test_check_integrity_combines_all_three_classes(tmp_path: Path) -> None:
    content = tmp_path / "note.md"
    content.write_text("# Note\n", encoding="utf-8")
    missing = tmp_path / "gone.md"

    findings = check_integrity(
        locations=[("n1", str(content)), ("n2", str(missing))],
        provenance=[("n2", "ghost")],
        sources=[("n1", True)],
    )

    assert [f.code for f in findings] == [
        "missing_content",
        "dangling_provenance",
        "dead_source",
    ]
    assert {f.node_id for f in findings} == {"n1", "n2"}
