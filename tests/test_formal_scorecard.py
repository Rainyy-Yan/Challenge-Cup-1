"""Offline contract tests for the formal evidence scorecard."""

import unittest

from evalkit.formal_scorecard import (
    build_scorecard,
    cluster_bootstrap_interval,
    validate_truth,
    wilson_interval,
)


def valid_truth() -> dict:
    """Return a fully adjudicated, assessable version-1 truth fixture."""
    return {
        "version": 1,
        "status": "frozen",
        "provenance": {"repository_sha": "a" * 40},
        "reviewers": ["reviewer-one", "reviewer-two"],
        "cases": [
            {
                "id": f"case-{index:03d}",
                "profile_id": f"profile-{index % 3 + 1}",
                "labels": {
                    "reviewer-one": {
                        "hallucination": "yes" if index % 3 == 0 else "no",
                        "adaptation": "correct" if index % 2 == 0 else "incorrect",
                    },
                    "reviewer-two": {
                        "hallucination": "yes" if index % 3 == 0 else "no",
                        "adaptation": "correct" if index % 2 == 0 else "incorrect",
                    },
                },
                "adjudicated_labels": {
                    "hallucination": "yes" if index % 3 == 0 else "no",
                    "adaptation": "correct" if index % 2 == 0 else "incorrect",
                },
            }
            for index in range(50)
        ],
        "coverage_universe": [
            {"id": "kp-001", "weight": 2.0, "covered": False, "evidence_ids": []},
            {"id": "kp-002", "weight": 3.0, "covered": True, "evidence_ids": ["e-002"]},
        ],
    }


class TestIntervals(unittest.TestCase):
    def test_zero_denominators_return_closed_zero_intervals(self):
        self.assertEqual(wilson_interval(0, 0), (0.0, 0.0))
        self.assertEqual(
            cluster_bootstrap_interval([], {"correct"}, seed=7, samples=4),
            (0.0, 0.0),
        )

    def test_wilson_zero_success_upper_bound_is_hand_derived(self):
        self.assertAlmostEqual(wilson_interval(0, 100)[1], 0.036993, places=6)

    def test_wilson_rejects_impossible_success_count_before_zero_denominator(self):
        with self.assertRaises(ValueError):
            wilson_interval(1, 0)

    def test_case_cluster_bootstrap_is_seed_deterministic(self):
        records = [
            {"case_id": "case-a", "label": "correct"},
            {"case_id": "case-a", "label": "correct"},
            {"case_id": "case-b", "label": "incorrect"},
        ]
        lower, upper = cluster_bootstrap_interval(records, {"correct"}, seed=7, samples=4)
        self.assertAlmostEqual(lower, 2 / 3, places=12)
        self.assertAlmostEqual(upper, 0.975, places=12)

    def test_scorecard_uses_exact_weighted_coverage_and_conservative_envelope(self):
        scorecard = build_scorecard(valid_truth())
        self.assertEqual(scorecard["coverage"]["point_estimate"], 0.6)
        adaptation = scorecard["adaptation_accuracy"]
        self.assertLessEqual(adaptation["interval"][0], adaptation["wilson_interval"][0])
        self.assertGreaterEqual(adaptation["interval"][1], adaptation["wilson_interval"][1])
        self.assertLessEqual(adaptation["interval"][0], adaptation["bootstrap_interval"][0])
        self.assertGreaterEqual(adaptation["interval"][1], adaptation["bootstrap_interval"][1])


