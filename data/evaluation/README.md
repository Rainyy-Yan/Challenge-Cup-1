# Formal evaluation evidence workflow

`formal_truth.template.json` is a non-claiming draft contract. It contains no
reviewer names, human conclusions, adjudications, or passing result. Copy it to
a separately controlled truth file; the empty template is intentionally
`not_assessable` when processed.

## Freeze before scoring

Freeze the case list, learner profiles, system commit, seed, coverage universe,
and the system outputs before reviewers receive their materials. Record the
repository Git SHA and cryptographic hashes for frozen input and output artifacts
in the evidence register. Do not replace a frozen artifact in place: make a new
dataset identifier and preserve its predecessor.

The version-1 `artifact_manifest` binds the truth rows to actual frozen UTF-8
content. Every artifact has exactly `id`, `kind`, `content`, `sha256`,
`citation_ids`, and `review_status`. Allowed kinds are `profile_snapshot`,
`case_input`, `claim_output`, `resource_output`, and `coverage_evidence`.
Profiles and cases point to frozen profile/case artifacts; claim and adaptation
rows point to frozen output artifacts. A covered knowledge point points to an
approved `coverage_evidence` artifact. Claim, resource, and coverage artifacts
must cite at least one approved citation.

Every citation has exactly `id`, `source_id`, `locator`, `excerpt`, `sha256`,
and `review_status`. Its digest binds the UTF-8 excerpt; an artifact digest binds
the UTF-8 content. Generate either content digest without adding a newline:

```powershell
py -3 -X utf8 -c "import hashlib; print(hashlib.sha256('exact content'.encode('utf-8')).hexdigest())"
```

After the artifacts and citations are final, compute the manifest digest from
canonical JSON (`sort_keys=True`, compact separators, and `ensure_ascii=False`)
and store it as `provenance.artifact_manifest_sha256`:

```python
canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

Freeze first, then hash, then assign reviewers. Any later content, citation, or
status change creates a new manifest hash and requires a new frozen evaluation
version; never repair a signed batch in place.

## Blind and independent review

Each claim and adaptation receives independent labels from exactly two
reviewers who cannot see system conclusions or each other's labels. The
`human_reviewer_roster` is a declaration contract: each filled entry must have
a stable `id` and `attested_human: true`. This declaration does not establish a
person's real-world identity; retain external signatures, assignment records,
and conflict-of-interest evidence separately.

When the two labels disagree, a third, distinct rostered adjudicator records
the final label through `adjudicated_by`. Never use a reviewer as their own
adjudicator. Mark genuinely undecidable material as `unassessable` rather than
inventing a conclusion.

For each of claims and adaptations, at least 95% of all rows must have two
non-`unassessable` reviewer labels, and those common valid pairs must span at
least 50 distinct cases. Cohen's Kappa is computed only from those pairs. The
separate final-label assessable-share gate also remains 95%.

## Score and register evidence

Only set `status` to `frozen` once every required field and label is complete.
Then run the offline, deterministic command:

```powershell
py -3 -X utf8 -m evalkit.formal_scorecard --truth <formal_truth.json> --out <report-directory>
```

The command always writes `scorecard.json` and `scorecard.md`, including when
the source is malformed JSON or invalid UTF-8. It exits `0` for assessable
evidence (`pass` or `fail`) and `2` for invalid or incomplete evidence
(`not_assessable`). Register the frozen truth
file, its hash, the two generated reports, command version, reviewer assignment
records, and any adjudication log in the project evidence index. The scorecard
is an official metric evidence gate, not the jury's 100-point score.

The schema checks declarations, hashes, references, reviewer labels, and signed
workflow boundaries. It does not prove that a source, reviewer identity,
signature, or quoted excerpt is externally authentic. Keep signatures,
assignment records, conflict-of-interest checks, and source originals in the
controlled evidence register; the scorecard must not claim those facts itself.
