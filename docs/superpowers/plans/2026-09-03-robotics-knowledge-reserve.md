# Robotics Knowledge Reserve Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Append 94 traceable industrial-robotics knowledge records as a non-Demo reserve without replacing the current corpus or claiming human verification.

**Architecture:** Keep the existing 26 records and Demo manifest unchanged, append only `KB-027` through `KB-120` with `demo_eligible=false`, and harden the existing audit algorithm around source-state parsing and subject-specific conflict detection. Store provenance in a focused research ledger and evidence cards while excluding raw scrape caches.

**Tech Stack:** Python 3.11 standard library, JSONL, Markdown, `unittest`, existing BM25 retriever and audit tool.

**Spec:** `docs/superpowers/specs/2026-09-03-robotics-knowledge-reserve-design.md`

## Global Constraints

- Preserve `KB-001` through `KB-026` exactly as they exist on the implementation branch baseline.
- Every new record has `origin="sourced"`, `verified=false`, and `demo_eligible=false`.
- Machine review never promotes a record to `verified=true` or adds it to the Demo manifest.
- Do not copy `.firecrawl` caches, vendor PDFs, candidate model code, reports, or unrelated dirty-worktree files.
- Use only the configured `xyh <246811510+xyh202131@users.noreply.github.com>` Git identity and never add AI authorship metadata.

---

### Task 1: Accept Explicit Pending-Review Source Markers

**Files:**
- Modify: `tests/test_pipeline.py`
- Modify: `tools/kb_audit.py`

**Interfaces:**
- Consumes: `audit(chunks, kps) -> AuditReport` and `Chunk.source`.
- Produces: source-state detection that accepts both `待核实` and `待人工核实` as explicit non-verified states.

- [ ] **Step 1: Write the failing source-state test**

Add a test that constructs an unverified manual chunk whose source begins with `【待人工核实】` and asserts that `audit()` does not emit `FAKESOURCE`. The regression catches removal of the explicit pending-review marker handling.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `py -3 -X utf8 -m unittest tests.test_pipeline.TestCrossValidation.test_explicit_manual_review_marker_is_not_fake_source -v`

Expected: FAIL because the current substring check recognizes `待核实` but not `待人工核实`.

- [ ] **Step 3: Implement the minimal source-state predicate**

Add a small helper in `tools/kb_audit.py` that returns true for either approved marker or for a traceable `｜sha:` source. Use it in the `FAKESOURCE` rule without changing `verified`.

- [ ] **Step 4: Run the focused test and current audit tests**

Run: `py -3 -X utf8 -m unittest tests.test_pipeline.TestCrossValidation -v`

Expected: PASS.

### Task 2: Prevent Product-Model False Conflicts

**Files:**
- Modify: `tests/test_pipeline.py`
- Modify: `tools/kb_audit.py`

**Interfaces:**
- Consumes: subjects extracted from chunk text and existing `jaccard_like` token similarity.
- Produces: normalized numeric predicates and explicit definition extraction used by `audit()`.

- [ ] **Step 1: Write independent failing conflict tests**

Add literal fixtures proving that two independent `UR30 (SW5.20)` facts produce no conflict, while two different values for the same `UR30` payload metric do produce `NUMCONFLICT`. Add a definition fixture proving that two meanings for the same `SRVO-005` code produce `DEFCONFLICT`.

- [ ] **Step 2: Run the three focused tests and verify RED**

Run the three new `TestCrossValidation` methods with `py -3 -X utf8 -m unittest ... -v`.

Expected: the independent-facts fixture fails because the current whole-sentence/model grouping creates a false conflict; the positive conflict fixtures characterize behavior that must remain detectable.

- [ ] **Step 3: Implement predicate-aware comparison**

In `tools/kb_audit.py`, normalize model suffixes, quantities, unit aliases, range endpoints and upper/lower bounds. Compare numeric values only when the normalized object-and-metric key matches. Extract definitions only when a definition marker immediately follows the subject, and emit definition differences as warnings.

- [ ] **Step 4: Run the conflict suite and verify GREEN**

Run: `py -3 -X utf8 -m unittest tests.test_pipeline.TestCrossValidation -v`

Expected: PASS with both false-positive prevention and true-conflict detection covered.

### Task 3: Append the Non-Demo Knowledge Reserve

