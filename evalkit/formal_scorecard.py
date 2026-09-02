"""Offline statistical helpers for independently adjudicated formal truth."""

from __future__ import annotations

import math
import random
import re
from datetime import datetime
from statistics import NormalDist


HALLUCINATION_LABELS = {"yes", "no", "unassessable"}
ADAPTATION_LABELS = {"correct", "incorrect", "unassessable"}
MACHINE_OWNED_IDENTITIES = {"ai", "auto", "bot", "codex", "llm", "machine", "model", "system"}


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


def _is_machine_owned(identity: object) -> bool:
    normalised = _normalise_identity(identity)
    if normalised is None:
        return False
    tokens = re.findall(r"[a-z0-9]+", normalised)
    return any(token in MACHINE_OWNED_IDENTITIES for token in tokens)


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
    if not all(value in allowed for value in values):
        return None
    return values  # type: ignore[return-value]


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
    case_ids: set[str],
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
        if not isinstance(case_id, str) or not case_id or case_id not in case_ids:
            errors.append(f"{singular} {display_id} references an unknown case_id")
        if prohibited.intersection(record):
            errors.append(f"{singular} {display_id} exposes a prohibited system conclusion")

        if reviewers is None:
            accepted.append(record)
            continue
        values = _reviewer_values(record.get("labels"), reviewers, allowed)
        if values is None:
            errors.append(f"{singular} {display_id} has incomplete reviewer labels")

        final_label = record.get("final_label")
        if final_label not in allowed:
            errors.append(f"{singular} {display_id} has an invalid final_label")
        if values is None or final_label not in allowed:
            continue
        adjudicator_present = "adjudicator_id" in record
        adjudicator = _normalise_identity(record.get("adjudicator_id"))
        if adjudicator_present and adjudicator is None:
            errors.append(f"{singular} {display_id} adjudicator must be a non-empty identity")
        if values[0] == values[1]:
            if final_label != values[0]:
                errors.append(f"{singular} {display_id} final_label must equal the common reviewer label")
        else:
            if adjudicator is None:
                if not adjudicator_present:
                    errors.append(f"{singular} {display_id} has unresolved reviewer disagreement")
            elif adjudicator in reviewers:
                errors.append(f"{singular} {display_id} adjudicator must be distinct from both reviewers")
            elif _is_machine_owned(record.get("adjudicator_id")):
                errors.append(f"{singular} {display_id} adjudicator must not be machine-owned")
        accepted.append(record)
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
    elif any(_is_machine_owned(reviewer) for reviewer in raw_reviewers):
        errors.append("reviewer identities must not be machine-owned")
        reviewer_ids = None
    else:
        reviewer_ids = [reviewer for reviewer in reviewers if reviewer is not None]

    cases = data.get("cases")
    if not isinstance(cases, list):
        return errors + ["cases must be a list"]
    if len(cases) < 50:
        errors.append("at least 50 cases are required")

    profiles = {
        case.get("profile_id")
        for case in cases
        if isinstance(case, dict) and isinstance(case.get("profile_id"), str) and case["profile_id"]
    }
    if len(profiles) < 3:
        errors.append("at least 3 profiles are required")

    case_ids: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"case {index} must be an object")
            continue
        case_id = case.get("id")
        display_id = case_id if isinstance(case_id, str) and case_id else str(index)
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"case {index} needs a non-empty id")
        elif case_id in case_ids:
            errors.append(f"duplicate case id: {case_id}")
        else:
            case_ids.add(case_id)

    claims = _validate_records(
        data, "claims", HALLUCINATION_LABELS, reviewer_ids, case_ids, errors
    )
    adaptations = _validate_records(
        data, "adaptations", ADAPTATION_LABELS, reviewer_ids, case_ids, errors
    )
    all_records = claims + adaptations
    assessable_records = sum(record.get("final_label") != "unassessable" for record in all_records)
    if all_records and assessable_records / len(all_records) < 0.90:
        errors.append("assessable share is below 0.90")

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
            elif point_id in coverage_ids:
                errors.append(f"duplicate coverage kp_id: {point_id}")
            else:
                coverage_ids.add(point_id)
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
                    and all(isinstance(evidence_id, str) and evidence_id for evidence_id in evidence_ids)
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


def build_scorecard(data: dict) -> dict:
    """Build deterministic measurements from adjudicated, human-owned labels."""
    errors = validate_truth(data)
    if errors:
        raise ValueError("invalid formal truth: " + "; ".join(errors))

    claims = data["claims"]
    adaptations = data["adaptations"]
    hallucination_records = [
        {"case_id": record["case_id"], "label": record["final_label"]}
        for record in claims
        if record["final_label"] != "unassessable"
    ]
    adaptation_records = [
        {"case_id": record["case_id"], "label": record["final_label"]}
        for record in adaptations
        if record["final_label"] != "unassessable"
    ]
    coverage = data.get("coverage_universe", [])
    covered_weight = sum(point["weight"] for point in coverage if point["covered"])
    total_weight = sum(point["weight"] for point in coverage)
    return {
        "hallucination_rate": _metric(hallucination_records, {"yes"}, seed=data["seed"]),
        "adaptation_accuracy": _metric(adaptation_records, {"correct"}, seed=data["seed"]),
        "coverage": {
            "numerator": covered_weight,
            "denominator": total_weight,
            "point_estimate": covered_weight / total_weight if total_weight else 0.0,
        },
        "kappa": {
            "hallucination": _kappa_from_records(
                claims,
                [_normalise_identity(reviewer) for reviewer in data["reviewers"]],
                HALLUCINATION_LABELS,
            ),
            "adaptation": _kappa_from_records(
                adaptations,
                [_normalise_identity(reviewer) for reviewer in data["reviewers"]],
                ADAPTATION_LABELS,
            ),
        },
        "assessable_share": (
            sum(
                record["final_label"] != "unassessable"
                for record in claims + adaptations
            )
            / len(claims + adaptations)
            if claims or adaptations
            else 0.0
        ),
    }
