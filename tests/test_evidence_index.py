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

    def test_generated_markdown_is_current(self):
        self.assertEqual(
            DEFAULT_MARKDOWN.read_text(encoding="utf-8"),
            render_markdown(load_index()),
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


if __name__ == "__main__":
    unittest.main()
