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
