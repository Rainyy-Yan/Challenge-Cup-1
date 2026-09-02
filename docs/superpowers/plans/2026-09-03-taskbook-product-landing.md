# Taskbook Product Landing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing XH-202630 research prototype into a taskbook-traceable product candidate with a reproducible, independently labeled, mathematically conservative evidence scorecard.

**Architecture:** Keep the current learning workflow unchanged and add a separate formal-evidence pipeline. Human reviewers own truth labels and adjudication; deterministic Python owns schema validation, inter-rater reliability, point estimates, uncertainty intervals, threshold gates, and report rendering.

**Tech Stack:** Python 3.11 standard library, `unittest`, JSON, Markdown, existing evidence-index tooling.

**Spec:** `docs/superpowers/specs/2026-09-03-taskbook-product-landing-design.md`

## Global Constraints

- Official taskbook clauses are the source of truth; internal stretch targets must be labeled as internal.
- Formal evidence requires at least 50 differentiated test cases and at least 3 learner profiles.
- Official thresholds are hallucination rate `< 0.05`, adaptation accuracy `>= 0.85`, and core-knowledge coverage `>= 0.90`.
- Machine processing must never create human-review identities, labels, authorization conclusions, or `human_verified` status.
- Formal scoring is offline and deterministic; it must not read `.env` or call a model provider.
- A metric passes only on the conservative confidence-interval boundary, not the point estimate alone.
- Existing frontend behavior and model routing are out of scope unless a regression blocks the formal workflow.

---

### Task 1: Official-requirement delivery matrix

**Files:**
- Create: `docs/官方任务书落地矩阵.md`
- Modify: `delivery/evidence/index.json`
- Generated: `delivery/evidence/INDEX.md`
- Test: `tests/test_evidence_index.py`

**Interfaces:**
- Consumes: official taskbook clauses, current repository evidence index, Issues #65-#69.
- Produces: a row-level map from official clause to implementation, acceptance evidence, owner, gate, and current status.

- [ ] **Step 1: Write the matrix with separate official and internal columns**

Include every clause from taskbook pages 4-10. Use only `met`, `partial`, `missing`, and `external` as status values. Assign overall ownership to 肖云涵 and keep independent reviewer roles explicitly unassigned.

- [ ] **Step 2: Register the matrix as G0 evidence**

Add `EV-G0-TASKBOOK-MAP-001` to `delivery/evidence/index.json` with status `working`, owner `xyh202131`, related issue `65`, and a limitation that implementation evidence is current but external human sign-off and submission receipts are not.

- [ ] **Step 3: Regenerate and validate the evidence index**

Run:

```powershell
py -3 -X utf8 -m tools.evidence_index --write
py -3 -X utf8 -m tools.evidence_index --check
py -3 -X utf8 -m unittest tests.test_evidence_index -v
```

Expected: index check exits 0 and all evidence-index tests pass.

- [ ] **Step 4: Commit the requirement mapping**

```powershell
git add -- docs/官方任务书落地矩阵.md delivery/evidence/index.json delivery/evidence/INDEX.md
git commit -m "docs: map taskbook requirements to delivery evidence"
```

### Task 2: Formal truth contract and statistical core

**Files:**
- Create: `evalkit/formal_scorecard.py`
- Create: `tests/test_formal_scorecard.py`

**Interfaces:**
- Consumes: `dict` loaded from the version-1 formal-truth JSON contract.
- Produces: `validate_truth(data: dict) -> list[str]`, `wilson_interval(successes: int, total: int, confidence: float = 0.95) -> tuple[float, float]`, `cluster_bootstrap_interval(records: list[dict], positive: set[str], *, seed: int, samples: int) -> tuple[float, float]`, and `build_scorecard(data: dict) -> dict`.

- [ ] **Step 1: Write failing formula tests**

Use hand-derived literals to cover zero denominators, Wilson bounds, deterministic clustered resampling, exact weighted coverage, and conservative interval envelopes. A representative assertion is:

