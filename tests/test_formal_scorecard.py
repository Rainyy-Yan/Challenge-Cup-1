"""Offline contract tests for the formal evidence scorecard."""

import math
import unittest
from unittest.mock import patch

from evalkit.formal_scorecard import (
    build_scorecard,
    cluster_bootstrap_interval,
    validate_truth,
    wilson_interval,
)


def valid_truth() -> dict:
    """Return a fully adjudicated, assessable version-1 truth fixture."""
    cases = [
        {"id": f"case-{index:03d}", "profile_id": f"profile-{index % 3 + 1}"}
        for index in range(50)
    ]
    claims = []
    adaptations = []
    for index in range(50):
        hallucination = "yes" if index % 3 == 0 else "no"
        adaptation = "correct" if index % 2 == 0 else "incorrect"
        reviewer_claims = {
            "reviewer-one": hallucination,
            "reviewer-two": hallucination,
        }
        reviewer_adaptations = {
            "reviewer-one": adaptation,
            "reviewer-two": adaptation,
        }
        claims.append(
            {
                "id": f"claim-{index:03d}",
                "case_id": f"case-{index:03d}",
                "labels": reviewer_claims,
                "final_label": hallucination,
            }
        )
        adaptations.append(
            {
                "id": f"adaptation-{index:03d}",
                "case_id": f"case-{index:03d}",
                "labels": reviewer_adaptations,
                "final_label": adaptation,
            }
        )
    return {
        "version": 1,
        "status": "frozen",
        "frozen_at": "2026-09-03T00:00:00Z",
        "seed": 731,
        "blind_to_system_output": True,
        "independent_ratings": True,
        "dataset": {
            "profile_ids": ["profile-1", "profile-2", "profile-3"],
            "cases": cases,
        },
        "provenance": {"repository_sha": "a" * 40},
        "reviewers": ["reviewer-one", "reviewer-two"],
        "review_protocol": {
            "human_reviewer_roster": [
                {"id": "reviewer-one", "attested_human": True},
                {"id": "reviewer-two", "attested_human": True},
                {"id": "adjudicator", "attested_human": True},
            ],
        },
        "claims": claims,
        "adaptations": adaptations,
        "coverage_universe": [
            {"kp_id": "kp-001", "weight": 2.0, "covered": False, "evidence_ids": []},
            {"kp_id": "kp-002", "weight": 3.0, "covered": True, "evidence_ids": ["e-002"]},
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

    def test_scorecard_uses_frozen_seed_and_ten_thousand_bootstrap_samples(self):
        truth = valid_truth()
        with patch("evalkit.formal_scorecard.cluster_bootstrap_interval", return_value=(0.2, 0.8)) as bootstrap:
            build_scorecard(truth)
        self.assertEqual(bootstrap.call_count, 2)
        for _, kwargs in bootstrap.call_args_list:
            self.assertEqual(kwargs["seed"], 731)
            self.assertEqual(kwargs["samples"], 10000)


class TestTruthValidation(unittest.TestCase):
    def test_rejects_fewer_than_fifty_cases(self):
        truth = valid_truth()
        truth["dataset"]["cases"] = truth["dataset"]["cases"][:49]
        truth["claims"] = truth["claims"][:49]
        truth["adaptations"] = truth["adaptations"][:49]
        self.assertEqual(validate_truth(truth), ["at least 50 cases are required"])

    def test_rejects_fewer_than_three_profiles(self):
        truth = valid_truth()
        truth["dataset"]["profile_ids"] = ["profile-1", "profile-2"]
        self.assertEqual(
            validate_truth(truth),
            ["dataset.profile_ids must contain at least 3 distinct non-empty values"],
        )

    def test_rejects_malformed_repository_sha(self):
        truth = valid_truth()
        truth["provenance"]["repository_sha"] = "not-a-sha"
        self.assertEqual(
            validate_truth(truth),
            ["provenance.repository_sha must be a 40-character lowercase hexadecimal SHA"],
        )

    def test_rejects_duplicate_case_ids(self):
        truth = valid_truth()
        truth["dataset"]["cases"][1]["id"] = truth["dataset"]["cases"][0]["id"]
        for records in (truth["claims"], truth["adaptations"]):
            records[1]["case_id"] = "case-000"
        self.assertEqual(validate_truth(truth), ["duplicate case id: case-000"])

    def test_rejects_reviewer_identity_collisions(self):
        truth = valid_truth()
        truth["reviewers"] = ["Same-Person", " same-person "]
        self.assertEqual(
            validate_truth(truth),
            ["reviewers must contain exactly two distinct non-empty identities"],
        )

    def test_rejects_reviewer_missing_from_human_roster(self):
        truth = valid_truth()
        truth["review_protocol"]["human_reviewer_roster"][0]["id"] = "someone-else"
        self.assertEqual(
            validate_truth(truth),
            ["reviewer reviewer-one is not declared in human_reviewer_roster"],
        )

    def test_declared_human_ids_may_contain_machine_like_substrings(self):
        for identity in ("Gail", "mail-reviewer", "ecosystem-expert", "reagent-specialist"):
            with self.subTest(identity=identity):
                truth = valid_truth()
                truth["reviewers"] = [identity, "reviewer-two"]
                truth["review_protocol"]["human_reviewer_roster"][0]["id"] = identity
                for records in (truth["claims"], truth["adaptations"]):
                    for record in records:
                        record["labels"][identity] = record["labels"].pop("reviewer-one")
                self.assertEqual(validate_truth(truth), [])

    def test_rejects_false_human_attestation(self):
        truth = valid_truth()
        truth["review_protocol"]["human_reviewer_roster"][0]["attested_human"] = False
        self.assertEqual(
            validate_truth(truth),
            ["human_reviewer_roster entry reviewer-one must set attested_human to true"],
        )

    def test_rejects_normalized_duplicate_human_roster_ids(self):
        truth = valid_truth()
        truth["review_protocol"]["human_reviewer_roster"].append(
            {"id": " Reviewer-One ", "attested_human": True}
        )
        self.assertEqual(
            validate_truth(truth),
            ["duplicate human_reviewer_roster id: Reviewer-One"],
        )

    def test_rejects_adjudicator_missing_from_human_roster(self):
        truth = valid_truth()
        truth["claims"][0]["labels"]["reviewer-two"] = "no"
        truth["claims"][0]["adjudicated_by"] = "unregistered-adjudicator"
        self.assertEqual(
            validate_truth(truth),
            [
                "claim claim-000 adjudicator unregistered-adjudicator is not declared "
                "in human_reviewer_roster"
            ],
        )

    def test_rejects_exposed_system_conclusion(self):
        truth = valid_truth()
        truth["claims"][0]["system_conclusion"] = "correct"
        self.assertEqual(
            validate_truth(truth),
            ["claim claim-000 exposes a prohibited system conclusion"],
        )

    def test_rejects_machine_label_field(self):
        truth = valid_truth()
        truth["claims"][0]["machine_label"] = "no"
        self.assertEqual(
            validate_truth(truth),
            ["claim claim-000 exposes a prohibited system conclusion"],
        )

    def test_rejects_incomplete_reviewer_labels(self):
        truth = valid_truth()
        del truth["claims"][0]["labels"]["reviewer-one"]
        self.assertEqual(
            validate_truth(truth),
            ["claim claim-000 has incomplete reviewer labels"],
        )

    def test_rejects_unresolved_reviewer_disagreement(self):
        truth = valid_truth()
        truth["claims"][0]["labels"]["reviewer-two"] = "no"
        self.assertEqual(
            validate_truth(truth),
            ["claim claim-000 has unresolved reviewer disagreement"],
        )

    def test_rejects_adjudicator_who_is_a_reviewer(self):
        truth = valid_truth()
        truth["claims"][0]["labels"]["reviewer-two"] = "no"
        truth["claims"][0]["adjudicated_by"] = " Reviewer-One "
        self.assertEqual(
            validate_truth(truth),
            ["claim claim-000 adjudicator must be distinct from both reviewers"],
        )

    def test_rejects_explicit_empty_adjudicator_identity(self):
        truth = valid_truth()
        truth["claims"][0]["adjudicated_by"] = "   "
        self.assertEqual(
            validate_truth(truth),
            ["claim claim-000 adjudicator must be a non-empty identity"],
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

    def test_rejects_whitespace_only_coverage_evidence(self):
        truth = valid_truth()
        truth["coverage_universe"][1]["evidence_ids"] = ["   "]
        self.assertEqual(
            validate_truth(truth),
            ["covered coverage point kp-002 needs evidence"],
        )

    def test_rejects_normalized_duplicate_coverage_ids(self):
        truth = valid_truth()
        truth["coverage_universe"][0]["kp_id"] = " KP-001 "
        truth["coverage_universe"][1]["kp_id"] = "kp-001"
        self.assertEqual(
            validate_truth(truth),
            ["duplicate coverage kp_id: kp-001"],
        )

    def test_rejects_low_assessable_share(self):
        truth = valid_truth()
        for record in truth["claims"][:12]:
            record["final_label"] = "unassessable"
            record["labels"] = {
                "reviewer-one": "unassessable",
                "reviewer-two": "unassessable",
            }
        self.assertIn("claims assessable share is below 0.95", validate_truth(truth))

    def test_rejects_low_or_undefined_kappa(self):
        truth = valid_truth()
        for records, inverse in (
            (truth["claims"], {"yes": "no", "no": "yes"}),
            (truth["adaptations"], {"correct": "incorrect", "incorrect": "correct"}),
        ):
            for record in records:
                record["labels"]["reviewer-two"] = inverse[record["labels"]["reviewer-one"]]
                record["adjudicated_by"] = "adjudicator"
        errors = validate_truth(truth)
        self.assertIn("hallucination kappa is below 0.60 or undefined", errors)
        self.assertIn("adaptation kappa is below 0.60 or undefined", errors)

    def test_scorecard_kappa_uses_reviewer_columns_not_adjudicated_labels(self):
        truth = valid_truth()
        claim = truth["claims"][0]
        claim["labels"]["reviewer-two"] = "no"
        claim["adjudicated_by"] = "adjudicator"
        first_scorecard = build_scorecard(truth)
        claim["final_label"] = "no"
        second_scorecard = build_scorecard(truth)
        self.assertEqual(
            first_scorecard["kappa"]["hallucination"]["value"],
            second_scorecard["kappa"]["hallucination"]["value"],
        )

    def test_final_unassessable_is_excluded_from_metric_denominator(self):
        truth = valid_truth()
        truth["claims"][0]["final_label"] = "unassessable"
        truth["claims"][0]["labels"] = {
            "reviewer-one": "unassessable",
            "reviewer-two": "unassessable",
        }
        scorecard = build_scorecard(truth)
        self.assertEqual(scorecard["hallucination_rate"]["denominator"], 49)
        self.assertEqual(scorecard["assessable_share"]["claims"], 0.98)
        self.assertEqual(scorecard["assessable_share"]["adaptations"], 1.0)

    def test_multiple_claims_for_one_case_are_kept_as_one_bootstrap_cluster(self):
        truth = valid_truth()
        truth["claims"].append(
            {
                "id": "claim-extra",
                "case_id": "case-000",
                "labels": {"reviewer-one": "yes", "reviewer-two": "yes"},
                "final_label": "yes",
            }
        )
        scorecard = build_scorecard(truth)
        self.assertEqual(scorecard["hallucination_rate"]["denominator"], 51)

    def test_rejects_common_reviewer_label_changed_by_final_label(self):
        truth = valid_truth()
        truth["claims"][0]["final_label"] = "no"
        self.assertEqual(
            validate_truth(truth),
            ["claim claim-000 final_label must equal the common reviewer label"],
        )

    def test_rejects_missing_freeze_controls(self):
        truth = valid_truth()
        del truth["frozen_at"]
        del truth["seed"]
        truth["blind_to_system_output"] = False
        truth["independent_ratings"] = False
        self.assertEqual(
            validate_truth(truth),
            [
                "frozen_at must be an ISO-8601 timestamp",
                "seed must be a non-negative integer",
                "blind_to_system_output must be true",
                "independent_ratings must be true",
            ],
        )

    def test_rejects_coverage_missing_boolean_duplicate_and_non_finite_fields(self):
        truth = valid_truth()
        coverage = truth["coverage_universe"]
        coverage[0]["kp_id"] = "  "
        coverage[0]["covered"] = 1
        coverage[0]["weight"] = math.nan
        coverage[1]["kp_id"] = "kp-002"
        coverage.append(
            {"kp_id": "kp-002", "weight": math.inf, "covered": False, "evidence_ids": []}
        )
        errors = validate_truth(truth)
        self.assertIn("coverage point 0 needs a non-empty kp_id", errors)
        self.assertIn("coverage point 0 has an invalid weight", errors)
        self.assertIn("coverage point 0 covered must be a boolean", errors)
        self.assertIn("duplicate coverage kp_id: kp-002", errors)
        self.assertIn("coverage point kp-002 has an invalid weight", errors)

    def test_rejects_claims_that_do_not_cover_fifty_distinct_cases(self):
        for key in ("claims", "adaptations"):
            with self.subTest(key=key):
                truth = valid_truth()
                for record in truth[key]:
                    record["case_id"] = "case-000"
                self.assertEqual(
                    validate_truth(truth),
                    [f"{key} must cover at least 50 distinct case_ids"],
                )

    def test_rejects_unassessable_claim_rate_without_dilution_by_adaptations(self):
        truth = valid_truth()
        for record in truth["claims"][:10]:
            record["final_label"] = "unassessable"
            record["labels"] = {
                "reviewer-one": "unassessable",
                "reviewer-two": "unassessable",
            }
        self.assertEqual(
            validate_truth(truth),
            ["claims assessable share is below 0.95"],
        )

    def test_malformed_reviewer_final_and_adjudicator_values_return_errors(self):
        cases = (
            ("labels", ["not-a-label"], "claim claim-000 has incomplete reviewer labels"),
            ("final_label", {}, "claim claim-000 has an invalid final_label"),
            ("adjudicated_by", [], "claim claim-000 adjudicator must be a non-empty identity"),
            ("adjudicated_by", None, "claim claim-000 adjudicator must be a non-empty identity"),
        )
        for field, value, expected in cases:
            with self.subTest(field=field):
                truth = valid_truth()
                record = truth["claims"][0]
                if field == "labels":
                    record["labels"]["reviewer-one"] = value
                elif field == "adjudicated_by":
                    record["labels"]["reviewer-two"] = "no"
                    record[field] = value
                else:
                    record[field] = value
                self.assertEqual(validate_truth(truth), [expected])

    def test_non_string_reviewer_identity_returns_a_stable_error(self):
        for identity in (["reviewer-one"], {}, None):
            with self.subTest(identity=identity):
                truth = valid_truth()
                truth["reviewers"] = [identity, "reviewer-two"]
                self.assertEqual(
                    validate_truth(truth),
                    ["reviewers must contain exactly two distinct non-empty identities"],
                )

    def test_rejects_whitespace_duplicate_dataset_case_ids(self):
        truth = valid_truth()
        truth["dataset"]["cases"][1]["id"] = " case-000 "
        truth["claims"][1]["case_id"] = " case-000 "
        truth["adaptations"][1]["case_id"] = " case-000 "
        errors = validate_truth(truth)
        self.assertIn("case case-000 must equal its stripped form", errors)
        self.assertIn("duplicate case id: case-000", errors)
        self.assertIn("claim claim-001 case_id must equal its stripped form", errors)
        self.assertIn("adaptation adaptation-001 case_id must equal its stripped form", errors)

    def test_rejects_case_profile_not_declared_by_dataset(self):
        truth = valid_truth()
        truth["dataset"]["cases"][0]["profile_id"] = "unknown-profile"
        self.assertEqual(
            validate_truth(truth),
            ["case case-000 references an undeclared profile_id"],
        )

    def test_rejects_case_coverage_that_represents_one_profile(self):
        truth = valid_truth()
        for case in truth["dataset"]["cases"]:
            case["profile_id"] = "profile-1"
        self.assertEqual(
            validate_truth(truth),
            [
                "claims must represent at least 3 distinct profile_ids",
                "adaptations must represent at least 3 distinct profile_ids",
            ],
        )

    def test_rejects_casefolded_record_reference_that_inflates_case_coverage(self):
        truth = valid_truth()
        truth["claims"][1]["case_id"] = "CASE-000"
        self.assertEqual(
            validate_truth(truth),
            ["claims must cover at least 50 distinct case_ids"],
        )

    def test_rejects_normalized_duplicate_dataset_profile_ids(self):
        truth = valid_truth()
        truth["dataset"]["profile_ids"] = [" Profile-1 ", "profile-1", "profile-2", "profile-3"]
        self.assertEqual(
            validate_truth(truth),
            ["duplicate dataset profile_id: profile-1"],
        )
