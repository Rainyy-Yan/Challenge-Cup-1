# Learner-First Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dense three-column result dashboard with a single learner-facing flow, prevent reasoning-text leakage, and give every submitted self-test a visible next action.

**Architecture:** Keep `server.py` as the only application entry and preserve all existing JSON APIs. Move the browser presentation into a semantic HTML shell, a dedicated stylesheet, a pure view-model helper that is testable with Node, and a DOM controller; keep detailed orchestration and provenance in collapsed evidence sections. Sanitize model narrative in Python before serialization and again at the display boundary.

**Tech Stack:** Python 3.11 standard library, HTML5, CSS, browser-native JavaScript, Node 24 built-in test runner, `unittest`.

**Spec:** `docs/superpowers/specs/2026-09-02-learner-first-frontend-design.md`

## Global Constraints

- The only supported runtime remains `server.py -> web/index.html`.
- Do not add a frontend framework, package manager, remote font, login system, database, or build step.
- Do not change BKT, retrieval, debate, audit, model routing, or the existing API request/response shapes.
- Evidence remains available but orchestration, full provenance, and source scoring are collapsed by default.
- Desktop content uses one reading column with a maximum width of approximately 960px; mobile keeps the same semantic order without horizontal overflow.
- New behavior follows a failing-test, minimal-implementation, passing-test cycle.

---

### Task 1: Sanitize Diagnostic Narrative Before Serialization

**Files:**
- Modify: `agents/examiner.py:382-408`
- Test: `tests/test_examiner.py` in `TestAnalyzeAndSynthesize`

**Interfaces:**
- Consumes: raw `str` returned by `self.llm.run` and `patterns: list[str]`.
- Produces: `_clean_narrative(raw: str, patterns: list[str]) -> str`, used by `ExaminerAgent.synthesize()`.

- [ ] **Step 1: Write failing narrative-cleaning tests**

Add direct behavioral cases to `TestAnalyzeAndSynthesize`:

```python
def test_reasoning_text_is_removed_from_narrative(self):
    from agents.examiner import _clean_narrative

    raw = "<think>internal chain of thought</think>\n建议先复习安全规程。"
    self.assertEqual(_clean_narrative(raw, []), "建议先复习安全规程。")

def test_unclosed_reasoning_uses_rule_based_fallback(self):
    from agents.examiner import _clean_narrative

    cleaned = _clean_narrative("<think>internal chain of thought", ["安全题连续答错"])
    self.assertEqual(cleaned, "安全题连续答错。")
    self.assertNotIn("think", cleaned.lower())

def test_empty_model_narrative_has_readable_fallback(self):
    from agents.examiner import _clean_narrative

    self.assertEqual(
        _clean_narrative("```text\n\n```", []),
        "已根据你的实际作答生成学习建议，请按推荐顺序开始学习。",
    )
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
python -m unittest tests.test_examiner.TestAnalyzeAndSynthesize -v
```

Expected: the three new tests fail because `_clean_narrative` is not defined.

- [ ] **Step 3: Implement the narrative boundary**

Add the helper near the synthesis section and call it from `synthesize()`:

```python
_THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.IGNORECASE | re.DOTALL)
_UNCLOSED_THINK = re.compile(r"<think\b[^>]*>.*$", re.IGNORECASE | re.DOTALL)


def _clean_narrative(raw: str, patterns: list[str]) -> str:
    text = _THINK_BLOCK.sub("", raw or "")
    text = _UNCLOSED_THINK.sub("", text).strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    if text:
        return text
    if patterns:
        return "；".join(pattern.rstrip("。") for pattern in patterns[:3]) + "。"
    return "已根据你的实际作答生成学习建议，请按推荐顺序开始学习。"
```

Change the return value in `synthesize()` to:

```python
return {"narrative": _clean_narrative(raw, patterns), "patterns": patterns}
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_examiner.TestAnalyzeAndSynthesize -v
```

Expected: all synthesis tests pass and no test output contains leaked reasoning text.

- [ ] **Step 5: Commit the backend boundary**

```powershell
git add -- agents/examiner.py tests/test_examiner.py
git commit -m "fix: hide model reasoning from learner reports"
```

---

### Task 2: Add Testable Frontend View-Model Decisions

**Files:**
- Create: `web/view-model.js`
- Create: `tests/frontend-ui.test.mjs`