```python
self.assertAlmostEqual(wilson_interval(0, 100)[1], 0.036993, places=6)
```

- [ ] **Step 2: Run formula tests and confirm RED**

Run:

```powershell
py -3 -X utf8 -m unittest tests.test_formal_scorecard.TestIntervals -v
```

Expected: import or assertion failure because `evalkit.formal_scorecard` is not implemented.

- [ ] **Step 3: Implement minimal formula helpers**

Implement Wilson's score interval with `statistics.NormalDist().inv_cdf`, deterministic case-cluster bootstrap with `random.Random(seed)`, a linear-interpolated quantile helper, and a conservative envelope that takes the wider of Wilson and clustered-bootstrap bounds.

- [ ] **Step 4: Run formula tests and confirm GREEN**

Run the command from Step 2. Expected: all interval tests pass.

- [ ] **Step 5: Write failing truth-validation tests**

Cover fewer than 50 cases, fewer than 3 profiles, malformed SHA, duplicated IDs, reviewer identity collisions, exposed system conclusions, incomplete labels, unresolved disagreements, invalid coverage weights, missing evidence for covered points, low assessable share, and low/undefined Kappa.

- [ ] **Step 6: Run validation tests and confirm RED**

```powershell
py -3 -X utf8 -m unittest tests.test_formal_scorecard.TestTruthValidation -v
```

Expected: failures because the validation contract is incomplete.

- [ ] **Step 7: Implement strict validation and Kappa**

Reject invalid inputs with a complete list of stable error messages. Compute Kappa from the two named reviewer columns without using final adjudicated labels. Require every disagreement to name the distinct adjudicator.

- [ ] **Step 8: Run validation tests and confirm GREEN**

Run the command from Step 6. Expected: all validation tests pass.

- [ ] **Step 9: Commit the statistical core**

```powershell
git add -- evalkit/formal_scorecard.py tests/test_formal_scorecard.py
git commit -m "feat: add independent evidence scoring core"
```

### Task 3: Scorecard, CLI, and machine-readable truth template

**Files:**
- Modify: `evalkit/formal_scorecard.py`
- Modify: `tests/test_formal_scorecard.py`
- Create: `data/evaluation/formal_truth.template.json`
- Create: `data/evaluation/README.md`

**Interfaces:**
- Consumes: `python -m evalkit.formal_scorecard --truth <json> --out <directory>`.
- Produces: `<directory>/scorecard.json` and `<directory>/scorecard.md`; exit 0 only for an assessable report, exit 2 for invalid or incomplete truth.

- [ ] **Step 1: Write failing end-to-end scorecard tests**

Create temporary truth files in tests. Verify `pass`, `fail`, and `not_assessable` states; verify that a point estimate over the target still fails when its conservative lower bound misses; verify deterministic JSON and Markdown output.

- [ ] **Step 2: Run end-to-end tests and confirm RED**

```powershell
py -3 -X utf8 -m unittest tests.test_formal_scorecard.TestScorecard tests.test_formal_scorecard.TestCli -v
```

Expected: failures because report building and CLI output do not exist.

- [ ] **Step 3: Implement report building and rendering**

The JSON must include provenance, data-quality gates, Kappa, numerator, denominator, point estimate, 95% interval, threshold, conservative decision, limitations, and `overall_status`. The Markdown must render the same values and explicitly state that this is an evidence gate, not the jury's 100-point score.

- [ ] **Step 4: Add a non-claiming template and operator guide**

The template must contain no invented human names or conclusions. Its status stays `draft`, and the guide must explain freezing, blinding, adjudication, hashing, scoring, and evidence registration.

- [ ] **Step 5: Run end-to-end tests and confirm GREEN**

Run the command from Step 2. Expected: all scorecard and CLI tests pass.

- [ ] **Step 6: Commit the runnable scorecard**

```powershell
git add -- evalkit/formal_scorecard.py tests/test_formal_scorecard.py data/evaluation/formal_truth.template.json data/evaluation/README.md
git commit -m "feat: produce reproducible formal metric scorecards"
```

