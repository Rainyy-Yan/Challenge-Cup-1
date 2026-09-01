# Task 8 report: staged review and finding validation

## Summary

Implemented the deterministic advisory service and staged qykw review engine.  The
implementation uses the existing strict prompt builders and provider capability
boundary, parses only exact structured responses, validates local commentable
lines before applying the finding limit, deterministically deduplicates and
fingerprints findings, and fails closed without exposing provider or prompt data.

## TDD evidence

Exact RED command:

```powershell
py -3 -m unittest tests.test_qykw_advisory tests.test_qykw_review -v
```

RED result: 11 expected `ModuleNotFoundError` errors for the missing
`tools.qykw.advisory` and `tools.qykw.review` modules.  A second RED cycle for
malformed-triage short-circuiting and explicit validation/limitation notes failed
with the expected two assertions before the minimal implementation change.

## Verification evidence

| Scope | Command | Result |
| --- | --- | --- |
| Focused | `py -3 -m unittest tests.test_qykw_advisory tests.test_qykw_review -v` | 13 passed |
| qykw | `py -3 -m unittest discover -s tests -p 'test_qykw*.py' -v` | 143 passed |
| Full | `py -3 -m unittest discover -s tests -v` | 441 passed |
| Compile | `py -3 -m compileall -q tools/qykw` | passed |
| Diff | `git diff --check`; `git show --check --stat --oneline HEAD` | passed |

## Changed files

- `tools/qykw/advisory.py`
- `tools/qykw/review.py`
- `tests/test_qykw_advisory.py`
- `tests/test_qykw_review.py`

## Commit and identity

- Commit: `d85a3971b49e7bb279ab8dc68aa65eb096cc5f9b feat: validate qykw review findings`
- Author: `xyh <246811510+xyh202131@users.noreply.github.com>`
- Committer: `xyh <246811510+xyh202131@users.noreply.github.com>`
- No push performed.

## Risks

This task intentionally covers only deterministic advisory/review behavior. State
storage, event orchestration, and publishing integration remain owned by their
separate tasks; no networked provider was invoked by these tests.

## Review fix pass 1

Two review blockers were corrected with a strict RED/GREEN cycle.  RED command:

```powershell
py -3 -m unittest tests.test_qykw_prompts tests.test_qykw_review -v
```

The initial run produced five expected failures and two expected errors: review
requests shared an idempotency key, triage still exposed finding candidates,
validation had no batch identity, and unbound candidates could reach review
flow.  The final focused run was 37 passed; including the provider schema
fixture was 66 passed.

Changes: each deep-review request derives a replay-stable per-chunk key;
validation derives a replay-stable candidate-batch key; triage has a
priorities-only schema; deep candidates require their actual source chunk ID;
the engine validates full plan/chunk provenance, chunk-local paths, and local
commentable lines before validation/publication.  Validation payloads carry the
bound source chunk IDs.  `tests/test_qykw_provider.py` now generates the
minimal valid response from the active strict triage schema, preserving generic
provider-boundary coverage without changing production provider code.

Verification: qykw 151 passed; full suite 449 passed; `py -3 -m compileall -q
tools/qykw` passed; `git diff --check` passed.  No network or push was used.