**Files:**
- Create: `tests/test_kb_reserve.py`
- Modify: `data/kb/robotics.jsonl`

**Interfaces:**
- Consumes: `Retriever.from_jsonl(path, demo_only=False|True)` and the knowledge-point JSON schema.
- Produces: a 120-record runtime corpus while retaining the 20-record formal Demo view.

- [ ] **Step 1: Write the failing reserve contract tests**

Assert literal expectations: 120 total IDs, continuous `KB-001` through `KB-120`, exactly 94 new records, every new record sourced/unverified/Demo-disabled, all new knowledge points valid, and the formal Demo ID set unchanged at the 20 IDs listed by `data/demo_source_manifest.json`.

- [ ] **Step 2: Run the reserve tests and verify RED**

Run: `py -3 -X utf8 -m unittest tests.test_kb_reserve -v`

Expected: FAIL because only 26 records exist.

- [ ] **Step 3: Append the reviewed candidate records**

Append only candidate IDs `KB-027` through `KB-120` to `data/kb/robotics.jsonl`. Add `demo_eligible:false` to every appended object without altering its ID, knowledge point, text, REF, URL, locator, SHA, origin or verification flag.

- [ ] **Step 4: Run reserve and Demo-manifest tests**

Run: `py -3 -X utf8 -m unittest tests.test_kb_reserve tests.test_demo_sources tests.test_demo_items -v`

Expected: PASS; ordinary corpus count 120 and Demo count 20.

### Task 4: Deliver Traceable Source Evidence

**Files:**
- Create: `docs/research/来源台账.md`
- Create: `docs/research/evidence-cards/README.md`
- Create: the evidence cards directly referenced by `REF-ROB-004`, `REF-ROB-007` through `REF-ROB-010`, `REF-ROB-012`, and `REF-ROB-014` through `REF-ROB-028`
- Modify: `tests/test_kb_reserve.py`

**Interfaces:**
- Consumes: source strings from `KB-027` through `KB-120`.
- Produces: a repository-visible REF and SHA trail without redistributing vendor source files.

- [ ] **Step 1: Extend the reserve test with provenance validation and verify RED**

Parse REF IDs and 64-character hashes from every new source. Assert every REF appears in `来源台账.md` and every hash appears in at least one evidence card. Run the focused test and confirm it fails because the research documents are absent.

- [ ] **Step 2: Copy only the required ledger and evidence-card content**

Create the ledger and required cards from the reviewed candidate documents. Add the OSHA evidence-card hash and the two newer Universal Robots page hashes while retaining older hashes as historical snapshots. Do not copy raw cache files or manuals.

- [ ] **Step 3: Run the provenance and corpus tests**

Run: `py -3 -X utf8 -m unittest tests.test_kb_reserve -v`

Expected: PASS with no missing REF IDs or hashes.

### Task 5: Verify, Review and Publish

**Files:**
- Modify if generated by the validated command: `data/audit/audit.json`
- Review all files changed by Tasks 1 through 4.

**Interfaces:**
- Consumes: completed corpus, audit rules, tests and provenance docs.
- Produces: one focused commit and a pull request against `qiyuankaiwu/Challenge-Cup:main`.

- [ ] **Step 1: Run the complete verification gate**

Run:

```powershell
py -3 -X utf8 -m tools.kb_audit
py -3 -X utf8 -m unittest discover -s tests
git diff --check
```

Expected: audit 0 errors, 120 total records, full suite 0 failures, and clean diff check.

- [ ] **Step 2: Review the exact diff and metadata**

Inspect `git status --short`, `git diff --stat`, `git diff`, `git diff --cached`, and `git log --oneline -10`. Confirm no credentials, scrape caches, vendor binaries, unrelated candidate files or modifications to the original 26 JSON lines.

- [ ] **Step 3: Create a focused commit**

Stage explicit paths only and commit with a short Conventional Commit message such as `feat: add reviewed robotics knowledge reserve`. Do not amend and do not add co-author metadata.

- [ ] **Step 4: Push and open the pull request**

Fetch and verify the remote branch state, push normally to the authorized fork branch, then create a PR against `qiyuankaiwu/Challenge-Cup:main`. The PR body must state that all 94 records remain unverified and Demo-disabled, list the verification commands, and link or create the corresponding issue without merging it.
