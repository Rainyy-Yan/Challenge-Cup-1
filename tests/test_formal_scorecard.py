"""Offline contract tests for the formal evidence scorecard."""

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from evalkit.formal_scorecard import (
    build_scorecard,
    cluster_bootstrap_interval,
    render_scorecard_markdown,
    validate_truth,
    wilson_interval,
)


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _artifact(
    artifact_id: str,
    kind: str,
    *,
    subject_id: str,
    case_id: str | None = None,
    citation_ids: list[str] | None = None,
) -> dict:
    content = f"frozen content for {artifact_id}"
    artifact = {
        "id": artifact_id,
        "kind": kind,
        "subject_id": subject_id,
        "content": content,
        "sha256": _sha256_text(content),
        "citation_ids": citation_ids or [],
        "review_status": "approved" if kind == "coverage_evidence" else "frozen",
    }
    if case_id is not None:
        artifact["case_id"] = case_id
    return artifact


def _refresh_manifest_hash(truth: dict) -> None:
    canonical = json.dumps(
        truth["artifact_manifest"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    truth["provenance"]["artifact_manifest_sha256"] = _sha256_text(canonical)


def _interval_text(interval: object) -> str:
    if interval is None:
        return "not available"
    lower, upper = interval
    return f"[{lower:.6f}, {upper:.6f}]"


def valid_truth() -> dict:
    """Return a fully adjudicated, assessable version-1 truth fixture."""
    citation = {
        "id": "citation-approved",
        "source_id": "source-register-001",
        "locator": "frozen://source-register-001#section-1",
        "excerpt": "reviewed source excerpt",
        "sha256": _sha256_text("reviewed source excerpt"),
        "review_status": "approved",
    }
    artifacts = [
        _artifact(
            f"profile-artifact-{index}",
            "profile_snapshot",
            subject_id=f"profile-{index}",
        )
        for index in range(1, 4)
    ]
    cases = []
    claims = []
    adaptations = []
    for index in range(50):
        case_id = f"case-{index:03d}"
        case_artifact_id = f"case-artifact-{index:03d}"
        claim_artifact_id = f"claim-artifact-{index:03d}"
        resource_artifact_id = f"resource-artifact-{index:03d}"
        cases.append(
            {
                "id": case_id,
                "profile_id": f"profile-{index % 3 + 1}",
                "artifact_id": case_artifact_id,
            }
        )
        artifacts.extend(
            [
                _artifact(case_artifact_id, "case_input", subject_id=case_id),
                _artifact(
                    claim_artifact_id,
                    "claim_output",
                    subject_id=f"claim-{index:03d}",
                    case_id=case_id,
                    citation_ids=["citation-approved"],
                ),
                _artifact(
                    resource_artifact_id,
                    "resource_output",
                    subject_id=f"adaptation-{index:03d}",
                    case_id=case_id,
                    citation_ids=["citation-approved"],
                ),
            ]
        )
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
                "case_id": case_id,
                "artifact_id": claim_artifact_id,
                "labels": reviewer_claims,
                "final_label": hallucination,
            }
        )
        adaptations.append(
            {
                "id": f"adaptation-{index:03d}",
                "case_id": case_id,
                "artifact_id": resource_artifact_id,
                "labels": reviewer_adaptations,
                "final_label": adaptation,
            }
        )
    artifacts.extend(
        [
            _artifact(
                "e-002",
                "coverage_evidence",
                subject_id="kp-002",
                citation_ids=["citation-approved"],
            ),
        ]
    )
    truth = {
        "version": 1,
        "status": "frozen",
        "frozen_at": "2026-09-03T00:00:00Z",
        "seed": 731,
        "blind_to_system_output": True,
        "independent_ratings": True,
        "dataset": {
            "dataset_id": "frozen-eval-v1",
            "profile_ids": ["profile-1", "profile-2", "profile-3"],
            "profile_artifact_ids": {
                "profile-1": "profile-artifact-1",
                "profile-2": "profile-artifact-2",
                "profile-3": "profile-artifact-3",
            },
            "cases": cases,
        },
        "provenance": {
            "repository_sha": "a" * 40,
            "artifact_manifest_sha256": "",
        },
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
        "artifact_manifest": {
            "version": 1,
            "artifacts": artifacts,
            "citations": [citation],
        },
    }
    _refresh_manifest_hash(truth)
    return truth


