# Formal evaluation evidence workflow

`formal_truth.template.json` is a non-claiming draft contract. It contains no
reviewer names, human conclusions, adjudications, or passing result. Copy it to
a separately controlled truth file; the empty template is intentionally
`not_assessable` when processed.

## Freeze before scoring

Freeze the case list, learner profiles, seed, coverage universe, and the system
outputs before reviewers receive their materials. Record
`provenance.repository_sha` and `provenance.artifact_manifest_sha256` (do not
add a root-level commit field) plus cryptographic hashes for frozen input and output
artifacts in the evidence register. Root `version` and `artifact_manifest.version`
must be exact integer `1`; Boolean values are rejected. Do not replace a frozen
artifact in place: make a new dataset identifier and preserve its predecessor.

The version-1 `artifact_manifest` binds the truth rows to actual frozen UTF-8
content. Artifact records use exactly `id`, `kind`, `subject_id`, `content`,
`sha256`, `citation_ids`, and `review_status`; `claim_output` and
`resource_output` additionally require `case_id`, while all other kinds forbid
`case_id`. Citation records use exactly `id`, `source_id`, `locator`, `excerpt`,
`sha256`, and `review_status`. Allowed artifact kinds are `profile_snapshot`,
`case_input`, `claim_output`, `resource_output`, and `coverage_evidence`.

Ownership is bidirectional rather than an ID-only declaration:

- each profile maps to its own `profile_snapshot` whose `subject_id` is that
  profile ID; at least three distinct artifacts and distinct profile content
  hashes are required;
- each case owns a non-reused `case_input` whose `subject_id` is the case ID;
  the 50 required cases must also produce 50 distinct
  `(profile_snapshot.sha256, case_input.sha256)` pairs;
- each claim/resource row owns a non-reused output artifact whose `subject_id`
  is the row ID and whose `case_id` matches the row's canonical case ID;
- each coverage artifact's `subject_id` is the knowledge-point ID it supports,
  so evidence for one point cannot be relabelled as evidence for another.

For every artifact kind, the manifest ID set must exactly equal the set of IDs
referenced by the dataset, rows, or coverage universe. Orphan artifacts and
extra artifacts competing for an existing subject are rejected. Coverage may
reference multiple evidence artifacts for one knowledge point, but every
manifested coverage artifact must appear in at least one coverage row.

Profile-snapshot content hashes must be unique. Claim and resource content may
legitimately repeat across different owned subjects, so those hashes are not
globally unique; ownership and the closed ID sets prevent row inflation.
Case-input hashes may repeat across different profiles, while the required 50
unique `(profile_snapshot.sha256, case_input.sha256)` pairs prevent duplicate
cases under the same profile. Claim, resource, and coverage artifacts must cite
at least one approved citation. A covered knowledge point requires at least one
valid evidence ID; an uncovered point must use exactly `"evidence_ids": []`.

Every citation has exactly `id`, `source_id`, `locator`, `excerpt`, `sha256`,
and `review_status`. Its digest binds the UTF-8 excerpt; an artifact digest binds
the UTF-8 content. Generate either content digest without adding a newline:

```powershell
py -3 -X utf8 -c "import hashlib; print(hashlib.sha256('exact content'.encode('utf-8')).hexdigest())"
```

After the artifacts and citations are final, compute the manifest digest from
canonical JSON (`sort_keys=True`, compact separators, and `ensure_ascii=False`)
and store it as `provenance.artifact_manifest_sha256`. This runnable command
uses the same canonicalization as the scorer (replace the placeholder path):

```powershell
py -3 -X utf8 -c "import hashlib,json,pathlib; d=json.loads(pathlib.Path('manifest.json').read_text(encoding='utf-8')); c=json.dumps(d,sort_keys=True,separators=(',',':'),ensure_ascii=False); print(hashlib.sha256(c.encode('utf-8')).hexdigest())"
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

For each of claims and adaptations, rows must cover at least 50 distinct cases
and represent at least three profiles. At least 95% of all rows must have two
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
the source is malformed JSON, invalid UTF-8, or contains a JSON-escaped lone
surrogate. All object keys and string values must encode as strict UTF-8 before
hashing begins. It exits `0` for assessable evidence (`pass` or `fail`) and `2`
for invalid or incomplete evidence (`not_assessable`). Register the frozen truth
file, its hash, the two generated reports, command version, reviewer assignment
records, and any adjudication log in the project evidence index. The scorecard
is an official metric evidence gate, not the jury's 100-point score.

Manifest and hash validation proves only internal content/reference binding; it
does not prove the real-world authenticity of sources, excerpts, signatures,
or reviewer identities. Keep signatures, assignment records,
conflict-of-interest checks, and source originals in the controlled evidence
register; the scorecard must not claim those facts itself.
