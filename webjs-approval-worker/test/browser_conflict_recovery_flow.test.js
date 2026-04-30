const test = require('node:test');
const assert = require('node:assert/strict');
const { runBrowserConflictRecoveryFlow } = require('../src/server');

test('runBrowserConflictRecoveryFlow retries once without final failure on first browser conflict', async () => {
  const calls = [];
  const result = await runBrowserConflictRecoveryFlow({
    error: new Error('The browser is already running for /tmp/session. Use a different `userDataDir` or stop the running browser first.'),
    reuseChromeProfile: false,
    browserConflictRecoveryAttempted: false,
    recover: async () => {
      calls.push('recover');
      return { cleaned_lock_files: ['/tmp/session/SingletonLock'], killed_pids: [123] };
    },
    onRecovered: async (payload) => {
      calls.push(['onRecovered', payload.cleaned_lock_files.length, payload.killed_pids.length]);
    },
    retry: async () => {
      calls.push('retry');
      return 'retried-success';
    },
    onFinalFailure: async () => {
      calls.push('finalFailure');
    },
  });

  assert.equal(result, 'retried-success');
  assert.deepEqual(calls, [
    'recover',
    ['onRecovered', 1, 1],
    'retry',
  ]);
});

test('runBrowserConflictRecoveryFlow falls through to final failure after retry budget is exhausted', async () => {
  const calls = [];
  const boom = new Error('The browser is already running for /tmp/session. Use a different `userDataDir` or stop the running browser first.');
  const result = await runBrowserConflictRecoveryFlow({
    error: boom,
    reuseChromeProfile: false,
    browserConflictRecoveryAttempted: true,
    recover: async () => {
      calls.push('recover');
    },
    onRecovered: async () => {
      calls.push('onRecovered');
    },
    retry: async () => {
      calls.push('retry');
      return 'should-not-happen';
    },
    onFinalFailure: async (error) => {
      calls.push(['finalFailure', error.message]);
      return 'failed';
    },
  });

  assert.equal(result, 'failed');
  assert.deepEqual(calls, [
    ['finalFailure', boom.message],
  ]);
});

test('runBrowserConflictRecoveryFlow retries once for approval branch callbacks', async () => {
  const calls = [];
  const result = await runBrowserConflictRecoveryFlow({
    error: new Error('The browser is already running for /tmp/approval-session. Use a different `userDataDir` or stop the running browser first.'),
    reuseChromeProfile: false,
    browserConflictRecoveryAttempted: false,
    recover: async () => {
      calls.push('recover-approval');
      return { cleaned_lock_files: ['/tmp/approval-session/SingletonSocket'], killed_pids: [] };
    },
    onRecovered: async (payload) => {
      calls.push(['onRecovered-approval', payload.cleaned_lock_files[0]]);
    },
    retry: async () => {
      calls.push('retry-approval');
      return 'approval-retried-success';
    },
    onFinalFailure: async () => {
      calls.push('finalFailure-approval');
    },
  });

  assert.equal(result, 'approval-retried-success');
  assert.deepEqual(calls, [
    'recover-approval',
    ['onRecovered-approval', '/tmp/approval-session/SingletonSocket'],
    'retry-approval',
  ]);
});

test('runBrowserConflictRecoveryFlow propagates final failure when handler throws', async () => {
  const boom = new Error('final failure');
  await assert.rejects(
    () => runBrowserConflictRecoveryFlow({
      error: new Error('other init error'),
      reuseChromeProfile: false,
      browserConflictRecoveryAttempted: true,
      recover: async () => ({ cleaned_lock_files: [], killed_pids: [] }),
      retry: async () => 'should-not-happen',
      onFinalFailure: async () => {
        throw boom;
      },
    }),
    boom,
  );
});