**Interfaces:**
- Consumes: narrative strings, `resources: Resource[]`, `path: string[]`, current knowledge-point ID, and `decision.action`.
- Produces: global `AgentEduView` with `cleanDisplayText(raw)`, `resourcesForKp(resources, kp)`, and `feedbackNextAction(path, currentKp, decision)`.

- [ ] **Step 1: Write failing Node tests for the pure decisions**

Create `tests/frontend-ui.test.mjs`:

```javascript
import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const sandbox = {};
vm.createContext(sandbox);
const source = fs.existsSync('web/view-model.js')
  ? fs.readFileSync('web/view-model.js', 'utf8')
  : '';
vm.runInContext(source, sandbox);
const view = sandbox.AgentEduView || {};
const required = name => {
  assert.equal(typeof view[name], 'function', `${name} must be exported`);
  return view[name];
};

test('cleanDisplayText removes complete and unclosed reasoning blocks', () => {
  const cleanDisplayText = required('cleanDisplayText');
  assert.equal(cleanDisplayText('<think>secret</think>\n先学习安全规程。'), '先学习安全规程。');
  assert.equal(cleanDisplayText('<think>secret'), '');
});

test('resourcesForKp returns only the selected learning unit', () => {
  const resources = [{kp: 'KP-01'}, {kp: 'KP-02'}, {kp: 'KP-01'}];
  const resourcesForKp = required('resourcesForKp');
  assert.deepEqual(
    JSON.parse(JSON.stringify(resourcesForKp(resources, 'KP-01'))),
    [{kp: 'KP-01'}, {kp: 'KP-01'}],
  );
});

test('feedbackNextAction keeps remediation on the current unit', () => {
  const feedbackNextAction = required('feedbackNextAction');
  assert.deepEqual(
    JSON.parse(JSON.stringify(feedbackNextAction(['KP-01', 'KP-02'], 'KP-01', {action: 'explain_down'}))),
    {kind: 'repeat', label: '重新学习本知识点', targetKp: 'KP-01'},
  );
});

test('feedbackNextAction advances to the next unit after success', () => {
  const feedbackNextAction = required('feedbackNextAction');
  assert.deepEqual(
    JSON.parse(JSON.stringify(feedbackNextAction(['KP-01', 'KP-02'], 'KP-01', {action: 'advance'}))),
    {kind: 'next', label: '继续下一知识点', targetKp: 'KP-02'},
  );
});
```

- [ ] **Step 2: Run the Node tests and verify RED**

Run:

```powershell
node --test tests/frontend-ui.test.mjs
```

Expected: FAIL because `web/view-model.js` does not exist.

- [ ] **Step 3: Implement the pure browser helpers**

Create `web/view-model.js` as a dependency-free script:

