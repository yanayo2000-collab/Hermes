const test = require('node:test');
const assert = require('node:assert/strict');
const { withTimeout } = require('../src/promise_timeout');

test('withTimeout resolves before deadline', async () => {
  const result = await withTimeout(Promise.resolve('ok'), { timeoutMs: 100, label: 'fast' });
  assert.equal(result, 'ok');
});

test('withTimeout rejects with labeled timeout error', async () => {
  const blocker = new Promise(() => {});
  await assert.rejects(
    () => withTimeout(blocker, { timeoutMs: 20, label: 'approve_call' }),
    (error) => {
      assert.match(String(error && error.message), /approve_call timed out after 20ms/);
      return true;
    },
  );
});
