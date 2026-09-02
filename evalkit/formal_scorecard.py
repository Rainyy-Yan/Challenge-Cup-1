"""Offline statistical helpers for independently adjudicated formal truth."""

from __future__ import annotations

import math
import random
import re
from statistics import NormalDist


HALLUCINATION_LABELS = {"yes", "no", "unassessable"}
ADAPTATION_LABELS = {"correct", "incorrect", "unassessable"}
MACHINE_OWNED_IDENTITIES = {"ai", "agent", "machine", "model", "system", "codex"}


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


def _metric(records: list[dict], positive: set[str]) -> dict:
    successes = sum(record["label"] in positive for record in records)
    total = len(records)
    wilson = wilson_interval(successes, total)
    bootstrap = cluster_bootstrap_interval(records, positive, seed=20260903, samples=2000)
    return {
        "numerator": successes,
        "denominator": total,
        "point_estimate": successes / total if total else 0.0,
        "wilson_interval": wilson,
        "bootstrap_interval": bootstrap,
        "interval": (min(wilson[0], bootstrap[0]), max(wilson[1], bootstrap[1])),
    }


def _is_machine_owned(identity: object) -> bool:
    return isinstance(identity, str) and identity.strip().casefold() in MACHINE_OWNED_IDENTITIES


def _complete_labels(labels: object, allowed: dict[str, set[str]]) -> bool:
    return (
        isinstance(labels, dict)
        and set(labels) == set(allowed)
        and all(labels[name] in values for name, values in allowed.items())
    )


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


def _kappa_from_cases(cases: list[dict], reviewers: list[str], metric: str) -> dict:
    left: list[str] = []
    right: list[str] = []
    allowed = HALLUCINATION_LABELS if metric == "hallucination" else ADAPTATION_LABELS
    for case in cases:
        labels = case.get("labels") if isinstance(case, dict) else None
        if not isinstance(labels, dict):
            continue
        first = labels.get(reviewers[0])
        second = labels.get(reviewers[1])
        if (
            isinstance(first, dict)
            and isinstance(second, dict)
            and first.get(metric) in allowed
            and second.get(metric) in allowed
        ):
            left.append(first[metric])
            right.append(second[metric])
    return _cohen_kappa(left, right)


