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

## Score and register evidence

Only set `status` to `frozen` once every required field and label is complete.
Then run the offline, deterministic command:

```powershell
py -3 -X utf8 -m evalkit.formal_scorecard --truth <formal_truth.json> --out <report-directory>
```

The command always writes `scorecard.json` and `scorecard.md` for readable JSON
input. It exits `0` for assessable evidence (`pass` or `fail`) and `2` for
invalid or incomplete evidence (`not_assessable`). Register the frozen truth
file, its hash, the two generated reports, command version, reviewer assignment
records, and any adjudication log in the project evidence index. The scorecard
is an official metric evidence gate, not the jury's 100-point score.
