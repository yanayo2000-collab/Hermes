const test = require('node:test');
const assert = require('node:assert/strict');
const { createApprovalRunStore } = require('../src/approval_run_store');

test('reuses in-flight promise for the same approval_run_id', async () => {
  const store = createApprovalRunStore({ ttlMs: 1000, now: () => 0 });
  let executions = 0;
  const run = () => store.run('run-1', async () => {
    executions += 1;
    return { result_code: 'approved' };
  });

  const [a, b] = await Promise.all([run(), run()]);
  assert.equal(executions, 1);
  assert.deepEqual(a, { result_code: 'approved' });
  assert.deepEqual(b, { result_code: 'approved' });
});

test('returns cached completed result within ttl', async () => {
  let nowValue = 0;
  const store = createApprovalRunStore({ ttlMs: 1000, now: () => nowValue });
  let executions = 0;

  const first = await store.run('run-2', async () => {
    executions += 1;
    return { result_code: 'approved', approved_count: 1 };
  });
  nowValue = 500;
  const second = await store.run('run-2', async () => {
    executions += 1;
    return { result_code: 'approved', approved_count: 2 };
  });

  assert.equal(executions, 1);
  assert.deepEqual(first, second);
  assert.equal(second.approved_count, 1);
});

test('reruns after ttl expires', async () => {
  let nowValue = 0;
  const store = createApprovalRunStore({ ttlMs: 1000, now: () => nowValue });
  let executions = 0;

  await store.run('run-3', async () => {
    executions += 1;
    return { result_code: 'approved', approved_count: 1 };
  });
  nowValue = 1500;
  const second = await store.run('run-3', async () => {
    executions += 1;
    return { result_code: 'approved', approved_count: 2 };
  });

  assert.equal(executions, 2);
  assert.equal(second.approved_count, 2);
});