### Task 4: Product handoff and acceptance evidence

**Files:**
- Create: `docs/官方指标数学口径与验收.md`
- Create: `docs/产品落地实施与验收计划.md`
- Modify: `README.md`
- Modify: `delivery/evidence/index.json`
- Generated: `delivery/evidence/INDEX.md`

**Interfaces:**
- Consumes: the implemented scorecard CLI and the taskbook-delivery matrix.
- Produces: one operator-facing workflow from deployment through UAT and formal evidence generation.

- [ ] **Step 1: Document formulas and non-negotiable evidence boundaries**

Copy the implemented function names, labels, formulas, confidence method, thresholds, and invalid-input behavior exactly. Include a worked example clearly marked as illustrative, not a competition result.

- [ ] **Step 2: Document milestones, dependencies, resources, UAT, and decision log**

Include the M0-M6 timeline, the critical path, overall owner 肖云涵, independent reviewer/adjudicator role constraints, five G0-G4 gates, rollback rules, and the exact evidence produced at each milestone.

- [ ] **Step 3: Update README commands and project status**

Replace stale “骨架版” language with “可运行研究原型”, retain limitations, and add the formal scorecard workflow without claiming that the empty template proves official metrics.

- [ ] **Step 4: Register formal-evaluation artifacts**

Add working/planned G2 entries for the truth contract, scorecard implementation, and future approved scorecard. Regenerate the evidence index and run its validation command.

- [ ] **Step 5: Verify documentation consistency**

Run:

```powershell
rg -n "骨架版|适配准确率达到 100%|human_verified" README.md docs data/evaluation delivery/evidence
py -3 -X utf8 -m tools.evidence_index --check
git diff --check
```

Expected: any remaining uses are historical warnings or explicit prohibitions, not current product claims; index and whitespace checks pass.

- [ ] **Step 6: Commit the handoff package**

```powershell
git add -- README.md docs/官方指标数学口径与验收.md docs/产品落地实施与验收计划.md delivery/evidence/index.json delivery/evidence/INDEX.md
git commit -m "docs: add taskbook delivery and acceptance workflow"
```

### Task 5: Full verification, review, and PR

**Files:**
- Review: every file changed since `origin/main`.
- Update only if verification exposes a real defect.

**Interfaces:**
- Consumes: the complete branch diff.
- Produces: a verified branch and a PR linked to Issues #65 and #67.

- [ ] **Step 1: Run focused tests**

```powershell
py -3 -X utf8 -m unittest tests.test_formal_scorecard tests.test_evidence_index -v
```

- [ ] **Step 2: Run complete Python and frontend suites in offline mode**

```powershell
$env:AGENTEDU_MINIMAX_API_KEY=''
$env:AGENTEDU_DEEPSEEK_API_KEY=''
$env:AGENTEDU_API_KEY=''
py -3 -X utf8 -m unittest discover -s tests
node --test tests/frontend-ui.test.mjs
```

- [ ] **Step 3: Run syntax, security, and diff checks**

```powershell
py -3 -m compileall -q evalkit tools tests
node --check web/app.js
git diff --check origin/main...HEAD
git status --short
```

Inspect tracked changes for credentials, personal data, generated caches, stale report claims, and unplanned files.

- [ ] **Step 4: Obtain code review and resolve findings**

Review formula correctness, statistical claims, validation fail-closed behavior, CLI exit semantics, documentation consistency, and test quality. Re-run every affected focused test after a fix.

- [ ] **Step 5: Push and create the PR**

```powershell
git push -u origin feat/taskbook-evidence-scorecard
gh pr create --base main --head feat/taskbook-evidence-scorecard
```

The PR body must summarize the requirement gap, formulas, evidence limitations, commands and results; include `Refs #65` and `Refs #67`. Do not claim the formal metrics pass until real reviewers fill and adjudicate the truth file.