```javascript
(function exposeViewModel(root){
  function cleanDisplayText(raw){
    return String(raw || '')
      .replace(/<think\b[^>]*>[\s\S]*?<\/think\s*>/gi, '')
      .replace(/<think\b[^>]*>[\s\S]*$/gi, '')
      .replace(/^```[a-zA-Z]*\s*/i, '')
      .replace(/\s*```$/i, '')
      .trim();
  }

  function resourcesForKp(resources, kp){
    return (resources || []).filter(resource => resource.kp === kp);
  }

  function feedbackNextAction(path, currentKp, decision){
    if(decision && decision.action === 'advance'){
      const next = path[path.indexOf(currentKp) + 1];
      if(next) return {kind:'next', label:'继续下一知识点', targetKp:next};
      return {kind:'summary', label:'查看更新后的学习建议', targetKp:null};
    }
    return {kind:'repeat', label:'重新学习本知识点', targetKp:currentKp};
  }

  root.AgentEduView = Object.freeze({cleanDisplayText, resourcesForKp, feedbackNextAction});
})(typeof window === 'undefined' ? globalThis : window);
```

- [ ] **Step 4: Run the Node tests and verify GREEN**

Run:

```powershell
node --check web/view-model.js
node --test tests/frontend-ui.test.mjs
```

Expected: syntax check and four tests pass.

- [ ] **Step 5: Commit the view model**

```powershell
git add -- web/view-model.js tests/frontend-ui.test.mjs
git commit -m "test: define learner workflow decisions"
```

---

### Task 3: Replace the Three-Column Dashboard With a Semantic Reading Flow

**Files:**
- Create: `web/styles.css`
- Create: `web/app.js`
- Modify: `web/index.html:7-885`
- Modify: `tests/test_server.py` in `TestOnlineFrontend`

**Interfaces:**
- Consumes: existing endpoints `/api/profiles`, `/api/intake`, `/api/interview/*`, `/api/run`, `/api/feedback`, and `/api/materials/stage` without shape changes.
- Produces: ordered landmarks `workflowProgress`, `learnerRecord`, `learningPlan`, `learningPath`, `learningContent`, `feedbackCard`, `decisionPanel`, and `evidencePanel`.

- [ ] **Step 1: Write a failing semantic landmark test**

Add an `HTMLParser` helper to `tests/test_server.py` and verify consumer-visible document order:

```python
from html.parser import HTMLParser


class _LandmarkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(values["id"])


def test_learner_flow_landmarks_follow_the_task_order(self):
    parser = _LandmarkParser()
    parser.feed(Path("web/index.html").read_text(encoding="utf-8"))
    expected = [
        "workflowProgress", "learnerRecord", "learningPlan", "learningPath",
        "learningContent", "feedbackCard", "decisionPanel", "evidencePanel",
    ]
    self.assertEqual([item for item in parser.ids if item in expected], expected)

def test_source_links_are_rendered_from_verified_provenance_text(self):
    path = Path("web/app.js")
    controller = path.read_text(encoding="utf-8") if path.exists() else ""

    self.assertIn("function sourceWithLink", controller)
    self.assertIn("sourceWithLink(kb.source)", controller)
```

- [ ] **Step 2: Run the landmark test and verify RED**

Run:

```powershell
python -m unittest tests.test_server.TestOnlineFrontend.test_learner_flow_landmarks_follow_the_task_order -v
```

Expected: FAIL because the current page uses the old dashboard IDs and order.

- [ ] **Step 3: Build the semantic HTML shell**

Replace inline CSS and JavaScript in `web/index.html` with local assets and ordered content:

```html
<link rel="stylesheet" href="/styles.css">
<main class="learning-shell">
  <nav id="workflowProgress" class="workflow-progress" aria-label="学习进度"></nav>
  <details id="learnerRecord" class="learner-record" open></details>
  <section id="learningPlan" class="result-section" hidden></section>
  <nav id="learningPath" class="learning-path" aria-label="建议学习顺序" hidden></nav>
  <section id="learningContent" class="result-section" hidden></section>
  <section id="feedbackCard" class="result-section" hidden></section>
  <section id="decisionPanel" class="decision-panel" tabindex="-1" hidden></section>
  <details id="evidencePanel" class="evidence-panel" hidden></details>
  <div id="uiAlert" class="ui-alert" role="alert" hidden></div>
</main>
<script src="/view-model.js"></script>
<script src="/app.js"></script>
```

Preserve all existing intake, interview, example-profile, material-staging, chart, resource, timeline, and feedback element IDs used by `web/app.js`. Remove the three-column `.grid` wrapper and place every learner-facing section in the landmark order asserted above.

- [ ] **Step 4: Implement the industrial-manual stylesheet**

Create `web/styles.css` with the approved visual system and single-column layout:

```css
:root{
  --paper:#f4f1e9; --surface:#fffdf8; --ink:#18232c; --muted:#65717a;
  --line:#d8d3c8; --accent:#c75821; --accent-soft:#f7e5d9;
  --success:#236b4b; --danger:#a33a32; --technical:#315d73;
  --sans:"Source Han Sans SC","Microsoft YaHei",sans-serif;
  --serif:"Source Han Serif SC","Songti SC","SimSun",serif;
  --mono:"Cascadia Mono","Consolas",monospace;
}
.learning-shell{width:min(100% - 32px,960px);margin:0 auto;padding:28px 0 72px}
.result-section,.learner-record,.evidence-panel{margin-top:18px;border:1px solid var(--line);background:var(--surface)}
.resource-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
.resource-body{overflow-wrap:anywhere}
@media(max-width:760px){
  .resource-grid{grid-template-columns:1fr}
  .learning-shell{width:min(100% - 24px,960px);padding-top:18px}
}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;animation:none!important}}
```

Use typographic hierarchy, numbered section labels, generous vertical spacing, visible focus states, `role="alert"` error blocks, and text labels in addition to status colors.

- [ ] **Step 5: Move the existing controller into `web/app.js`**

Move the current API and intake/interview/material functions without changing their endpoint contracts. Initialize only after all three scripts load:

```javascript
'use strict';
const view = window.AgentEduView;
const $ = selector => document.querySelector(selector);
let activeKp = null;

function boot(){
  bindEntryMode();
  bindActions();
  updateWorkflowProgress('intake');
  loadProfiles();
}

boot();
```

The new controller must set `learningPlan`, `learningPath`, `learningContent`, `feedbackCard`, `decisionPanel`, and `evidencePanel` from the same session object already returned by the server. Move the timeline and three existing SVG charts inside `evidencePanel`, so the evidence remains available without competing with the learning task.

When a plan has been generated, collapse the intake record and advance the visible progress state:

```javascript
function revealLearningPlan(){
  $('#learnerRecord').open = false;
  $('#learningPlan').hidden = false;
  $('#learningPath').hidden = false;
  $('#learningContent').hidden = false;
  $('#feedbackCard').hidden = false;
  $('#evidencePanel').hidden = false;
  updateWorkflowProgress('learn');
  $('#learningPlan').scrollIntoView({block:'start', behavior:reduceMotion() ? 'auto' : 'smooth'});
}
```

- [ ] **Step 6: Run focused static and syntax checks and verify GREEN**

Run:

```powershell
python -m unittest tests.test_server.TestOnlineFrontend -v
node --check web/view-model.js
node --check web/app.js
```

Expected: landmark order and existing provenance-link contract pass; both scripts parse.

- [ ] **Step 7: Commit the semantic shell**

```powershell
git add -- web/index.html web/styles.css web/app.js tests/test_server.py
git commit -m "feat: reorganize the learner workspace"
```

---

### Task 4: Focus Resources and Make Feedback Continue the Learning Path

**Files:**
- Modify: `web/app.js`
- Modify: `web/styles.css`
- Modify: `tests/frontend-ui.test.mjs`

**Interfaces:**
- Consumes: `AgentEduView.resourcesForKp`, `AgentEduView.feedbackNextAction`, `S.path`, `S.path_names`, `S.resources`, and `S.decision`.
- Produces: `selectLearningUnit(kp)`, `renderLearningContent(kp)`, and `renderDecision(decision, answers)` with synchronized resource and quiz state.

- [ ] **Step 1: Add failing edge-case tests for the final and hold branches**

Append to `tests/frontend-ui.test.mjs`:

```javascript
test('feedbackNextAction finishes when there is no following unit', () => {
  assert.deepEqual(
    JSON.parse(JSON.stringify(view.feedbackNextAction(['KP-01'], 'KP-01', {action: 'advance'}))),
    {kind: 'summary', label: '查看更新后的学习建议', targetKp: null},
  );
});

test('feedbackNextAction keeps consolidation on the current unit', () => {
  assert.deepEqual(
    JSON.parse(JSON.stringify(view.feedbackNextAction(['KP-01', 'KP-02'], 'KP-01', {action: 'consolidate'}))),
    {kind: 'repeat', label: '重新学习本知识点', targetKp: 'KP-01'},
  );
});
```

- [ ] **Step 2: Temporarily mutate the helper and verify the tests detect the regression**

Change the `consolidate` return to `{kind:'next', label:'继续下一知识点', targetKp:'KP-02'}`, run:

```powershell
node --test tests/frontend-ui.test.mjs
```

Expected: the consolidation test fails. Restore the correct helper and rerun until all tests pass.

- [ ] **Step 3: Implement knowledge-unit navigation and focused resources**

In `web/app.js`, render the path as buttons and show only resources for the active knowledge point:

```javascript
function selectLearningUnit(kp){
  activeKp = kp;
  document.querySelectorAll('[data-learning-kp]').forEach(button => {
    button.setAttribute('aria-current', button.dataset.learningKp === kp ? 'step' : 'false');
  });
  renderLearningContent(kp);
  buildQuiz(kp);
}

function renderLearningContent(kp){
  const resources = view.resourcesForKp(S.resources, kp);
  $('#resources').innerHTML = resources.map(resourceCard).join('');
  $('#learningContent').hidden = false;
}
```

Render lecture, case, and quiz cards in a `.resource-grid`. Each card shows a short type label, title, difficulty, and collapsed body. Keep “核验与溯源” nested and closed by default, and keep URLs behind `sourceWithLink()`.

- [ ] **Step 4: Implement the visible decision and next action**

After `/api/feedback` returns, calculate the hand-checked score already present in `answers`, then render the decision:

```javascript
function renderDecision(decision, answers){
  const correct = answers.filter(Boolean).length;
  const next = view.feedbackNextAction(S.path, activeKp, decision);
  $('#decisionPanel').innerHTML = `
    <p class="section-kicker">本轮结果</p>
    <h2>答对 ${correct} / ${answers.length} 题</h2>
    <p>${esc(decision.reason)}</p>
    <button class="btn" id="continueLearning" type="button">${esc(next.label)}</button>`;
  $('#decisionPanel').hidden = false;
  $('#decisionPanel').focus({preventScroll:true});
  $('#decisionPanel').scrollIntoView({block:'center', behavior: reduceMotion() ? 'auto' : 'smooth'});
  $('#continueLearning').onclick = () => continueFromDecision(next);
}
```

`continueFromDecision(next)` selects `next.targetKp` for `next`, scrolls back to the current resource for `repeat`, and scrolls to `learningPlan` for `summary`. Clear the old answer picks whenever the active knowledge point changes.

- [ ] **Step 5: Run frontend tests and Python frontend contract tests**

Run:

```powershell
node --check web/app.js
node --test tests/frontend-ui.test.mjs
python -m unittest tests.test_server.TestOnlineFrontend -v
```

Expected: all frontend behavior tests and HTML contracts pass.

- [ ] **Step 6: Commit the learning continuation flow**

```powershell
git add -- web/app.js web/styles.css tests/frontend-ui.test.mjs
git commit -m "feat: guide learners after self assessment"
```

---

### Task 5: Wire CI and Verify the Live Experience

**Files:**
- Modify: `.github/workflows/ci.yml` in the existing `frontend` and `smoke` jobs
- Modify: `docs/superpowers/specs/2026-09-02-learner-first-frontend-design.md` status line

**Interfaces:**
- Consumes: the final HTML, CSS, JavaScript, and unchanged Python service.
- Produces: repeatable CI checks plus visual evidence at 1280px and 390px widths.

- [ ] **Step 1: Add Node verification to the existing frontend CI job**

Use the repository's previously pinned Node action and exact commands:

```yaml
- name: Set up Node.js
  uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0
  with:
    node-version: "24"

- name: Verify learner frontend
  run: |
    node --check web/view-model.js
    node --check web/app.js
    node --test tests/frontend-ui.test.mjs
    python -m unittest tests.test_server.TestOnlineFrontend -v
```

Extend the smoke job after the root request:

```bash
curl --fail --silent http://127.0.0.1:8765/styles.css >/dev/null
curl --fail --silent http://127.0.0.1:8765/view-model.js >/dev/null
curl --fail --silent http://127.0.0.1:8765/app.js >/dev/null
```

- [ ] **Step 2: Run the complete local verification suite**

Run:

```powershell
$env:AGENTEDU_MINIMAX_API_KEY = ' '
$env:AGENTEDU_DEEPSEEK_API_KEY = ' '
python -m unittest discover -s tests -v
python -m compileall -q agents core evalkit tools cli.py config.py orchestrator.py server.py
node --check web/view-model.js
node --check web/app.js
node --test tests/frontend-ui.test.mjs
git diff --check
```

Expected: 404 Python tests pass after three narrative cases and one landmark-order case, all six Node tests pass, compilation succeeds, and the diff check reports no errors.

- [ ] **Step 3: Verify the running page at desktop width**

Start a separate local verification server on port 8766 with both model-key environment variables set to one blank space. This forces deterministic `MockLLM` behavior without changing `.env`, spending provider quota, or interrupting the user's real-model service on port 8000. Open `http://127.0.0.1:8766/` at 1280×900, choose 学习者A, generate the plan, and verify:

- the result uses one main reading column;
- the diagnostic narrative contains no `<think>` text;
- learning path buttons switch the visible resources and quiz together;
- orchestration and provenance are closed until explicitly expanded;
- long source URLs do not widen the page.

- [ ] **Step 4: Verify feedback continuation and mobile width**

Answer all four self-test questions, submit, and verify the result panel receives focus and shows score, reason, and one primary next-action button. Repeat at 390×844 and verify `document.documentElement.scrollWidth <= window.innerWidth`, no clipped button, and the same landmark order.

- [ ] **Step 5: Mark the design implemented and commit CI evidence**

Change the design status line to `状态：已实现并完成本地验收`, then run `git diff --check` and commit:

```powershell
git add -- .github/workflows/ci.yml docs/superpowers/specs/2026-09-02-learner-first-frontend-design.md
git commit -m "ci: verify the learner-first frontend"
```

- [ ] **Step 6: Request code review before push or PR creation**

Provide the reviewer with the spec, this plan, the commit range beginning at `4acca7a`, the full test results, and desktop/mobile screenshots. Resolve every critical or important issue, rerun the complete verification suite, and only then ask the user for push/PR authorization.
