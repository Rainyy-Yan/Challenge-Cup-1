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

const appSandbox = {window: {AgentEduView: view}};
vm.createContext(appSandbox);
const appSource = fs.existsSync('web/app.js')
  ? fs.readFileSync('web/app.js', 'utf8')
  : '';
vm.runInContext(appSource, appSandbox);
const app = appSandbox.AgentEduApp || {};
const requiredApp = name => {
  assert.equal(typeof app[name], 'function', `${name} must be exported`);
  return app[name];
};

test('resourceBody preserves headings, evidence, numbered steps, and bullets', () => {
  const resourceBody = requiredApp('resourceBody');
  const html = resourceBody({
    kind: 'guide',
    body: '# 安全准备\n## 操作步骤\n> 依据 KB-01\n1. 检查急停\n- 记录结果',
  });
  assert.match(html, /<h3>安全准备<\/h3>/);
  assert.match(html, /<h4>操作步骤<\/h4>/);
  assert.match(html, /class="rsource">依据 KB-01/);
  assert.match(html, /class="rpoint">1\. 检查急停/);
  assert.match(html, /class="rbullet">记录结果/);
});

test('chart models retain scales, learner fit, and path completion semantics', () => {
  const fitChartModel = requiredApp('fitChartModel');
  const pathChartModel = requiredApp('pathChartModel');
  const session = {
    resources: [{kp: 'KP-01', difficulty: 3}, {kp: 'KP-01', difficulty: 4}],
    diagnosis: {mastery: [{kp: 'KP-01', score: 0.5}]},
    path: ['KP-01'], path_names: ['安全规程'], kp_index: { 'KP-01': {name: '安全规程'} },
  };
  assert.deepEqual(JSON.parse(JSON.stringify(fitChartModel(session).levels)), [1, 2, 3, 4, 5]);
  assert.deepEqual(JSON.parse(JSON.stringify(fitChartModel(session).items[0])), {
    kp: 'KP-01', name: '安全规程', learnerLevel: 3, windowTop: 5, difficulties: [3, 4],
  });
  assert.deepEqual(JSON.parse(JSON.stringify(pathChartModel(session)[0])), {
    name: '安全规程', completed: true, resourceCount: 2, difficulty: 3,
  });
});

test('resource count refreshes from the feedback-updated session', () => {
  const renderResourceCount = requiredApp('renderResourceCount');
  const heading = {textContent: ''};
  const count = renderResourceCount({resources: [{}, {}, {}]}, heading);
  assert.equal(count, 3);
  assert.equal(heading.textContent, 3);
});

test('intake and material failures announce errors through the alert region', async () => {
  const parseIntake = requiredApp('parseIntake');
  const stageMaterial = requiredApp('stageMaterial');
  const elements = {
    '#parseIntake': {disabled: false, textContent: ''},
    '#intakeText': {value: '测试学习经历'},
    '#intakeSummary': {hidden: true, textContent: ''},
    '#startInterview': {disabled: true},
    '#materialFile': {files: []},
    '#stageResult': {hidden: true, textContent: '', classList: {remove() {}}},
    '#uiAlert': {hidden: true, textContent: ''},
  };
  appSandbox.document = {querySelector: selector => elements[selector]};
  appSandbox.fetch = async () => ({ok: false, json: async () => ({error: '服务不可用'})});

  await parseIntake();
  assert.equal(elements['#uiAlert'].hidden, false);
  assert.match(elements['#uiAlert'].textContent, /无法解析：服务不可用/);

  elements['#uiAlert'].hidden = true;
  await stageMaterial();
  assert.equal(elements['#uiAlert'].hidden, false);
  assert.match(elements['#uiAlert'].textContent, /请先选择要提交的资料/);
});

test('gap chart exposes each learner mastery score as visible text', () => {
  const gapChartModel = requiredApp('gapChartModel');
  const drawGaps = requiredApp('drawGaps');
  const item = gapChartModel({
    diagnosis: {gaps: ['KP-01'], mastery: [{kp: 'KP-01', name: '安全规程', score: 0.5, correct: 2, asked: 4}]},
  })[0];
  assert.equal(item.scoreLabel, '掌握度 50.0%');

  const svg = {innerHTML: '', nodes: [], append(...nodes) { this.nodes.push(...nodes); }, appendChild(node) { this.nodes.push(node); }};
  appSandbox.document = {
    querySelector: selector => selector === '#chartGaps' ? svg : undefined,
    createElementNS: () => ({setAttribute() {}, textContent: ''}),
  };
  requiredApp('setSession')({diagnosis: {gaps: ['KP-01'], mastery: [{kp: 'KP-01', name: '安全规程', score: 0.5, correct: 2, asked: 4}]}});
  drawGaps();
  assert.ok(svg.nodes.some(node => node.textContent === '掌握度 50.0% · 2/4'));
});

test('submitFb refreshes the rendered resource count after feedback returns new resources', async () => {
  const submitFb = requiredApp('submitFb');
  const setSession = requiredApp('setSession');
  const element = () => ({hidden: true, textContent: '', innerHTML: '', disabled: false, value: 'KP-01', focus() {}, append() {}, appendChild() {}, insertAdjacentHTML() {}});
  const elements = {
    '#submitFb': element(), '#verdict': element(), '#decisionPanel': element(), '#workflowProgress': element(),
    '#timeline': element(), '#rcount': element(), '#resources': element(), '#learningPath': element(),
    '#fbKp': element(), '#quiz': element(), '#chartFit': element(), '#chartPath': element(), '#uiAlert': element(),
  };
  appSandbox.document = {
    querySelector: selector => elements[selector],
    querySelectorAll: selector => selector === '.feedback-question' ? [{dataset: {pick: '1', answer: '1'}}] : [],
    createElementNS: () => ({setAttribute() {}, textContent: '', appendChild() {}}),
  };
  const before = {session_id: 'S-1', events: [], resources: [], diagnosis: {mastery: [], gaps: []}, path: ['KP-01'], path_names: ['安全规程'], kp_index: {'KP-01': {name: '安全规程'}}};
  const after = {...before, resources: [{kp: 'KP-01', kind: 'guide', title: '新增资料', difficulty: 2, claims: [], body: '内容'}], decision: {action: 'advance', reason: '已掌握'}};
  setSession(before);
  appSandbox.fetch = async () => ({ok: true, json: async () => after});

  await submitFb();
  assert.equal(elements['#rcount'].textContent, 1);
});