class TestTruthValidation(unittest.TestCase):
    def test_rejects_fewer_than_fifty_cases(self):
        truth = valid_truth()
        truth["cases"] = truth["cases"][:49]
        self.assertEqual(validate_truth(truth), ["at least 50 cases are required"])

    def test_rejects_fewer_than_three_profiles(self):
        truth = valid_truth()
        for case in truth["cases"]:
            case["profile_id"] = "only-profile"
        self.assertEqual(validate_truth(truth), ["at least 3 profiles are required"])

    def test_rejects_malformed_repository_sha(self):
        truth = valid_truth()
        truth["provenance"]["repository_sha"] = "not-a-sha"
        self.assertEqual(
            validate_truth(truth),
            ["provenance.repository_sha must be a 40-character lowercase hexadecimal SHA"],
        )

    def test_rejects_duplicate_case_ids(self):
        truth = valid_truth()
        truth["cases"][1]["id"] = truth["cases"][0]["id"]
        self.assertEqual(validate_truth(truth), ["duplicate case id: case-000"])

    def test_rejects_reviewer_identity_collisions(self):
        truth = valid_truth()
        truth["reviewers"] = ["same-person", "same-person"]
        self.assertEqual(
            validate_truth(truth),
            ["reviewers must contain exactly two distinct non-empty identities"],
        )

    def test_rejects_machine_owned_reviewer_identity(self):
        truth = valid_truth()
        truth["reviewers"] = ["machine", "reviewer-two"]
        self.assertEqual(
            validate_truth(truth),
            ["reviewer identities must not be machine-owned"],
        )

    def test_rejects_exposed_system_conclusion(self):
        truth = valid_truth()
        truth["cases"][0]["system_conclusion"] = "correct"
        self.assertEqual(
            validate_truth(truth),
            ["case case-000 exposes a prohibited system conclusion"],
        )

    def test_rejects_machine_label_field(self):
        truth = valid_truth()
        truth["cases"][0]["machine_label"] = "no"
        self.assertEqual(
            validate_truth(truth),
            ["case case-000 exposes a prohibited system conclusion"],
        )

    def test_rejects_incomplete_reviewer_labels(self):
        truth = valid_truth()
        del truth["cases"][0]["labels"]["reviewer-one"]["adaptation"]
        self.assertEqual(
            validate_truth(truth),
            ["case case-000 has incomplete reviewer labels"],
        )

    def test_rejects_unresolved_reviewer_disagreement(self):
        truth = valid_truth()
        truth["cases"][0]["labels"]["reviewer-two"]["hallucination"] = "no"
        self.assertEqual(
            validate_truth(truth),
            ["case case-000 has unresolved reviewer disagreement"],
        )

    def test_rejects_adjudicator_who_is_a_reviewer(self):
        truth = valid_truth()
        truth["cases"][0]["labels"]["reviewer-two"]["hallucination"] = "no"
        truth["cases"][0]["adjudicator_id"] = "reviewer-one"
        self.assertEqual(
            validate_truth(truth),
            ["case case-000 adjudicator must be distinct from both reviewers"],
        )

    def test_rejects_invalid_coverage_weight(self):
        truth = valid_truth()
        truth["coverage_universe"][0]["weight"] = 0
        self.assertEqual(
            validate_truth(truth),
            ["coverage point kp-001 has an invalid weight"],
        )

    def test_rejects_covered_point_without_evidence(self):
        truth = valid_truth()
        truth["coverage_universe"][1]["evidence_ids"] = []
        self.assertEqual(
            validate_truth(truth),
            ["covered coverage point kp-002 needs evidence"],
        )

    def test_rejects_low_assessable_share(self):
        truth = valid_truth()
        for case in truth["cases"][:6]:
            case["adjudicated_labels"]["adaptation"] = "unassessable"
        self.assertIn("assessable share is below 0.90", validate_truth(truth))

    def test_rejects_low_or_undefined_kappa(self):
        truth = valid_truth()
        for case in truth["cases"]:
            labels = case["labels"]
            labels["reviewer-two"]["hallucination"] = (
                "no" if labels["reviewer-one"]["hallucination"] == "yes" else "yes"
            )
            labels["reviewer-two"]["adaptation"] = (
                "incorrect"
                if labels["reviewer-one"]["adaptation"] == "correct"
                else "correct"
            )
            case["adjudicator_id"] = "adjudicator"
        errors = validate_truth(truth)
        self.assertIn("hallucination kappa is below 0.60 or undefined", errors)
        self.assertIn("adaptation kappa is below 0.60 or undefined", errors)

    def test_scorecard_kappa_uses_reviewer_columns_not_adjudicated_labels(self):
        truth = valid_truth()
        for case in truth["cases"]:
            case["adjudicated_labels"] = {"hallucination": "no", "adaptation": "incorrect"}
        scorecard = build_scorecard(truth)
        self.assertEqual(scorecard["kappa"]["hallucination"]["value"], 1.0)
        self.assertEqual(scorecard["kappa"]["adaptation"]["value"], 1.0)