def validate_truth(data: dict) -> list[str]:
    """Return every non-cascading violation of the version-1 truth contract."""
    if not isinstance(data, dict):
        return ["truth must be an object"]

    errors: list[str] = []
    if data.get("version") != 1:
        errors.append("truth version must be 1")
    if data.get("status") != "frozen":
        errors.append("truth status must be frozen")

    provenance = data.get("provenance")
    sha = provenance.get("repository_sha") if isinstance(provenance, dict) else None
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        errors.append("provenance.repository_sha must be a 40-character lowercase hexadecimal SHA")

    reviewers = data.get("reviewers")
    reviewers_valid = (
        isinstance(reviewers, list)
        and len(reviewers) == 2
        and all(isinstance(reviewer, str) and reviewer.strip() for reviewer in reviewers)
        and len(set(reviewers)) == 2
    )
    if not reviewers_valid:
        errors.append("reviewers must contain exactly two distinct non-empty identities")
    elif any(_is_machine_owned(reviewer) for reviewer in reviewers):
        errors.append("reviewer identities must not be machine-owned")
        reviewers_valid = False

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
    assessable_cases = 0
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

        prohibited = {
            "system_conclusion",
            "machine_conclusion",
            "model_conclusion",
            "system_label",
            "machine_label",
            "model_label",
        }
        if prohibited.intersection(case):
            errors.append(f"case {display_id} exposes a prohibited system conclusion")

        adjudicated = case.get("adjudicated_labels")
        adjudicated_complete = _complete_labels(
            adjudicated,
            {
                "hallucination": HALLUCINATION_LABELS,
                "adaptation": ADAPTATION_LABELS,
            },
        )
        if not adjudicated_complete:
            errors.append(f"case {display_id} has incomplete adjudicated labels")
        elif (
            adjudicated["hallucination"] != "unassessable"
            and adjudicated["adaptation"] != "unassessable"
        ):
            assessable_cases += 1

        if not reviewers_valid:
            continue
        reviewer_labels = case.get("labels")
        columns_complete = isinstance(reviewer_labels, dict) and set(reviewer_labels) == set(reviewers)
        if columns_complete:
            columns_complete = all(
                _complete_labels(
                    reviewer_labels[reviewer],
                    {
                        "hallucination": HALLUCINATION_LABELS,
                        "adaptation": ADAPTATION_LABELS,
                    },
                )
                for reviewer in reviewers
            )
        if not columns_complete:
            errors.append(f"case {display_id} has incomplete reviewer labels")
            continue

        first = reviewer_labels[reviewers[0]]
        second = reviewer_labels[reviewers[1]]
        disagreement = any(first[metric] != second[metric] for metric in ("hallucination", "adaptation"))
        if not disagreement:
            continue
        adjudicator = case.get("adjudicator_id")
        if not isinstance(adjudicator, str) or not adjudicator.strip():
            errors.append(f"case {display_id} has unresolved reviewer disagreement")
        elif adjudicator in reviewers:
            errors.append(f"case {display_id} adjudicator must be distinct from both reviewers")
        elif _is_machine_owned(adjudicator):
            errors.append(f"case {display_id} adjudicator must not be machine-owned")

    if cases and assessable_cases / len(cases) < 0.90:
        errors.append("assessable share is below 0.90")

    coverage = data.get("coverage_universe")
    if not isinstance(coverage, list):
        errors.append("coverage_universe must be a list")
    else:
        valid_weight_total = 0.0
        for index, point in enumerate(coverage):
            if not isinstance(point, dict):
                errors.append(f"coverage point {index} must be an object")
                continue
            point_id = point.get("id")
            display_id = point_id if isinstance(point_id, str) and point_id else str(index)
            weight = point.get("weight")
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight <= 0:
                errors.append(f"coverage point {display_id} has an invalid weight")
            else:
                valid_weight_total += weight
            if point.get("covered") is True:
                evidence_ids = point.get("evidence_ids")
                if not (
                    isinstance(evidence_ids, list)
                    and evidence_ids
                    and all(isinstance(evidence_id, str) and evidence_id for evidence_id in evidence_ids)
                ):
                    errors.append(f"covered coverage point {display_id} needs evidence")
        if valid_weight_total <= 0:
            errors.append("coverage universe needs positive total weight")

    if reviewers_valid:
        for metric in ("hallucination", "adaptation"):
            kappa = _kappa_from_cases(cases, reviewers, metric)["value"]
            if kappa is None or kappa < 0.60:
                errors.append(f"{metric} kappa is below 0.60 or undefined")
    return errors


def build_scorecard(data: dict) -> dict:
    """Build deterministic measurements from adjudicated, human-owned labels."""
    errors = validate_truth(data)
    if errors:
        raise ValueError("invalid formal truth: " + "; ".join(errors))

    cases = data.get("cases", [])
    hallucination_records = [
        {"case_id": case["id"], "label": case["adjudicated_labels"]["hallucination"]}
        for case in cases
    ]
    adaptation_records = [
        {"case_id": case["id"], "label": case["adjudicated_labels"]["adaptation"]}
        for case in cases
    ]
    coverage = data.get("coverage_universe", [])
    covered_weight = sum(point["weight"] for point in coverage if point["covered"])
    total_weight = sum(point["weight"] for point in coverage)
    return {
        "hallucination_rate": _metric(hallucination_records, {"yes"}),
        "adaptation_accuracy": _metric(adaptation_records, {"correct"}),
        "coverage": {
            "numerator": covered_weight,
            "denominator": total_weight,
            "point_estimate": covered_weight / total_weight if total_weight else 0.0,
        },
        "kappa": {
            "hallucination": _kappa_from_cases(cases, data["reviewers"], "hallucination"),
            "adaptation": _kappa_from_cases(cases, data["reviewers"], "adaptation"),
        },
    }