def passing_truth() -> dict:
    """Return a hand-built fixture whose conservative boundaries meet all gates."""
    truth = valid_truth()
    truth["claims"] = []
    truth["adaptations"] = []
    truth["artifact_manifest"]["artifacts"] = [
        artifact
        for artifact in truth["artifact_manifest"]["artifacts"]
        if artifact["kind"] not in {"claim_output", "resource_output"}
    ]
    for index in range(200):
        case_id = f"case-{index % 50:03d}"
        label = "yes" if index == 0 else "no"
        truth["claims"].append(
            {
                "id": f"pass-claim-{index:03d}",
                "case_id": case_id,
                "artifact_id": f"pass-claim-artifact-{index:03d}",
                "labels": {"reviewer-one": label, "reviewer-two": label},
                "final_label": label,
            }
        )
        truth["artifact_manifest"]["artifacts"].append(
            _artifact(
                f"pass-claim-artifact-{index:03d}",
                "claim_output",
                subject_id=f"pass-claim-{index:03d}",
                case_id=case_id,
                citation_ids=["citation-approved"],
            )
        )
    for index in range(300):
        case_id = f"case-{index % 50:03d}"
        label = "incorrect" if index < 30 else "correct"
        truth["adaptations"].append(
            {
                "id": f"pass-adaptation-{index:03d}",
                "case_id": case_id,
                "artifact_id": f"pass-resource-artifact-{index:03d}",
                "labels": {"reviewer-one": label, "reviewer-two": label},
                "final_label": label,
            }
        )
        truth["artifact_manifest"]["artifacts"].append(
            _artifact(
                f"pass-resource-artifact-{index:03d}",
                "resource_output",
                subject_id=f"pass-adaptation-{index:03d}",
                case_id=case_id,
                citation_ids=["citation-approved"],
            )
        )
    truth["coverage_universe"] = [
        {"kp_id": "kp-001", "weight": 2.0, "covered": True, "evidence_ids": ["e-001"]},
        {"kp_id": "kp-002", "weight": 3.0, "covered": True, "evidence_ids": ["e-002"]},
    ]
    truth["artifact_manifest"]["artifacts"].append(
        _artifact(
            "e-001",
            "coverage_evidence",
            subject_id="kp-001",
            citation_ids=["citation-approved"],
        )
    )
    _refresh_manifest_hash(truth)
    return truth


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
    def test_accepts_subject_bound_artifact_manifest(self):
        self.assertEqual(validate_truth(valid_truth()), [])

    def test_rejects_reused_artifact_across_records(self):
        truth = valid_truth()
        for record in truth["claims"]:
            record["artifact_id"] = "claim-artifact-000"

        self.assertIn(
            "claims artifact_id must not be reused across records",
            validate_truth(truth),
        )

    def test_rejects_orphan_artifact_for_every_kind(self):
        cases = (
            ("profile_snapshot", "profile-orphan", None, []),
            ("case_input", "case-orphan", None, []),
            ("claim_output", "claim-orphan", "case-000", ["citation-approved"]),
            (
                "resource_output",
                "adaptation-orphan",
                "case-000",
                ["citation-approved"],
            ),
            (
                "coverage_evidence",
                "kp-orphan",
                None,
                ["citation-approved"],
            ),
        )
        for kind, subject_id, case_id, citation_ids in cases:
            with self.subTest(kind=kind):
                truth = valid_truth()
                truth["artifact_manifest"]["artifacts"].append(
                    _artifact(
                        f"orphan-{kind}",
                        kind,
                        subject_id=subject_id,
                        case_id=case_id,
                        citation_ids=citation_ids,
                    )
                )
                _refresh_manifest_hash(truth)

                self.assertIn(
                    f"artifact_manifest {kind} ids must exactly match referenced ids",
                    validate_truth(truth),
                )

    def test_allows_repeated_output_evidence_and_cross_profile_case_content(self):
        truth = passing_truth()
        artifacts = {
            artifact["id"]: artifact
            for artifact in truth["artifact_manifest"]["artifacts"]
        }
        for source_id, target_id in (
            ("pass-claim-artifact-000", "pass-claim-artifact-001"),
            ("pass-resource-artifact-000", "pass-resource-artifact-001"),
            ("e-001", "e-002"),
            ("case-artifact-000", "case-artifact-001"),
        ):
            artifacts[target_id]["content"] = artifacts[source_id]["content"]
            artifacts[target_id]["sha256"] = artifacts[source_id]["sha256"]
        _refresh_manifest_hash(truth)

        self.assertEqual(validate_truth(truth), [])

    def test_rejects_profile_artifact_reuse_and_duplicate_profile_content(self):
        for mutation, expected in (
            (
                "reuse",
                "dataset.profile_artifact_ids must reference at least 3 distinct artifacts",
            ),
            (
                "content",
                "profile_snapshot content hashes must be unique",
            ),
        ):
            with self.subTest(mutation=mutation):
                truth = valid_truth()
                if mutation == "reuse":
                    truth["dataset"]["profile_artifact_ids"]["profile-2"] = (
                        "profile-artifact-1"
                    )
                else:
                    first, second = truth["artifact_manifest"]["artifacts"][:2]
                    second["content"] = first["content"]
                    second["sha256"] = first["sha256"]
                    _refresh_manifest_hash(truth)
                self.assertIn(expected, validate_truth(truth))

    def test_rejects_duplicate_case_content_under_the_same_profile(self):
        truth = valid_truth()
        artifacts = {
            artifact["id"]: artifact
            for artifact in truth["artifact_manifest"]["artifacts"]
        }
        source = artifacts["case-artifact-000"]
        clone = artifacts["case-artifact-003"]
        clone["content"] = source["content"]
        clone["sha256"] = source["sha256"]
        _refresh_manifest_hash(truth)

        self.assertIn(
            "dataset cases must contain at least 50 unique profile/case content pairs",
            validate_truth(truth),
        )

    def test_rejects_wrong_artifact_subject_and_case_ownership(self):
        mutations = (
            ("profile-artifact-1", "subject_id", "profile-2", "profile profile-1 artifact subject_id mismatch"),
            ("case-artifact-000", "subject_id", "case-001", "case case-000 artifact subject_id mismatch"),
            ("claim-artifact-000", "subject_id", "claim-001", "claim claim-000 artifact subject_id mismatch"),
            ("claim-artifact-000", "case_id", "case-001", "claim claim-000 artifact case_id mismatch"),
            ("e-002", "subject_id", "kp-001", "coverage point kp-002 evidence subject_id mismatch"),
        )
        for artifact_id, field, value, expected in mutations:
            with self.subTest(artifact_id=artifact_id, field=field):
                truth = valid_truth()
                artifact = next(
                    item
                    for item in truth["artifact_manifest"]["artifacts"]
                    if item["id"] == artifact_id
                )
                artifact[field] = value
                _refresh_manifest_hash(truth)
                self.assertIn(expected, validate_truth(truth))

    def test_rejects_boolean_truth_and_manifest_versions(self):
        for location in ("truth", "manifest"):
            with self.subTest(location=location):
                truth = valid_truth()
                if location == "truth":
                    truth["version"] = True
                else:
                    truth["artifact_manifest"]["version"] = True
                    _refresh_manifest_hash(truth)
                self.assertTrue(
                    any("version must be 1" in error for error in validate_truth(truth))
                )

    def test_rejects_nested_lone_surrogate_before_hashing(self):
        truth = valid_truth()
        truth["artifact_manifest"]["artifacts"][0]["content"] = "bad\ud800text"

        self.assertEqual(
            validate_truth(truth),
            [
                "truth.artifact_manifest.artifacts[0].content contains text "
                "that is not strict UTF-8"
            ],
        )

    def test_rejects_truth_that_only_names_artifacts_without_a_manifest(self):
        truth = valid_truth()
        del truth["artifact_manifest"]
        del truth["provenance"]["artifact_manifest_sha256"]

        scorecard = build_scorecard(truth)

        self.assertEqual(scorecard["overall_status"], "not_assessable")
        self.assertTrue(
            any("artifact_manifest" in error for error in scorecard["data_quality"]["errors"])
        )

    def test_rejects_manifest_and_content_hash_mismatches(self):
        for mutation, expected in (
            ("manifest", "provenance.artifact_manifest_sha256"),
            ("content", "artifact profile-artifact-1 sha256"),
        ):
            with self.subTest(mutation=mutation):
                truth = valid_truth()
                if mutation == "manifest":
                    truth["provenance"]["artifact_manifest_sha256"] = "0" * 64
                else:
                    truth["artifact_manifest"]["artifacts"][0]["content"] += " changed"
                    _refresh_manifest_hash(truth)
                self.assertTrue(
                    any(expected in error for error in validate_truth(truth)),
                    validate_truth(truth),
                )

    def test_rejects_output_artifact_without_an_approved_citation(self):
        truth = valid_truth()
        artifact = next(
            item
            for item in truth["artifact_manifest"]["artifacts"]
            if item["id"] == "claim-artifact-000"
        )
        artifact["citation_ids"] = []
        _refresh_manifest_hash(truth)

        self.assertIn(
            "artifact claim-artifact-000 must cite at least one approved citation",
            validate_truth(truth),
        )

    def test_rejects_unknown_citation_and_coverage_evidence_ids(self):
        for mutation, expected in (
            ("citation", "artifact claim-artifact-000 references unknown citation_id"),
            ("coverage", "covered coverage point kp-002 references unknown evidence_id"),
        ):
            with self.subTest(mutation=mutation):
                truth = valid_truth()
                if mutation == "citation":
                    artifact = next(
                        item
                        for item in truth["artifact_manifest"]["artifacts"]
                        if item["id"] == "claim-artifact-000"
                    )
                    artifact["citation_ids"] = ["missing-citation"]
                    _refresh_manifest_hash(truth)
                else:
                    truth["coverage_universe"][1]["evidence_ids"] = ["missing-evidence"]
                self.assertTrue(
                    any(expected in error for error in validate_truth(truth)),
                    validate_truth(truth),
                )

    def test_rejects_unknown_fields_at_every_contract_level(self):
        mutations = {
            "top": lambda truth: truth.__setitem__("verdict", "pass"),
            "dataset": lambda truth: truth["dataset"].__setitem__("metadata", {}),
            "provenance": lambda truth: truth["provenance"].__setitem__("verdict", "pass"),
            "review_protocol": lambda truth: truth["review_protocol"].__setitem__("verdict", "pass"),
            "roster": lambda truth: truth["review_protocol"]["human_reviewer_roster"][0].__setitem__("role", "judge"),
            "case": lambda truth: truth["dataset"]["cases"][0].__setitem__("difficulty", 5),
            "record": lambda truth: truth["claims"][0].__setitem__("metadata", {"system_conclusion": "no"}),
            "coverage": lambda truth: truth["coverage_universe"][0].__setitem__("verdict", "covered"),
            "manifest": lambda truth: truth["artifact_manifest"].__setitem__("verdict", "valid"),
            "artifact": lambda truth: truth["artifact_manifest"]["artifacts"][0].__setitem__("metadata", {}),
            "citation": lambda truth: truth["artifact_manifest"]["citations"][0].__setitem__("verdict", "valid"),
        }
        for level, mutate in mutations.items():
            with self.subTest(level=level):
                truth = valid_truth()
                mutate(truth)
                if level in {"manifest", "artifact", "citation"}:
                    _refresh_manifest_hash(truth)
                self.assertTrue(
                    any("unknown fields" in error for error in validate_truth(truth)),
                    validate_truth(truth),
                )

    def test_profile_and_case_artifacts_must_resolve_to_expected_kinds(self):
        mutations = (
            lambda truth: truth["dataset"]["profile_artifact_ids"].__setitem__(
                "profile-1", "case-artifact-000"
            ),
            lambda truth: truth["dataset"]["cases"][0].__setitem__(
                "artifact_id", "profile-artifact-1"
            ),
            lambda truth: truth["claims"][0].__setitem__(
                "artifact_id", "resource-artifact-000"
            ),
            lambda truth: truth["adaptations"][0].__setitem__(
                "artifact_id", "claim-artifact-000"
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                truth = valid_truth()
                mutate(truth)
                self.assertTrue(validate_truth(truth))

    def test_common_assessable_pairs_must_cover_fifty_cases(self):
        truth = valid_truth()
        record = truth["claims"][-1]
        record["labels"]["reviewer-two"] = "unassessable"
        record["adjudicated_by"] = "adjudicator"

        errors = validate_truth(truth)

        self.assertIn("claims common reviewer pairs must cover at least 50 distinct case_ids", errors)

    def test_small_sample_perfect_kappa_is_not_assessable(self):
        truth = valid_truth()
        for record in truth["claims"][2:]:
            record["labels"] = {
                "reviewer-one": "unassessable",
                "reviewer-two": "unassessable",
            }
            record["final_label"] = "unassessable"

        scorecard = build_scorecard(truth)

        self.assertEqual(scorecard["overall_status"], "not_assessable")
        self.assertEqual(scorecard["kappa"]["hallucination"]["decision"], False)

    def test_rejects_missing_dataset_id(self):
        truth = valid_truth()
        del truth["dataset"]["dataset_id"]
        self.assertEqual(
            validate_truth(truth),
            ["dataset.dataset_id must be a non-empty stable string"],
        )

    def test_rejects_empty_or_whitespace_dataset_id(self):
        for dataset_id in ("", "   ", " frozen-eval-v1 "):
            with self.subTest(dataset_id=dataset_id):
                truth = valid_truth()
                truth["dataset"]["dataset_id"] = dataset_id
                self.assertEqual(
                    validate_truth(truth),
                    ["dataset.dataset_id must be a non-empty stable string"],
                )

    def test_valid_dataset_id_is_in_report_provenance(self):
        scorecard = build_scorecard(valid_truth())
        self.assertEqual(scorecard["provenance"]["dataset_id"], "frozen-eval-v1")

    def test_rejects_fewer_than_fifty_cases(self):
        truth = valid_truth()
        truth["dataset"]["cases"] = truth["dataset"]["cases"][:49]
        truth["claims"] = truth["claims"][:49]
        truth["adaptations"] = truth["adaptations"][:49]
        self.assertIn("at least 50 cases are required", validate_truth(truth))

    def test_rejects_fewer_than_three_profiles(self):
        truth = valid_truth()
        truth["dataset"]["profile_ids"] = ["profile-1", "profile-2"]
        del truth["dataset"]["profile_artifact_ids"]["profile-3"]
        errors = validate_truth(truth)
        self.assertIn(
            "dataset.profile_ids must contain at least 3 distinct non-empty values",
            errors,
        )
        self.assertIn(
            "dataset.profile_artifact_ids must reference at least 3 distinct artifacts",
            errors,
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
        for artifact_id in ("claim-artifact-001", "resource-artifact-001"):
            artifact = next(
                item
                for item in truth["artifact_manifest"]["artifacts"]
                if item["id"] == artifact_id
            )
            artifact["case_id"] = "case-000"
        _refresh_manifest_hash(truth)
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

    def test_accepts_two_human_reviewers_when_all_records_agree(self):
        truth = valid_truth()
        truth["review_protocol"]["human_reviewer_roster"].pop()
        self.assertEqual(validate_truth(truth), [])

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
        self.assertIn(
            "covered coverage point kp-002 needs evidence",
            validate_truth(truth),
        )

    def test_rejects_whitespace_only_coverage_evidence(self):
        truth = valid_truth()
        truth["coverage_universe"][1]["evidence_ids"] = ["   "]
        self.assertIn(
            "covered coverage point kp-002 needs evidence",
            validate_truth(truth),
        )

    def test_rejects_normalized_duplicate_coverage_ids(self):
        truth = valid_truth()
        truth["coverage_universe"][0]["kp_id"] = " KP-001 "
        truth["coverage_universe"][1]["kp_id"] = "kp-001"
        evidence = next(
            item
            for item in truth["artifact_manifest"]["artifacts"]
            if item["id"] == "e-002"
        )
        evidence["subject_id"] = "kp-001"
        _refresh_manifest_hash(truth)
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
        truth = passing_truth()
        truth["claims"][1]["final_label"] = "unassessable"
        truth["claims"][1]["labels"] = {
            "reviewer-one": "unassessable",
            "reviewer-two": "unassessable",
        }
        scorecard = build_scorecard(truth)
        self.assertEqual(scorecard["hallucination_rate"]["denominator"], 199)
        self.assertEqual(scorecard["assessable_share"]["claims"], 0.995)
        self.assertEqual(scorecard["assessable_share"]["adaptations"], 1.0)

    def test_multiple_claims_for_one_case_are_kept_as_one_bootstrap_cluster(self):
        truth = valid_truth()
        truth["claims"].append(
            {
                "id": "claim-extra",
                "case_id": "case-000",
                "artifact_id": "claim-artifact-extra",
                "labels": {"reviewer-one": "yes", "reviewer-two": "yes"},
                "final_label": "yes",
            }
        )
        truth["artifact_manifest"]["artifacts"].append(
            _artifact(
                "claim-artifact-extra",
                "claim_output",
                subject_id="claim-extra",
                case_id="case-000",
                citation_ids=["citation-approved"],
            )
        )
        _refresh_manifest_hash(truth)
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

    def test_rejects_overflowing_coverage_weight_totals(self):
        for covered_values in ((True, True), (False, True)):
            with self.subTest(covered_values=covered_values):
                truth = valid_truth()
                for point, covered in zip(truth["coverage_universe"], covered_values):
                    point["weight"] = 1e308
                    point["covered"] = covered

                scorecard = build_scorecard(truth)

                self.assertEqual(scorecard["overall_status"], "not_assessable")
                self.assertTrue(
                    any("coverage" in error and "finite" in error for error in scorecard["data_quality"]["errors"]),
                    scorecard["data_quality"]["errors"],
                )

    def test_rejects_claims_that_do_not_cover_fifty_distinct_cases(self):
        for key in ("claims", "adaptations"):
            with self.subTest(key=key):
                truth = valid_truth()
                for record in truth[key]:
                    record["case_id"] = "case-000"
                    artifact = next(
                        item
                        for item in truth["artifact_manifest"]["artifacts"]
                        if item["id"] == record["artifact_id"]
                    )
                    artifact["case_id"] = "case-000"
                _refresh_manifest_hash(truth)
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
        artifact = next(
            item
            for item in truth["artifact_manifest"]["artifacts"]
            if item["id"] == "claim-artifact-001"
        )
        artifact["case_id"] = "case-000"
        _refresh_manifest_hash(truth)
        self.assertEqual(
            validate_truth(truth),
            ["claims must cover at least 50 distinct case_ids"],
        )

    def test_rejects_normalized_duplicate_dataset_profile_ids(self):
        truth = valid_truth()
        truth["dataset"]["profile_ids"] = ["profile-1", "PROFILE-1", "profile-2", "profile-3"]
        self.assertEqual(
            validate_truth(truth),
            ["duplicate dataset profile_id: PROFILE-1"],
        )

    def test_rejects_invalid_dataset_profile_id_entries_without_reporting_extra_profiles(self):
        for profile_id in ("", None, "   ", " profile-4", "profile-4 "):
            with self.subTest(profile_id=profile_id):
                truth = valid_truth()
                truth["dataset"]["profile_ids"].append(profile_id)

                self.assertEqual(
                    validate_truth(truth),
                    [
                        "dataset.profile_ids[3] must be a non-empty trimmed string"
                    ],
                )
                scorecard = build_scorecard(truth)
                self.assertEqual(scorecard["overall_status"], "not_assessable")
                self.assertNotEqual(scorecard["data_quality"]["gates"]["profiles"]["actual"], 6)


class TestScorecard(unittest.TestCase):
    def test_limitations_cannot_be_mutated_across_scorecards(self):
        first = build_scorecard(passing_truth())
        first["limitations"].append("caller pollution")

        second = build_scorecard(passing_truth())

        self.assertNotIn("caller pollution", second["limitations"])

    def test_every_status_reports_the_complete_trust_boundary(self):
        expected = (
            "Manifest and hash validation proves only internal content/reference binding; "
            "it does not prove the real-world authenticity of sources, excerpts, "
            "signatures, or reviewer identities."
        )
        for truth in (passing_truth(), valid_truth(), {"version": 1}):
            with self.subTest(status=build_scorecard(truth)["overall_status"]):
                self.assertIn(expected, build_scorecard(truth)["limitations"])

    def test_scorecard_thresholds_come_from_config(self):
        script = """
import json
import config
config.TARGET_HALLUCINATION = 0.123
config.TARGET_ADAPT = 0.789
config.TARGET_COVERAGE = 0.654
config.FORMAL_KAPPA_MIN = 0.432
from evalkit import formal_scorecard
card = formal_scorecard._not_assessable_scorecard({}, [])
print(json.dumps({
    "hallucination": card["hallucination_rate"]["threshold"],
    "adaptation": card["adaptation_accuracy"]["threshold"],
    "coverage": card["coverage"]["threshold"],
    "kappa": card["data_quality"]["thresholds"]["minimum_kappa"],
}))
"""
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "hallucination": 0.123,
                "adaptation": 0.789,
                "coverage": 0.654,
                "kappa": 0.432,
            },
        )

    def test_complete_truth_that_meets_every_conservative_gate_passes(self):
        scorecard = build_scorecard(passing_truth())

        self.assertEqual(scorecard["overall_status"], "pass")
        self.assertTrue(scorecard["hallucination_rate"]["conservative_decision"])
        self.assertTrue(scorecard["adaptation_accuracy"]["conservative_decision"])
        self.assertTrue(scorecard["coverage"]["conservative_decision"])
        self.assertEqual(scorecard["data_quality"]["decision"], "pass")
        self.assertIn("provenance", scorecard)
        self.assertIn("limitations", scorecard)
        self.assertEqual(scorecard["data_quality"]["gates"]["profiles"]["actual"], 3)

    def test_markdown_renders_each_data_quality_gate_with_json_values(self):
        scorecard = build_scorecard(passing_truth())
        markdown = render_scorecard_markdown(scorecard)

        gates = scorecard["data_quality"]["gates"]
        for gate_name in (
            "cases",
            "profiles",
            "claims_assessable_share",
            "adaptations_assessable_share",
            "hallucination_kappa",
            "adaptation_kappa",
            "claims_common_pair_cases",
            "claims_common_pair_share",
            "adaptations_common_pair_cases",
            "adaptations_common_pair_share",
        ):
            gate = gates[gate_name]
            self.assertIn(
                (
                    f"- {gate_name}: actual {gate['actual']:.6f}; "
                    f"threshold {gate['operator']} {gate['threshold']:.6f}; "
                    f"decision: {gate['decision']}"
                ),
                markdown,
            )

    def test_markdown_matches_json_intervals_agreement_and_assessability(self):
        scorecard = build_scorecard(passing_truth())
        markdown = render_scorecard_markdown(scorecard)

        for key in ("hallucination_rate", "adaptation_accuracy", "coverage"):
            metric = scorecard[key]
            self.assertIn(f"- Wilson 95% interval: {_interval_text(metric['wilson_interval'])}", markdown)
            self.assertIn(
                f"- Cluster bootstrap 95% interval: {_interval_text(metric['bootstrap_interval'])}",
                markdown,
            )
            self.assertIn(f"- Conservative interval: {_interval_text(metric['interval'])}", markdown)
        for kind, key in (("Claims", "hallucination"), ("Adaptations", "adaptation")):
            kappa = scorecard["kappa"][key]
            self.assertIn(f"### {kind}", markdown)
            self.assertIn(f"- Paired ratings (n): {kappa['n']}", markdown)
            self.assertIn(f"- Raw agreement: {kappa['agreement']:.6f}", markdown)
            self.assertIn(f"- Cohen's Kappa: {kappa['value']:.6f}", markdown)
        self.assertIn(
            f"- Claims assessable share: {scorecard['assessable_share']['claims']:.6f}",
            markdown,
        )
        self.assertIn(
            f"- Claims common-pair share: {scorecard['common_pair']['claims']['share']:.6f}",
            markdown,
        )
        self.assertIn(
            f"- Claims common-pair distinct cases: {scorecard['common_pair']['claims']['cases']}",
            markdown,
        )

    def test_point_estimate_above_adaptation_target_but_lower_bound_below_target_fails(self):
        truth = valid_truth()
        for index, record in enumerate(truth["adaptations"]):
            label = "correct" if index < 43 else "incorrect"
            record["labels"] = {"reviewer-one": label, "reviewer-two": label}
            record["final_label"] = label
        truth["coverage_universe"][0]["covered"] = True
        truth["coverage_universe"][0]["evidence_ids"] = ["e-001"]
        truth["artifact_manifest"]["artifacts"].append(
            _artifact(
                "e-001",
                "coverage_evidence",
                subject_id="kp-001",
                citation_ids=["citation-approved"],
            )
        )
        _refresh_manifest_hash(truth)

        scorecard = build_scorecard(truth)

        adaptation = scorecard["adaptation_accuracy"]
        self.assertEqual(adaptation["point_estimate"], 0.86)
        self.assertGreaterEqual(adaptation["point_estimate"], adaptation["threshold"])
        self.assertLess(adaptation["interval"][0], adaptation["threshold"])
        self.assertFalse(adaptation["conservative_decision"])
        self.assertEqual(scorecard["overall_status"], "fail")

    def test_invalid_truth_is_reported_as_not_assessable_with_quality_errors(self):
        truth = valid_truth()
        truth["status"] = "draft"

        scorecard = build_scorecard(truth)

        self.assertEqual(scorecard["overall_status"], "not_assessable")
        self.assertEqual(scorecard["data_quality"]["decision"], "not_assessable")
        self.assertIn("truth status must be frozen", scorecard["limitations"])


class TestCli(unittest.TestCase):
    def _run_cli(self, truth: dict, output: Path) -> subprocess.CompletedProcess[str]:
        truth_path = output.parent / "truth.json"
        truth_path.write_text(json.dumps(truth), encoding="utf-8")
        environment = os.environ.copy()
        for key in (
            "AGENTEDU_MINIMAX_API_KEY",
            "AGENTEDU_DEEPSEEK_API_KEY",
            "AGENTEDU_API_KEY",
        ):
            environment[key] = ""
        return subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                "-m",
                "evalkit.formal_scorecard",
                "--truth",
                str(truth_path),
                "--out",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

    def test_cli_writes_byte_deterministic_reports_for_assessable_pass(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = self._run_cli(passing_truth(), root / "first")
            second = self._run_cli(passing_truth(), root / "second")

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(
                (root / "first" / "scorecard.json").read_bytes(),
                (root / "second" / "scorecard.json").read_bytes(),
            )
            self.assertEqual(
                (root / "first" / "scorecard.md").read_bytes(),
                (root / "second" / "scorecard.md").read_bytes(),
            )
            report = json.loads((root / "first" / "scorecard.json").read_text("utf-8"))
            markdown = (root / "first" / "scorecard.md").read_text("utf-8")
            self.assertEqual(report["overall_status"], "pass")
            self.assertIn("official metric evidence gate", markdown)
            self.assertIn("not the jury's 100-point score", markdown)

    def test_cli_writes_not_assessable_reports_for_empty_template(self):
        template = Path(__file__).resolve().parents[1] / "data" / "evaluation" / "formal_truth.template.json"
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "report"
            truth = json.loads(template.read_text(encoding="utf-8"))
            result = self._run_cli(truth, output)

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertTrue((output / "scorecard.json").is_file())
            self.assertTrue((output / "scorecard.md").is_file())
            report = json.loads((output / "scorecard.json").read_text("utf-8"))
            self.assertEqual(report["overall_status"], "not_assessable")

    def test_cli_writes_reports_and_exits_zero_for_assessable_metric_fail(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "report"
            result = self._run_cli(valid_truth(), output)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((output / "scorecard.json").is_file())
            self.assertTrue((output / "scorecard.md").is_file())
            report = json.loads((output / "scorecard.json").read_text("utf-8"))
            self.assertEqual(report["overall_status"], "fail")

    def test_cli_writes_not_assessable_reports_for_top_level_array(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "report"
            result = self._run_cli([], output)

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertTrue((output / "scorecard.json").is_file())
            self.assertTrue((output / "scorecard.md").is_file())

    def test_cli_handles_invalid_utf8_without_traceback_and_writes_both_reports(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            truth_path = root / "truth.json"
            output = root / "report"
            truth_path.write_bytes(b"\xff\xfe\xfa")

            result = subprocess.run(
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    "-m",
                    "evalkit.formal_scorecard",
                    "--truth",
                    str(truth_path),
                    "--out",
                    str(output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            report = json.loads((output / "scorecard.json").read_text("utf-8"))
            self.assertEqual(report["overall_status"], "not_assessable")
            self.assertTrue((output / "scorecard.md").is_file())

    def test_cli_handles_json_escaped_lone_surrogate_without_traceback(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            truth_path = root / "truth.json"
            output = root / "report"
            truth_path.write_text('{"version": 1, "value": "\\ud800"}', encoding="ascii")

            result = subprocess.run(
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    "-m",
                    "evalkit.formal_scorecard",
                    "--truth",
                    str(truth_path),
                    "--out",
                    str(output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            report = json.loads((output / "scorecard.json").read_text("utf-8"))
            self.assertEqual(report["overall_status"], "not_assessable")
            self.assertEqual(
                report["data_quality"]["errors"],
                ["truth.value contains text that is not strict UTF-8"],
            )
            self.assertTrue((output / "scorecard.md").is_file())

    def test_cli_writes_not_assessable_reports_for_empty_object_and_missing_fields(self):
        for truth in ({}, {"version": 1}):
            with self.subTest(truth=truth):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    output = Path(temporary_directory) / "report"
                    result = self._run_cli(truth, output)

                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertTrue((output / "scorecard.json").is_file())
                    self.assertTrue((output / "scorecard.md").is_file())
