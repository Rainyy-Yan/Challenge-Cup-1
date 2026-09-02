"""Offline statistical helpers for independently adjudicated formal truth."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
from datetime import datetime
from pathlib import Path
from statistics import NormalDist

from config import (
    FORMAL_KAPPA_MIN,
    TARGET_ADAPT,
    TARGET_COVERAGE,
    TARGET_HALLUCINATION,
)


HALLUCINATION_LABELS = {"yes", "no", "unassessable"}
ADAPTATION_LABELS = {"correct", "incorrect", "unassessable"}
HALLUCINATION_THRESHOLD = TARGET_HALLUCINATION
ADAPTATION_THRESHOLD = TARGET_ADAPT
COVERAGE_THRESHOLD = TARGET_COVERAGE
KAPPA_THRESHOLD = FORMAL_KAPPA_MIN

TOP_LEVEL_FIELDS = {
    "version",
    "status",
    "frozen_at",
    "seed",
    "blind_to_system_output",
    "independent_ratings",
    "dataset",
    "provenance",
    "reviewers",
    "review_protocol",
    "claims",
    "adaptations",
    "coverage_universe",
    "artifact_manifest",
}
ARTIFACT_KINDS = {
    "profile_snapshot",
    "case_input",
    "claim_output",
    "resource_output",
    "coverage_evidence",
}


def _unknown_fields(value: dict, allowed: set[str], label: str, errors: list[str]) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        errors.append(f"{label} has unknown fields: {', '.join(unknown)}")


def _missing_fields(value: dict, required: set[str], label: str, errors: list[str]) -> None:
    missing = sorted(required - set(value))
    if missing:
        errors.append(f"{label} is missing fields: {', '.join(missing)}")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _finite_fsum(values: list[float]) -> float | None:
    try:
        result = math.fsum(values)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _quality_gates(
    *,
    cases: int | None,
    profiles: int | None,
    claims_assessable_share: float | None,
    adaptations_assessable_share: float | None,
    claims_common_pair_cases: int | None,
    claims_common_pair_share: float | None,
    adaptations_common_pair_cases: int | None,
    adaptations_common_pair_share: float | None,
    hallucination_kappa: float | None,
    adaptation_kappa: float | None,
) -> dict:
    """Return explicit actual/threshold decisions for every quality gate."""
    gates = {
        "cases": (cases, 50),
        "profiles": (profiles, 3),
        "claims_assessable_share": (claims_assessable_share, 0.95),
        "adaptations_assessable_share": (adaptations_assessable_share, 0.95),
        "claims_common_pair_cases": (claims_common_pair_cases, 50),
        "claims_common_pair_share": (claims_common_pair_share, 0.95),
        "adaptations_common_pair_cases": (adaptations_common_pair_cases, 50),
        "adaptations_common_pair_share": (adaptations_common_pair_share, 0.95),
        "hallucination_kappa": (hallucination_kappa, KAPPA_THRESHOLD),
        "adaptation_kappa": (adaptation_kappa, KAPPA_THRESHOLD),
    }
    return {
        name: {
            "actual": actual,
            "threshold": threshold,
            "operator": ">=",
            "decision": actual is not None and actual >= threshold,
        }
        for name, (actual, threshold) in gates.items()
    }


def wilson_interval(
    successes: int, total: int, confidence: float = 0.95
) -> tuple[float, float]:
    """Return a two-sided Wilson score interval, closed at zero observations."""
    if total < 0 or successes < 0 or successes > total:
        raise ValueError("successes must be between zero and total")
    if total == 0:
        return (0.0, 0.0)
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between zero and one")

    z = NormalDist().inv_cdf(1.0 - (1.0 - confidence) / 2.0)
    proportion = successes / total
    denominator = 1.0 + z * z / total
    centre = (proportion + z * z / (2.0 * total)) / denominator
    margin = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / total + z * z / (4.0 * total * total)
        )
        / denominator
    )
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _linear_quantile(values: list[float], probability: float) -> float:
    """Return an inclusive linear-interpolated quantile for sorted values."""
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * probability
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return values[lower_index]
    fraction = position - lower_index
    return values[lower_index] + fraction * (values[upper_index] - values[lower_index])


def cluster_bootstrap_interval(
    records: list[dict],
    positive: set[str],
    *,
    seed: int,
    samples: int,
) -> tuple[float, float]:
    """Bootstrap a label proportion by resampling complete case clusters."""
    if not records or samples <= 0:
        return (0.0, 0.0)

    clusters: dict[str, list[dict]] = {}
    for record in records:
        case_id = record.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("each bootstrap record needs a non-empty case_id")
        clusters.setdefault(case_id, []).append(record)

    cluster_values = list(clusters.values())
    generator = random.Random(seed)
    estimates: list[float] = []
    for _ in range(samples):
        draw = [cluster_values[generator.randrange(len(cluster_values))] for _ in cluster_values]
        sampled_records = [record for cluster in draw for record in cluster]
        estimates.append(
            sum(record.get("label") in positive for record in sampled_records)
            / len(sampled_records)
        )
    estimates.sort()
    return (_linear_quantile(estimates, 0.025), _linear_quantile(estimates, 0.975))


def _metric(records: list[dict], positive: set[str], *, seed: int) -> dict:
    successes = sum(record["label"] in positive for record in records)
    total = len(records)
    wilson = wilson_interval(successes, total)
    bootstrap = cluster_bootstrap_interval(records, positive, seed=seed, samples=10000)
    return {
        "numerator": successes,
        "denominator": total,
        "point_estimate": successes / total if total else 0.0,
        "wilson_interval": wilson,
        "bootstrap_interval": bootstrap,
        "interval": (min(wilson[0], bootstrap[0]), max(wilson[1], bootstrap[1])),
    }


def _normalise_identity(identity: object) -> str | None:
    if not isinstance(identity, str):
        return None
    normalised = identity.strip().casefold()
    return normalised or None


def _validate_artifact_manifest(data: dict, errors: list[str]) -> dict[str, dict]:
    manifest = data.get("artifact_manifest")
    if not isinstance(manifest, dict):
        errors.append("artifact_manifest must be an object")
        return {}
    manifest_fields = {"version", "artifacts", "citations"}
    _unknown_fields(manifest, manifest_fields, "artifact_manifest", errors)
    _missing_fields(manifest, manifest_fields, "artifact_manifest", errors)
    if manifest.get("version") != 1:
        errors.append("artifact_manifest.version must be 1")

    raw_citations = manifest.get("citations")
    valid_citations: set[str] = set()
    seen_citations: set[str] = set()
    if not isinstance(raw_citations, list):
        errors.append("artifact_manifest.citations must be a list")
    else:
        citation_fields = {
            "id",
            "source_id",
            "locator",
            "excerpt",
            "sha256",
            "review_status",
        }
        for index, citation in enumerate(raw_citations):
            label = f"citation {index}"
            if not isinstance(citation, dict):
                errors.append(f"{label} must be an object")
                continue
            raw_id = citation.get("id")
            citation_id = _normalise_identity(raw_id)
            display_id = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else str(index)
            label = f"citation {display_id}"
            before = len(errors)
            _unknown_fields(citation, citation_fields, label, errors)
            _missing_fields(citation, citation_fields, label, errors)
            if citation_id is None or raw_id != raw_id.strip():
                errors.append(f"citation {index} needs a non-empty trimmed id")
            elif citation_id in seen_citations:
                errors.append(f"duplicate citation id: {display_id}")
            else:
                seen_citations.add(citation_id)
            for field in ("source_id", "locator", "excerpt"):
                value = citation.get(field)
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"{label} {field} must be a non-empty string")
            excerpt = citation.get("excerpt")
            digest = citation.get("sha256")
            if not isinstance(excerpt, str) or not isinstance(digest, str) or digest != _text_sha256(excerpt):
                errors.append(f"{label} sha256 does not match excerpt")
            if citation.get("review_status") != "approved":
                errors.append(f"{label} review_status must be approved")
            if citation_id is not None and len(errors) == before:
                valid_citations.add(citation_id)

    raw_artifacts = manifest.get("artifacts")
    artifacts: dict[str, dict] = {}
    seen_artifacts: set[str] = set()
    if not isinstance(raw_artifacts, list):
        errors.append("artifact_manifest.artifacts must be a list")
    else:
        artifact_fields = {
            "id",
            "kind",
            "content",
            "sha256",
            "citation_ids",
            "review_status",
        }
        for index, artifact in enumerate(raw_artifacts):
            label = f"artifact {index}"
            if not isinstance(artifact, dict):
                errors.append(f"{label} must be an object")
                continue
            raw_id = artifact.get("id")
            artifact_id = _normalise_identity(raw_id)
            display_id = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else str(index)
            label = f"artifact {display_id}"
            before = len(errors)
            _unknown_fields(artifact, artifact_fields, label, errors)
            _missing_fields(artifact, artifact_fields, label, errors)
            if artifact_id is None or raw_id != raw_id.strip():
                errors.append(f"artifact {index} needs a non-empty trimmed id")
            elif artifact_id in seen_artifacts:
                errors.append(f"duplicate artifact id: {display_id}")
            else:
                seen_artifacts.add(artifact_id)
            kind = artifact.get("kind")
            if kind not in ARTIFACT_KINDS:
                errors.append(f"{label} has an invalid kind")
            content = artifact.get("content")
            digest = artifact.get("sha256")
            if not isinstance(content, str) or not content.strip():
                errors.append(f"{label} content must be non-empty")
            elif not isinstance(digest, str) or digest != _text_sha256(content):
                errors.append(f"{label} sha256 does not match content")
            citation_ids = artifact.get("citation_ids")
            valid_artifact_citations: set[str] = set()
            if not isinstance(citation_ids, list):
                errors.append(f"{label} citation_ids must be a list")
            else:
                seen_ids: set[str] = set()
                for raw_citation_id in citation_ids:
                    citation_id = _normalise_identity(raw_citation_id)
                    if citation_id is None or raw_citation_id != raw_citation_id.strip():
                        errors.append(f"{label} has an invalid citation_id")
                    elif citation_id in seen_ids:
                        errors.append(f"{label} has a duplicate citation_id")
                    elif citation_id not in valid_citations:
                        errors.append(f"{label} references unknown citation_id: {raw_citation_id}")
                    else:
                        seen_ids.add(citation_id)
                        valid_artifact_citations.add(citation_id)
            expected_status = "approved" if kind == "coverage_evidence" else "frozen"
            if artifact.get("review_status") != expected_status:
                errors.append(f"{label} review_status must be {expected_status}")
            if kind in {"claim_output", "resource_output", "coverage_evidence"} and not valid_artifact_citations:
                errors.append(f"{label} must cite at least one approved citation")
            if artifact_id is not None and len(errors) == before:
                artifacts[artifact_id] = artifact

    provenance = data.get("provenance")
    supplied_hash = (
        provenance.get("artifact_manifest_sha256")
        if isinstance(provenance, dict)
        else None
    )
    try:
        expected_hash = _text_sha256(_canonical_json(manifest))
    except (TypeError, ValueError):
        expected_hash = None
    if (
        not isinstance(supplied_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", supplied_hash) is None
        or supplied_hash != expected_hash
    ):
        errors.append(
            "provenance.artifact_manifest_sha256 must match the canonical artifact_manifest"
        )
    return artifacts


def _resolve_artifact(
    artifact_id: object,
    expected_kind: str,
    artifacts: dict[str, dict],
    label: str,
    errors: list[str],
) -> None:
    canonical = _normalise_identity(artifact_id)
    if canonical is None or not isinstance(artifact_id, str) or artifact_id != artifact_id.strip():
        errors.append(f"{label} needs a non-empty trimmed artifact_id")
        return
    artifact = artifacts.get(canonical)
    if artifact is None:
        errors.append(f"{label} references an unknown or invalid artifact_id")
    elif artifact.get("kind") != expected_kind:
        errors.append(f"{label} artifact_id must resolve to {expected_kind}")


def _human_reviewer_roster(data: dict, errors: list[str]) -> set[str] | None:
    """Validate identity attestations without claiming to prove real-world identity.

    Roster truth is an external-evidence obligation. This function validates only
    the explicit declaration contract supplied in the frozen truth data.
    """
    review_protocol = data.get("review_protocol")
    roster = (
        review_protocol.get("human_reviewer_roster")
        if isinstance(review_protocol, dict)
        else None
    )
    if not isinstance(roster, list):
        errors.append("review_protocol.human_reviewer_roster must be a list")
        return None

    roster_errors_before = len(errors)
    identities: set[str] = set()
    for index, entry in enumerate(roster):
        if not isinstance(entry, dict):
            errors.append(f"human_reviewer_roster entry {index} must be an object")
            continue
        _unknown_fields(
            entry,
            {"id", "attested_human"},
            f"human_reviewer_roster entry {index}",
            errors,
        )
        _missing_fields(
            entry,
            {"id", "attested_human"},
            f"human_reviewer_roster entry {index}",
            errors,
        )
        raw_identity = entry.get("id")
        identity = _normalise_identity(raw_identity)
        display_id = (
            raw_identity.strip() if isinstance(raw_identity, str) else str(index)
        )
        if identity is None:
            errors.append(f"human_reviewer_roster entry {index} needs a non-empty id")
        elif identity in identities:
            errors.append(f"duplicate human_reviewer_roster id: {display_id}")
        else:
            identities.add(identity)
        if entry.get("attested_human") is not True:
            errors.append(
                f"human_reviewer_roster entry {display_id} must set attested_human to true"
            )

    if len(identities) < 2:
        errors.append(
            "human_reviewer_roster must contain at least 2 distinct non-empty identities"
        )
    if len(errors) != roster_errors_before:
        return None
    return identities


def _canonical_case_id(case_id: object) -> str | None:
    if not isinstance(case_id, str) or not case_id.strip():
        return None
    return case_id.strip().casefold()


def _valid_dataset_profile_ids(profile_ids: object, errors: list[str]) -> set[str]:
    """Validate and normalize the sole profile universe used by reports."""
    valid_profile_ids: set[str] = set()
    if not isinstance(profile_ids, list):
        return valid_profile_ids
    for index, profile_id in enumerate(profile_ids):
        if (
            not isinstance(profile_id, str)
            or not profile_id.strip()
            or profile_id != profile_id.strip()
        ):
            errors.append(
                f"dataset.profile_ids[{index}] must be a non-empty trimmed string"
            )
            continue
        canonical_profile_id = profile_id.casefold()
        if canonical_profile_id in valid_profile_ids:
            errors.append(f"duplicate dataset profile_id: {profile_id}")
        else:
            valid_profile_ids.add(canonical_profile_id)
    return valid_profile_ids


def _reviewer_values(
    labels: object, reviewer_ids: list[str], allowed: set[str]
) -> tuple[str, str] | None:
    if not isinstance(labels, dict):
        return None
    normalised_labels: dict[str, object] = {}
    for identity, label in labels.items():
        normalised = _normalise_identity(identity)
        if normalised is None or normalised in normalised_labels:
            return None
        normalised_labels[normalised] = label
    if set(normalised_labels) != set(reviewer_ids):
        return None
    values = tuple(normalised_labels[reviewer] for reviewer in reviewer_ids)
    if not all(isinstance(value, str) and value in allowed for value in values):
        return None
    return (values[0], values[1])


def _cohen_kappa(left: list[str], right: list[str]) -> dict:
    """Compute Kappa from two reviewer columns, excluding unassessable pairs."""
    paired = [
        (left_label, right_label)
        for left_label, right_label in zip(left, right)
        if left_label != "unassessable" and right_label != "unassessable"
    ]
    total = len(paired)
    if not total:
        return {"n": 0, "agreement": None, "value": None}

    labels = {label for pair in paired for label in pair}
    agreement = sum(left_label == right_label for left_label, right_label in paired) / total
    expected = sum(
        (sum(left_label == label for left_label, _ in paired) / total)
        * (sum(right_label == label for _, right_label in paired) / total)
        for label in labels
    )
    value = (agreement - expected) / (1.0 - expected) if expected < 1.0 else None
    return {"n": total, "agreement": agreement, "value": value}


def _kappa_from_records(records: list[dict], reviewers: list[str], allowed: set[str]) -> dict:
    left: list[str] = []
    right: list[str] = []
    for record in records:
        labels = record.get("labels") if isinstance(record, dict) else None
        values = _reviewer_values(labels, reviewers, allowed)
        if values is None:
            continue
        left.append(values[0])
        right.append(values[1])
    return _cohen_kappa(left, right)


def _common_pair_stats(
    records: list[dict], reviewers: list[str], allowed: set[str]
) -> dict:
    paired_records = []
    for record in records:
        values = _reviewer_values(record.get("labels"), reviewers, allowed)
        if values is not None and "unassessable" not in values:
            paired_records.append(record)
    case_ids = {
        _canonical_case_id(record.get("case_id"))
        for record in paired_records
        if _canonical_case_id(record.get("case_id")) is not None
    }
    return {
        "records": len(paired_records),
        "cases": len(case_ids),
        "share": len(paired_records) / len(records) if records else 0.0,
    }


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _record_error_prefix(kind: str, record: dict, index: int) -> str:
    record_id = record.get("id")
    return record_id if isinstance(record_id, str) and record_id else str(index)


def _validate_records(
    data: dict,
    key: str,
    allowed: set[str],
    reviewers: list[str] | None,
    human_roster: set[str] | None,
    case_profiles: dict[str, str],
    artifacts: dict[str, dict],
    errors: list[str],
) -> list[dict]:
    records = data.get(key)
    if not isinstance(records, list):
        errors.append(f"{key} must be a list")
        return []

    accepted: list[dict] = []
    record_ids: set[str] = set()
    prohibited = {
        "system_conclusion",
        "machine_conclusion",
        "model_conclusion",
        "system_label",
        "machine_label",
        "model_label",
    }
    singular = key[:-1]
    expected_artifact_kind = "claim_output" if key == "claims" else "resource_output"
    allowed_fields = {"id", "case_id", "artifact_id", "labels", "final_label", "adjudicated_by"}
    required_fields = allowed_fields - {"adjudicated_by"}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            errors.append(f"{singular} {index} must be an object")
            continue
        display_id = _record_error_prefix(singular, record, index)
        prohibited_fields = prohibited.intersection(record)
        if prohibited_fields:
            errors.append(f"{singular} {display_id} exposes a prohibited system conclusion")
        other_unknown = set(record) - allowed_fields - prohibited
        if other_unknown:
            errors.append(
                f"{singular} {display_id} has unknown fields: {', '.join(sorted(other_unknown))}"
            )
        _missing_fields(record, required_fields, f"{singular} {display_id}", errors)
        record_id = record.get("id")
        canonical_record_id = _normalise_identity(record_id)
        if (
            canonical_record_id is None
            or not isinstance(record_id, str)
            or record_id != record_id.strip()
        ):
            errors.append(f"{singular} {index} needs a non-empty trimmed id")
        elif canonical_record_id in record_ids:
            errors.append(f"duplicate {singular} id: {record_id}")
        else:
            record_ids.add(canonical_record_id)

        case_id = record.get("case_id")
        canonical_case_id = _canonical_case_id(case_id)
        if not isinstance(case_id, str) or canonical_case_id is None:
            errors.append(f"{singular} {display_id} references an unknown case_id")
        elif case_id != case_id.strip():
            errors.append(f"{singular} {display_id} case_id must equal its stripped form")
        elif canonical_case_id not in case_profiles:
            errors.append(f"{singular} {display_id} references an unknown case_id")
        _resolve_artifact(
            record.get("artifact_id"),
            expected_artifact_kind,
            artifacts,
            f"{singular} {display_id}",
            errors,
        )
        accepted.append(record)

        if reviewers is None:
            continue
        values = _reviewer_values(record.get("labels"), reviewers, allowed)
        if values is None:
            errors.append(f"{singular} {display_id} has incomplete reviewer labels")

        final_label = record.get("final_label")
        final_label_valid = isinstance(final_label, str) and final_label in allowed
        if not final_label_valid:
            errors.append(f"{singular} {display_id} has an invalid final_label")
        if values is None or not final_label_valid:
            continue
        adjudicator_present = "adjudicated_by" in record
        adjudicator = _normalise_identity(record.get("adjudicated_by"))
        if adjudicator_present and adjudicator is None:
            errors.append(f"{singular} {display_id} adjudicator must be a non-empty identity")
        elif (
            adjudicator is not None
            and human_roster is not None
            and adjudicator not in human_roster
        ):
            errors.append(
                f"{singular} {display_id} adjudicator {record['adjudicated_by'].strip()} "
                "is not declared in human_reviewer_roster"
            )
        if values[0] == values[1]:
            if final_label != values[0]:
                errors.append(f"{singular} {display_id} final_label must equal the common reviewer label")
        else:
            if adjudicator is None:
                if not adjudicator_present:
                    errors.append(f"{singular} {display_id} has unresolved reviewer disagreement")
            elif adjudicator in reviewers:
                errors.append(f"{singular} {display_id} adjudicator must be distinct from both reviewers")
    return accepted


def validate_truth(data: dict) -> list[str]:
    """Return every non-cascading violation of the version-1 truth contract."""
    if not isinstance(data, dict):
        return ["truth must be an object"]

    errors: list[str] = []
    _unknown_fields(data, TOP_LEVEL_FIELDS, "truth", errors)
    if data.get("version") != 1:
        errors.append("truth version must be 1")
    if data.get("status") != "frozen":
        errors.append("truth status must be frozen")
    if not _valid_timestamp(data.get("frozen_at")):
        errors.append("frozen_at must be an ISO-8601 timestamp")
    seed = data.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        errors.append("seed must be a non-negative integer")
    if data.get("blind_to_system_output") is not True:
        errors.append("blind_to_system_output must be true")
    if data.get("independent_ratings") is not True:
        errors.append("independent_ratings must be true")

    provenance = data.get("provenance")
    if isinstance(provenance, dict):
        _unknown_fields(
            provenance,
            {"repository_sha", "artifact_manifest_sha256"},
            "provenance",
            errors,
        )
    sha = provenance.get("repository_sha") if isinstance(provenance, dict) else None
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        errors.append("provenance.repository_sha must be a 40-character lowercase hexadecimal SHA")

    artifacts = _validate_artifact_manifest(data, errors)

    review_protocol = data.get("review_protocol")
    if isinstance(review_protocol, dict):
        _unknown_fields(
            review_protocol,
            {"human_reviewer_roster"},
            "review_protocol",
            errors,
        )
    human_roster = _human_reviewer_roster(data, errors)
    raw_reviewers = data.get("reviewers")
    reviewers = (
        [_normalise_identity(reviewer) for reviewer in raw_reviewers]
        if isinstance(raw_reviewers, list)
        else []
    )
    reviewers_valid = (
        len(reviewers) == 2
        and all(reviewer is not None for reviewer in reviewers)
        and len(set(reviewers)) == 2
    )
    if not reviewers_valid:
        errors.append("reviewers must contain exactly two distinct non-empty identities")
        reviewer_ids: list[str] | None = None
    else:
        reviewer_ids = [reviewer for reviewer in reviewers if reviewer is not None]
        if human_roster is not None:
            for raw_reviewer, reviewer in zip(raw_reviewers, reviewer_ids):
                if reviewer not in human_roster:
                    errors.append(
                        f"reviewer {raw_reviewer.strip()} is not declared in "
                        "human_reviewer_roster"
                    )

    dataset = data.get("dataset")
    if isinstance(dataset, dict):
        dataset_fields = {
            "dataset_id",
            "profile_ids",
            "profile_artifact_ids",
            "cases",
        }
        _unknown_fields(dataset, dataset_fields, "dataset", errors)
    dataset_id = dataset.get("dataset_id") if isinstance(dataset, dict) else None
    if (
        not isinstance(dataset_id, str)
        or not dataset_id.strip()
        or dataset_id != dataset_id.strip()
    ):
        errors.append("dataset.dataset_id must be a non-empty stable string")
    cases = dataset.get("cases") if isinstance(dataset, dict) else None
    if not isinstance(cases, list):
        return errors + ["dataset.cases must be a list"]
    if len(cases) < 50:
        errors.append("at least 50 cases are required")

    profile_ids = dataset.get("profile_ids")
    valid_profile_ids = _valid_dataset_profile_ids(profile_ids, errors)
    if len(valid_profile_ids) < 3:
        errors.append("dataset.profile_ids must contain at least 3 distinct non-empty values")
    profile_universe_valid = len(valid_profile_ids) >= 3

    profile_artifact_ids = dataset.get("profile_artifact_ids")
    if not isinstance(profile_artifact_ids, dict):
        errors.append("dataset.profile_artifact_ids must be an object")
    else:
        normalised_mapping: dict[str, object] = {}
        mapping_valid = True
        for raw_profile_id, artifact_id in profile_artifact_ids.items():
            canonical_profile_id = _normalise_identity(raw_profile_id)
            if (
                canonical_profile_id is None
                or not isinstance(raw_profile_id, str)
                or raw_profile_id != raw_profile_id.strip()
                or canonical_profile_id in normalised_mapping
            ):
                errors.append("dataset.profile_artifact_ids has an invalid or duplicate profile key")
                mapping_valid = False
                continue
            normalised_mapping[canonical_profile_id] = artifact_id
        if set(normalised_mapping) != valid_profile_ids:
            errors.append(
                "dataset.profile_artifact_ids keys must exactly match dataset.profile_ids"
            )
            mapping_valid = False
        if mapping_valid:
            for profile_id, artifact_id in normalised_mapping.items():
                _resolve_artifact(
                    artifact_id,
                    "profile_snapshot",
                    artifacts,
                    f"profile {profile_id}",
                    errors,
                )

    case_profiles: dict[str, str] = {}
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"case {index} must be an object")
            continue
        _unknown_fields(case, {"id", "profile_id", "artifact_id"}, f"case {index}", errors)
        _missing_fields(case, {"id", "profile_id", "artifact_id"}, f"case {index}", errors)
        case_id = case.get("id")
        canonical_case_id = _canonical_case_id(case_id)
        display_id = case_id.strip() if canonical_case_id is not None else str(index)
        if canonical_case_id is None:
            errors.append(f"case {index} needs a non-empty id")
            continue
        if case_id != case_id.strip():
            errors.append(f"case {display_id} must equal its stripped form")
        if canonical_case_id in case_profiles:
            errors.append(f"duplicate case id: {display_id}")
            continue

        profile_id = case.get("profile_id")
        canonical_profile_id = _normalise_identity(profile_id)
        if canonical_profile_id is None:
            errors.append(f"case {display_id} needs a non-empty profile_id")
            continue
        if profile_universe_valid and canonical_profile_id not in valid_profile_ids:
            errors.append(f"case {display_id} references an undeclared profile_id")
        _resolve_artifact(
            case.get("artifact_id"),
            "case_input",
            artifacts,
            f"case {display_id}",
            errors,
        )
        case_profiles[canonical_case_id] = canonical_profile_id

    claims_errors_before = len(errors)
    claims = _validate_records(
        data,
        "claims",
        HALLUCINATION_LABELS,
        reviewer_ids,
        human_roster,
        case_profiles,
        artifacts,
        errors,
    )
    claims_records_valid = len(errors) == claims_errors_before
    adaptations_errors_before = len(errors)
    adaptations = _validate_records(
        data,
        "adaptations",
        ADAPTATION_LABELS,
        reviewer_ids,
        human_roster,
        case_profiles,
        artifacts,
        errors,
    )
    adaptations_records_valid = len(errors) == adaptations_errors_before
    for key, records, records_valid in (
        ("claims", claims, claims_records_valid),
        ("adaptations", adaptations, adaptations_records_valid),
    ):
        distinct_case_ids = {
            _canonical_case_id(record.get("case_id"))
            for record in records
            if (
                isinstance(record.get("case_id"), str)
                and record["case_id"] == record["case_id"].strip()
                and _canonical_case_id(record.get("case_id")) in case_profiles
            )
        }
        if len(case_profiles) >= 50 and len(distinct_case_ids) < 50:
            errors.append(f"{key} must cover at least 50 distinct case_ids")
        elif len(case_profiles) >= 50:
            covered_profiles = {case_profiles[case_id] for case_id in distinct_case_ids}
            if len(covered_profiles) < 3:
                errors.append(f"{key} must represent at least 3 distinct profile_ids")
        assessable_share = (
            sum(record.get("final_label") != "unassessable" for record in records) / len(records)
            if records
            else 0.0
        )
        if assessable_share < 0.95:
            errors.append(f"{key} assessable share is below 0.95")
        if (
            reviewer_ids is not None
            and records_valid
            and assessable_share >= 0.95
            and len(case_profiles) >= 50
            and len(distinct_case_ids) >= 50
        ):
            allowed = HALLUCINATION_LABELS if key == "claims" else ADAPTATION_LABELS
            common_pair = _common_pair_stats(records, reviewer_ids, allowed)
            if common_pair["cases"] < 50:
                errors.append(
                    f"{key} common reviewer pairs must cover at least 50 distinct case_ids"
                )
            if common_pair["share"] < 0.95:
                errors.append(f"{key} common reviewer pair share is below 0.95")

    coverage = data.get("coverage_universe")
    if not isinstance(coverage, list):
        errors.append("coverage_universe must be a list")
    else:
        valid_weights: list[float] = []
        covered_weights: list[float] = []
        all_weights_valid = True
        coverage_ids: set[str] = set()
        for index, point in enumerate(coverage):
            if not isinstance(point, dict):
                errors.append(f"coverage point {index} must be an object")
                continue
            _unknown_fields(
                point,
                {"kp_id", "weight", "covered", "evidence_ids"},
                f"coverage point {index}",
                errors,
            )
            _missing_fields(
                point,
                {"kp_id", "weight", "covered", "evidence_ids"},
                f"coverage point {index}",
                errors,
            )
            point_id = point.get("kp_id")
            has_point_id = isinstance(point_id, str) and bool(point_id.strip())
            display_id = point_id if has_point_id else str(index)
            if not has_point_id:
                errors.append(f"coverage point {index} needs a non-empty kp_id")
            elif point_id.strip().casefold() in coverage_ids:
                errors.append(f"duplicate coverage kp_id: {point_id}")
            else:
                coverage_ids.add(point_id.strip().casefold())
            weight = point.get("weight")
            if (
                isinstance(weight, bool)
                or not isinstance(weight, (int, float))
                or not math.isfinite(weight)
                or weight <= 0
            ):
                errors.append(f"coverage point {display_id} has an invalid weight")
                all_weights_valid = False
            else:
                valid_weights.append(float(weight))
            covered = point.get("covered")
            if type(covered) is not bool:
                errors.append(f"coverage point {display_id} covered must be a boolean")
            if covered is True:
                if isinstance(weight, (int, float)) and not isinstance(weight, bool) and math.isfinite(weight) and weight > 0:
                    covered_weights.append(float(weight))
                evidence_ids = point.get("evidence_ids")
                if not (
                    isinstance(evidence_ids, list)
                    and evidence_ids
                    and all(
                        isinstance(evidence_id, str)
                        and bool(evidence_id.strip())
                        and evidence_id == evidence_id.strip()
                        for evidence_id in evidence_ids
                    )
                ):
                    errors.append(f"covered coverage point {display_id} needs evidence")
                else:
                    for evidence_id in evidence_ids:
                        artifact = artifacts.get(evidence_id.strip().casefold())
                        if artifact is None:
                            errors.append(
                                f"covered coverage point {display_id} references unknown evidence_id: {evidence_id}"
                            )
                        elif artifact.get("kind") != "coverage_evidence":
                            errors.append(
                                f"covered coverage point {display_id} evidence_id must resolve to coverage_evidence"
                            )
        if all_weights_valid:
            total_weight = _finite_fsum(valid_weights)
            covered_weight = _finite_fsum(covered_weights)
            if total_weight is None or total_weight <= 0:
                errors.append("coverage total weight must be positive and finite")
            elif covered_weight is None:
                errors.append("coverage covered weight must be finite")
            else:
                coverage_ratio = covered_weight / total_weight
                if not math.isfinite(coverage_ratio):
                    errors.append("coverage ratio must be finite")

    if reviewer_ids is not None:
        for records, allowed, metric in (
            (claims, HALLUCINATION_LABELS, "hallucination"),
            (adaptations, ADAPTATION_LABELS, "adaptation"),
        ):
            kappa = _kappa_from_records(records, reviewer_ids, allowed)["value"]
            if kappa is None or kappa < KAPPA_THRESHOLD:
                errors.append(
                    f"{metric} kappa is below {KAPPA_THRESHOLD:.2f} or undefined"
                )
    return errors


def _metric_placeholder(threshold: float) -> dict:
    """Return a stable metric shape when frozen truth cannot be assessed."""
    return {
        "numerator": None,
        "denominator": None,
        "point_estimate": None,
        "wilson_interval": None,
        "bootstrap_interval": None,
        "interval": None,
        "threshold": threshold,
        "conservative_decision": False,
    }


def _kappa_placeholder() -> dict:
    return {
        "n": 0,
        "agreement": None,
        "value": None,
        "threshold": KAPPA_THRESHOLD,
        "decision": False,
    }


def _provenance(data: object) -> dict:
    """Keep only deterministic, input-supplied provenance fields in reports."""
    if not isinstance(data, dict):
        return {}
    provenance = data.get("provenance")
    result = {}
    if isinstance(provenance, dict):
        for key in ("repository_sha", "artifact_manifest_sha256"):
            if isinstance(provenance.get(key), str):
                result[key] = provenance[key]
    dataset = data.get("dataset")
    if isinstance(dataset, dict) and isinstance(dataset.get("dataset_id"), str):
        result["dataset_id"] = dataset["dataset_id"]
    if isinstance(data.get("frozen_at"), str):
        result["frozen_at"] = data["frozen_at"]
    seed = data.get("seed")
    if isinstance(seed, int) and not isinstance(seed, bool):
        result["seed"] = seed
    return result


def _not_assessable_scorecard(data: object, errors: list[str]) -> dict:
    return {
        "provenance": _provenance(data),
        "data_quality": {
            "decision": "not_assessable",
            "errors": errors,
            "thresholds": {
                "minimum_cases": 50,
                "minimum_profiles": 3,
                "minimum_assessable_share": 0.95,
                "minimum_common_pair_cases": 50,
                "minimum_common_pair_share": 0.95,
                "minimum_kappa": KAPPA_THRESHOLD,
            },
            "gates": _quality_gates(
                cases=None,
                profiles=None,
                claims_assessable_share=None,
                adaptations_assessable_share=None,
                claims_common_pair_cases=None,
                claims_common_pair_share=None,
                adaptations_common_pair_cases=None,
                adaptations_common_pair_share=None,
                hallucination_kappa=None,
                adaptation_kappa=None,
            ),
        },
        "kappa": {
            "hallucination": _kappa_placeholder(),
            "adaptation": _kappa_placeholder(),
        },
        "hallucination_rate": _metric_placeholder(HALLUCINATION_THRESHOLD),
        "adaptation_accuracy": _metric_placeholder(ADAPTATION_THRESHOLD),
        "coverage": {
            "numerator": None,
            "denominator": None,
            "point_estimate": None,
            "wilson_interval": None,
            "bootstrap_interval": None,
            "interval": None,
            "threshold": COVERAGE_THRESHOLD,
            "conservative_decision": False,
        },
        "assessable_share": {"claims": None, "adaptations": None},
        "common_pair": {
            "claims": {"records": None, "cases": None, "share": None},
            "adaptations": {"records": None, "cases": None, "share": None},
        },
        "limitations": errors + [
            "Human reviewer roster attestations are declarations; external signatures or records establish their real-world authenticity."
        ],
        "overall_status": "not_assessable",
    }


def _with_metric_decision(metric: dict, threshold: float, *, lower_is_better: bool) -> dict:
    result = dict(metric)
    result["threshold"] = threshold
    interval = result["interval"]
    result["conservative_decision"] = (
        interval[1] < threshold if lower_is_better else interval[0] >= threshold
    )
    return result


def build_scorecard(data: dict) -> dict:
    """Build a deterministic formal evidence gate from frozen truth data.

    Invalid or incomplete input returns a reportable ``not_assessable`` card so
    command-line consumers always have an auditable artifact to inspect.
    """
    errors = validate_truth(data)
    if errors:
        return _not_assessable_scorecard(data, errors)

    claims = data["claims"]
    adaptations = data["adaptations"]
    hallucination_records = [
        {"case_id": _canonical_case_id(record["case_id"]), "label": record["final_label"]}
        for record in claims
        if record["final_label"] != "unassessable"
    ]
    adaptation_records = [
        {"case_id": _canonical_case_id(record["case_id"]), "label": record["final_label"]}
        for record in adaptations
        if record["final_label"] != "unassessable"
    ]
    coverage = data.get("coverage_universe", [])
    covered_weight = math.fsum(point["weight"] for point in coverage if point["covered"])
    total_weight = math.fsum(point["weight"] for point in coverage)
    hallucination = _with_metric_decision(
        _metric(hallucination_records, {"yes"}, seed=data["seed"]),
        HALLUCINATION_THRESHOLD,
        lower_is_better=True,
    )
    adaptation = _with_metric_decision(
        _metric(adaptation_records, {"correct"}, seed=data["seed"]),
        ADAPTATION_THRESHOLD,
        lower_is_better=False,
    )
    coverage_metric = {
        "numerator": covered_weight,
        "denominator": total_weight,
        "point_estimate": covered_weight / total_weight if total_weight else 0.0,
        "wilson_interval": None,
        "bootstrap_interval": None,
        "interval": None,
        "threshold": COVERAGE_THRESHOLD,
    }
    coverage_metric["conservative_decision"] = (
        coverage_metric["point_estimate"] >= COVERAGE_THRESHOLD
    )
    hallucination_kappa = _kappa_from_records(
        claims,
        [_normalise_identity(reviewer) for reviewer in data["reviewers"]],
        HALLUCINATION_LABELS,
    )
    adaptation_kappa = _kappa_from_records(
        adaptations,
        [_normalise_identity(reviewer) for reviewer in data["reviewers"]],
        ADAPTATION_LABELS,
    )
    reviewer_ids = [_normalise_identity(reviewer) for reviewer in data["reviewers"]]
    claims_common_pair = _common_pair_stats(
        claims, reviewer_ids, HALLUCINATION_LABELS
    )
    adaptations_common_pair = _common_pair_stats(
        adaptations, reviewer_ids, ADAPTATION_LABELS
    )
    for kappa in (hallucination_kappa, adaptation_kappa):
        kappa["threshold"] = KAPPA_THRESHOLD
        kappa["decision"] = kappa["value"] is not None and kappa["value"] >= KAPPA_THRESHOLD

    passed = (
        hallucination["conservative_decision"]
        and adaptation["conservative_decision"]
        and coverage_metric["conservative_decision"]
        and hallucination_kappa["decision"]
        and adaptation_kappa["decision"]
    )
    return {
        "provenance": _provenance(data),
        "data_quality": {
            "decision": "pass",
            "errors": [],
            "thresholds": {
                "minimum_cases": 50,
                "minimum_profiles": 3,
                "minimum_assessable_share": 0.95,
                "minimum_common_pair_cases": 50,
                "minimum_common_pair_share": 0.95,
                "minimum_kappa": KAPPA_THRESHOLD,
            },
            "gates": _quality_gates(
                cases=len(data["dataset"]["cases"]),
                profiles=len(
                    _valid_dataset_profile_ids(data["dataset"]["profile_ids"], [])
                ),
                claims_assessable_share=(
                    sum(record["final_label"] != "unassessable" for record in claims)
                    / len(claims)
                    if claims
                    else 0.0
                ),
                adaptations_assessable_share=(
                    sum(record["final_label"] != "unassessable" for record in adaptations)
                    / len(adaptations)
                    if adaptations
                    else 0.0
                ),
                claims_common_pair_cases=claims_common_pair["cases"],
                claims_common_pair_share=claims_common_pair["share"],
                adaptations_common_pair_cases=adaptations_common_pair["cases"],
                adaptations_common_pair_share=adaptations_common_pair["share"],
                hallucination_kappa=hallucination_kappa["value"],
                adaptation_kappa=adaptation_kappa["value"],
            ),
        },
        "hallucination_rate": hallucination,
        "adaptation_accuracy": adaptation,
        "coverage": {
            **coverage_metric,
        },
        "kappa": {
            "hallucination": hallucination_kappa,
            "adaptation": adaptation_kappa,
        },
        "assessable_share": {
            "claims": (
                sum(record["final_label"] != "unassessable" for record in claims) / len(claims)
                if claims
                else 0.0
            ),
            "adaptations": (
                sum(record["final_label"] != "unassessable" for record in adaptations)
                / len(adaptations)
                if adaptations
                else 0.0
            ),
        },
        "common_pair": {
            "claims": claims_common_pair,
            "adaptations": adaptations_common_pair,
        },
        "limitations": [
            "Human reviewer roster attestations are declarations; external signatures or records establish their real-world authenticity."
        ],
        "overall_status": "pass" if passed else "fail",
    }


def _display(value: object) -> str:
    if value is None:
        return "not available"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def _interval_display(interval: object) -> str:
    if not isinstance(interval, (list, tuple)) or len(interval) != 2:
        return "not available"
    return f"[{_display(interval[0])}, {_display(interval[1])}]"


def render_scorecard_markdown(scorecard: dict) -> str:
    """Render a deterministic, operator-readable companion to scorecard JSON."""
    lines = [
        "# Formal Metric Scorecard",
        "",
        "This is an official metric evidence gate, not the jury's 100-point score.",
        "",
        f"Overall status: **{scorecard['overall_status']}**",
        "",
        "## Provenance",
        "",
        "```json",
        json.dumps(
            scorecard["provenance"],
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ),
        "```",
        "",
        "## Data quality gates",
        "",
        f"Decision: **{scorecard['data_quality']['decision']}**",
    ]
    for error in scorecard["data_quality"]["errors"]:
        lines.append(f"- {error}")
    for gate_name, gate in scorecard["data_quality"]["gates"].items():
        actual = (
            f"{float(gate['actual']):.6f}"
            if isinstance(gate["actual"], (int, float))
            else _display(gate["actual"])
        )
        lines.append(
            f"- {gate_name}: actual {actual}; threshold {gate['operator']} "
            f"{float(gate['threshold']):.6f}; decision: {gate['decision']}"
        )
    lines.extend(["", "## Metrics", ""])
    for title, key in (
        ("Hallucination rate", "hallucination_rate"),
        ("Adaptation accuracy", "adaptation_accuracy"),
        ("Core-knowledge coverage", "coverage"),
    ):
        metric = scorecard[key]
        lines.extend(
            [
                f"### {title}",
                "",
                f"- Numerator: {_display(metric['numerator'])}",
                f"- Denominator: {_display(metric['denominator'])}",
                f"- Point estimate: {_display(metric['point_estimate'])}",
                f"- Wilson 95% interval: {_interval_display(metric['wilson_interval'])}",
                f"- Cluster bootstrap 95% interval: {_interval_display(metric['bootstrap_interval'])}",
                f"- Conservative interval: {_interval_display(metric['interval'])}",
                f"- Threshold: {_display(metric['threshold'])}",
                f"- Conservative decision: {_display(metric['conservative_decision'])}",
                "",
            ]
        )
    lines.extend(["## Reviewer agreement", ""])
    for title, key in (("Claims", "hallucination"), ("Adaptations", "adaptation")):
        kappa = scorecard["kappa"][key]
        lines.extend(
            [
                f"### {title}",
                "",
                f"- Paired ratings (n): {_display(kappa['n'])}",
                f"- Raw agreement: {_display(kappa['agreement'])}",
                f"- Cohen's Kappa: {_display(kappa['value'])}",
                f"- Threshold: {_display(kappa['threshold'])}",
                f"- Decision: {_display(kappa['decision'])}",
                "",
            ]
        )
    lines.extend(["## Assessability", ""])
    for title, key in (("Claims", "claims"), ("Adaptations", "adaptations")):
        pair = scorecard["common_pair"][key]
        lines.extend(
            [
                f"- {title} assessable share: {_display(scorecard['assessable_share'][key])}",
                f"- {title} common-pair share: {_display(pair['share'])}",
                f"- {title} common-pair distinct cases: {_display(pair['cases'])}",
            ]
        )
    lines.append("")
    lines.extend(["## Limitations", ""])
    lines.extend(f"- {limitation}" for limitation in scorecard["limitations"])
    return "\n".join(lines) + "\n"


def _write_scorecard(output: Path, scorecard: dict) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "scorecard.json").write_text(
        json.dumps(
            scorecard,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (output / "scorecard.md").write_text(render_scorecard_markdown(scorecard), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Write JSON and Markdown evidence reports for a frozen truth file."""
    parser = argparse.ArgumentParser(description="Build a deterministic formal metric scorecard")
    parser.add_argument("--truth", required=True, type=Path, help="frozen formal-truth JSON")
    parser.add_argument("--out", required=True, type=Path, help="report output directory")
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.truth.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        scorecard = _not_assessable_scorecard({}, [f"truth file could not be read as JSON: {error}"])
    else:
        scorecard = build_scorecard(data)
    _write_scorecard(args.out, scorecard)
    return 0 if scorecard["overall_status"] in {"pass", "fail"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
