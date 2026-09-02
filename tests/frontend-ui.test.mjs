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
