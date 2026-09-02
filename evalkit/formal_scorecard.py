"""Offline statistical helpers for independently adjudicated formal truth."""

from __future__ import annotations

import argparse
import json
import math
import random
import re
from datetime import datetime
from pathlib import Path
from statistics import NormalDist


HALLUCINATION_LABELS = {"yes", "no", "unassessable"}
ADAPTATION_LABELS = {"correct", "incorrect", "unassessable"}
HALLUCINATION_THRESHOLD = 0.05
ADAPTATION_THRESHOLD = 0.85
COVERAGE_THRESHOLD = 0.90
KAPPA_THRESHOLD = 0.60


def _quality_gates(
    *,
    cases: int | None,
    profiles: int | None,
    claims_assessable_share: float | None,
    adaptations_assessable_share: float | None,
    hallucination_kappa: float | None,
    adaptation_kappa: float | None,
) -> dict:
    """Return explicit actual/threshold decisions for every quality gate."""
    gates = {
        "cases": (cases, 50),
        "profiles": (profiles, 3),
        "claims_assessable_share": (claims_assessable_share, 0.95),
        "adaptations_assessable_share": (adaptations_assessable_share, 0.95),
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
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            errors.append(f"{singular} {index} must be an object")
            continue
        display_id = _record_error_prefix(singular, record, index)
        record_id = record.get("id")
        if not isinstance(record_id, str) or not record_id:
            errors.append(f"{singular} {index} needs a non-empty id")
        elif record_id in record_ids:
            errors.append(f"duplicate {singular} id: {record_id}")
        else:
            record_ids.add(record_id)

        case_id = record.get("case_id")
        canonical_case_id = _canonical_case_id(case_id)
        if not isinstance(case_id, str) or canonical_case_id is None:
            errors.append(f"{singular} {display_id} references an unknown case_id")
        elif case_id != case_id.strip():
            errors.append(f"{singular} {display_id} case_id must equal its stripped form")
        elif canonical_case_id not in case_profiles:
            errors.append(f"{singular} {display_id} references an unknown case_id")
        if prohibited.intersection(record):
            errors.append(f"{singular} {display_id} exposes a prohibited system conclusion")
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
    sha = provenance.get("repository_sha") if isinstance(provenance, dict) else None
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        errors.append("provenance.repository_sha must be a 40-character lowercase hexadecimal SHA")

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
    valid_profile_ids: set[str] = set()
    if isinstance(profile_ids, list):
        for profile_id in profile_ids:
            if not isinstance(profile_id, str) or not profile_id.strip():
                continue
            canonical_profile_id = profile_id.strip().casefold()
            if canonical_profile_id in valid_profile_ids:
                errors.append(f"duplicate dataset profile_id: {profile_id}")
            else:
                valid_profile_ids.add(canonical_profile_id)
    if len(valid_profile_ids) < 3:
        errors.append("dataset.profile_ids must contain at least 3 distinct non-empty values")
    profile_universe_valid = len(valid_profile_ids) >= 3

    case_profiles: dict[str, str] = {}
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"case {index} must be an object")
            continue
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
        case_profiles[canonical_case_id] = canonical_profile_id

    claims = _validate_records(
        data,
        "claims",
        HALLUCINATION_LABELS,
        reviewer_ids,
        human_roster,
        case_profiles,
        errors,
    )
    adaptations = _validate_records(
        data,
        "adaptations",
        ADAPTATION_LABELS,
        reviewer_ids,
        human_roster,
        case_profiles,
        errors,
    )
    for key, records in (("claims", claims), ("adaptations", adaptations)):
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

    coverage = data.get("coverage_universe")
    if not isinstance(coverage, list):
        errors.append("coverage_universe must be a list")
    else:
        valid_weight_total = 0.0
        coverage_ids: set[str] = set()
        for index, point in enumerate(coverage):
            if not isinstance(point, dict):
                errors.append(f"coverage point {index} must be an object")
                continue
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
            else:
                valid_weight_total += weight
            covered = point.get("covered")
            if type(covered) is not bool:
                errors.append(f"coverage point {display_id} covered must be a boolean")
            if covered is True:
                evidence_ids = point.get("evidence_ids")
                if not (
                    isinstance(evidence_ids, list)
                    and evidence_ids
                    and all(
                        isinstance(evidence_id, str) and evidence_id.strip()
                        for evidence_id in evidence_ids
                    )
                ):
                    errors.append(f"covered coverage point {display_id} needs evidence")
        if valid_weight_total <= 0:
            errors.append("coverage universe needs positive total weight")

    if reviewer_ids is not None:
        for records, allowed, metric in (
            (claims, HALLUCINATION_LABELS, "hallucination"),
            (adaptations, ADAPTATION_LABELS, "adaptation"),
        ):
            kappa = _kappa_from_records(records, reviewer_ids, allowed)["value"]
            if kappa is None or kappa < 0.60:
                errors.append(f"{metric} kappa is below 0.60 or undefined")
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
    result = dict(provenance) if isinstance(provenance, dict) else {}
    dataset = data.get("dataset")
    if isinstance(dataset, dict) and "dataset_id" in dataset:
        result["dataset_id"] = dataset["dataset_id"]
    for key in ("frozen_at", "seed"):
        if key in data:
            result[key] = data[key]
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
                "minimum_kappa": KAPPA_THRESHOLD,
            },
            "gates": _quality_gates(
                cases=None,
                profiles=None,
                claims_assessable_share=None,
                adaptations_assessable_share=None,
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
            "interval": None,
            "threshold": COVERAGE_THRESHOLD,
            "conservative_decision": False,
        },
        "assessable_share": {"claims": None, "adaptations": None},
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
    covered_weight = sum(point["weight"] for point in coverage if point["covered"])
    total_weight = sum(point["weight"] for point in coverage)
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
                "minimum_kappa": KAPPA_THRESHOLD,
            },
            "gates": _quality_gates(
                cases=len(data["dataset"]["cases"]),
                profiles=len(data["dataset"]["profile_ids"]),
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
        json.dumps(scorecard["provenance"], ensure_ascii=False, sort_keys=True, indent=2),
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
                f"- 95% interval: {_interval_display(metric['interval'])}",
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
                f"- Cohen's Kappa: {_display(kappa['value'])}",
                f"- Threshold: {_display(kappa['threshold'])}",
                f"- Decision: {_display(kappa['decision'])}",
                "",
            ]
        )
    lines.extend(["## Limitations", ""])
    lines.extend(f"- {limitation}" for limitation in scorecard["limitations"])
    return "\n".join(lines) + "\n"


def _write_scorecard(output: Path, scorecard: dict) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "scorecard.json").write_text(
        json.dumps(scorecard, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
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
    except (OSError, json.JSONDecodeError) as error:
        scorecard = _not_assessable_scorecard({}, [f"truth file could not be read as JSON: {error}"])
    else:
        scorecard = build_scorecard(data)
    _write_scorecard(args.out, scorecard)
    return 0 if scorecard["overall_status"] in {"pass", "fail"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
