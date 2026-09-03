"""Delivery evidence index validation and rendering tests."""

import copy
import unittest

from tools.evidence_index import (
    DEFAULT_MARKDOWN,
    ROOT,
    load_index,
    render_markdown,
    validate_index,
)


class TestEvidenceIndex(unittest.TestCase):
    def test_repository_index_is_valid(self):
        self.assertEqual(validate_index(load_index(), ROOT), [])

    def test_taskbook_matrix_is_registered_as_working_g0_evidence(self):
        entry = next(
            item for item in load_index()["entries"]
            if item["id"] == "EV-G0-TASKBOOK-MAP-001"
        )
        self.assertEqual(entry["gate"], "G0")
        self.assertEqual(entry["status"], "working")
        self.assertEqual(entry["owner"], "xyh202131")
        self.assertEqual(entry["related_issues"], [65])
        self.assertIn("外部", entry["limitations"])

    def test_taskbook_matrix_keeps_status_vocabulary_and_role_boundary(self):
        matrix = (ROOT / "docs" / "官方任务书落地矩阵.md").read_text(encoding="utf-8")
        self.assertIn("官方要求（原文口径）", matrix)
        self.assertIn("内部强化/解释（非官方要求）", matrix)
        self.assertIn("来源复核者为 `xyh202131`", matrix)
        self.assertIn("独立领域评分者和仲裁者仍为", matrix)
        statuses = {line.rsplit("|", 2)[1].strip() for line in matrix.splitlines() if line.startswith("| O-")}
        self.assertTrue(statuses <= {"met", "partial", "missing", "external"})
        self.assertTrue(statuses)

    def test_generated_markdown_is_current(self):
        self.assertEqual(
            DEFAULT_MARKDOWN.read_text(encoding="utf-8"),
            render_markdown(load_index()),
        )

    def test_rendered_markdown_contains_every_entry_and_gate(self):
        data = load_index()
        rendered = render_markdown(data)
        evidence_rows = [
            line for line in rendered.splitlines() if line.startswith("| EV-")
        ]

        self.assertEqual(len(data["entries"]), len(evidence_rows))
        for entry in data["entries"]:
            self.assertTrue(
                any(
                    row.startswith(f"| {entry['id']} | {entry['gate']} |")
                    for row in evidence_rows
                ),
                entry["id"],
            )

    def test_duplicate_paths_are_rejected(self):
        data = copy.deepcopy(load_index())
        data["entries"][1]["path"] = data["entries"][0]["path"]
        errors = validate_index(data, ROOT)
        self.assertTrue(any("duplicate evidence path" in error for error in errors))

    def test_dangling_evidence_sources_are_rejected(self):
        data = copy.deepcopy(load_index())
        data["entries"][1]["source"] = "EV-G0-MISSING-999"
        errors = validate_index(data, ROOT)
        self.assertTrue(any("references unknown evidence" in error for error in errors))

    def test_project_name_is_required(self):
        data = copy.deepcopy(load_index())
        data["project"] = ""
        errors = validate_index(data, ROOT)
        self.assertIn("project must be a non-empty string", errors)

    def test_absolute_and_parent_paths_are_rejected(self):
        unsafe_paths = (
            "C:/Users/name/evidence.log",
            "../evidence.log",
            "/tmp/evidence.log",
            "delivery//evidence.log",
            "https://example.com/evidence.log",
        )
        for unsafe_path in unsafe_paths:
            with self.subTest(path=unsafe_path):
                data = copy.deepcopy(load_index())
                data["entries"][-1]["path"] = unsafe_path
                errors = validate_index(data, ROOT)
                self.assertTrue(any("safe repository-relative" in error for error in errors))

    def test_ambiguous_version_names_are_rejected(self):
        for ambiguous_path in (
            "delivery/evidence/final-v2.zip",
            "delivery/latest/evidence.zip",
        ):
            with self.subTest(path=ambiguous_path):
                data = copy.deepcopy(load_index())
                data["entries"][-1]["path"] = ambiguous_path
                errors = validate_index(data, ROOT)
                self.assertTrue(any("ambiguous version name" in error for error in errors))

    def test_invalid_scalar_types_are_reported_instead_of_crashing(self):
        invalid_values = {
            "gate": [],
            "category": {},
            "status": [],
            "visibility": {},
        }
        for field, value in invalid_values.items():
            with self.subTest(field=field):
                data = copy.deepcopy(load_index())
                data["entries"][0][field] = value
                errors = validate_index(data, ROOT)
                self.assertTrue(any(f".{field}" in error for error in errors))

    def test_boolean_issue_numbers_are_rejected(self):
        data = copy.deepcopy(load_index())
        data["entries"][0]["related_issues"] = [True]
        errors = validate_index(data, ROOT)
        self.assertTrue(any("positive issue numbers" in error for error in errors))

    def test_candidate_and_approved_evidence_require_a_commit(self):
        for status in ("candidate", "approved"):
            with self.subTest(status=status):
                data = copy.deepcopy(load_index())
                data["entries"][-1]["status"] = status
                data["entries"][-1]["repo_commit"] = None
                errors = validate_index(data, ROOT)
                self.assertTrue(any("repo_commit is required" in error for error in errors))

    def test_render_rejects_invalid_entries_with_value_error(self):
        invalid_values = {"category": "unknown", "status": "unknown"}
        for field, value in invalid_values.items():
            with self.subTest(field=field):
                data = copy.deepcopy(load_index())
                data["entries"][0][field] = value
                with self.assertRaisesRegex(ValueError, rf"\.{field}"):
                    render_markdown(data)

    def test_missing_fields_do_not_hide_duplicate_ids_or_paths(self):
        data = copy.deepcopy(load_index())
        data["entries"][1]["id"] = data["entries"][0]["id"]
        data["entries"][1]["path"] = data["entries"][0]["path"]
        del data["entries"][1]["owner"]

        errors = validate_index(data, ROOT)

        self.assertTrue(any("missing fields: owner" in error for error in errors))
        self.assertTrue(any("duplicate evidence ID" in error for error in errors))
        self.assertTrue(any("duplicate evidence path" in error for error in errors))

    def test_invalid_status_does_not_trigger_path_existence_check(self):
        data = copy.deepcopy(load_index())
        data["entries"][-1]["status"] = []
        data["entries"][-1]["path"] = "delivery/evidence/not-created.md"

        errors = validate_index(data, ROOT)

        self.assertTrue(any(".status is not recognized" in error for error in errors))
        self.assertFalse(any(".path does not exist" in error for error in errors))

    def test_markdown_cells_escape_formatting_and_html(self):
        data = copy.deepcopy(load_index())
        entry = data["entries"][-1]
        entry["title"] = "<script>*bold* [link] | `code`"
        entry["path"] = "delivery/evidence/report`draft`.md"

        rendered = render_markdown(data)

        self.assertIn(
            "&lt;script&gt;\\*bold\\* \\[link\\] \\| &#96;code&#96;",
            rendered,
        )
        self.assertIn("report&#96;draft&#96;.md", rendered)


if __name__ == "__main__":
    unittest.main()
