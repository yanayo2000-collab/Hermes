const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const {
  recoverLocalAuthBrowserConflict,
  parseChromeProcessesUsingUserDataDir,
} = require('../src/server');

test('recoverLocalAuthBrowserConflict removes stale singleton locks and kills only matching chrome pids', () => {
  const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'webjs-worker-recovery-'));
  const userDataDir = path.join(tmpRoot, 'session-wa-approval-demo');
  fs.mkdirSync(userDataDir, { recursive: true });
  const singletonLock = path.join(userDataDir, 'SingletonLock');
  const singletonCookie = path.join(userDataDir, 'SingletonCookie');
  const singletonSocket = path.join(userDataDir, 'SingletonSocket');
  fs.writeFileSync(singletonLock, 'lock');
  fs.writeFileSync(singletonCookie, 'cookie');
  fs.writeFileSync(singletonSocket, 'socket');

  const killed = [];
  const result = recoverLocalAuthBrowserConflict({
    userDataDir,
    psOutput: [
      `123 /chrome --user-data-dir=${userDataDir}`,
      '456 /chrome --user-data-dir=/tmp/other-session',
      '789 /chrome without-user-data-dir',
    ].join('\n'),
    execFileSync(command, args) {
      killed.push({ command, args });
      return '';
    },
  });

  assert.equal(result.cleaned_lock_files.length, 3);
  assert.deepEqual(result.killed_pids, [123]);
  assert.equal(fs.existsSync(singletonLock), false);
  assert.equal(fs.existsSync(singletonCookie), false);
  assert.equal(fs.existsSync(singletonSocket), false);
  assert.deepEqual(killed, [{ command: 'kill', args: ['-TERM', '123'] }]);
});

test('recoverLocalAuthBrowserConflict skips pid kill when no process owns the same user-data-dir', () => {
  const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'webjs-worker-recovery-'));
  const userDataDir = path.join(tmpRoot, 'session-wa-approval-demo');
  fs.mkdirSync(userDataDir, { recursive: true });
  const singletonLock = path.join(userDataDir, 'SingletonLock');
  fs.writeFileSync(singletonLock, 'lock');

  const killed = [];
  const result = recoverLocalAuthBrowserConflict({
    userDataDir,
    psOutput: '456 /chrome --user-data-dir=/tmp/other-session',
    execFileSync(command, args) {
      killed.push({ command, args });
      return '';
    },
  });

  assert.deepEqual(result.killed_pids, []);
  assert.equal(fs.existsSync(singletonLock), false);
  assert.deepEqual(killed, []);
});

test('parseChromeProcessesUsingUserDataDir supports split user-data-dir arguments', () => {
  const userDataDir = '/tmp/session with spaces';
  const rows = parseChromeProcessesUsingUserDataDir(
    [
      `123 /chrome --user-data-dir "${userDataDir}"`,
      `456 /chrome --user-data-dir /tmp/other-session`,
    ].join('\n'),
    userDataDir,
  );
  assert.deepEqual(rows.map((entry) => entry.pid), [123]);
});

test('parseChromeProcessesUsingUserDataDir ignores non-chrome commands even when user-data-dir matches', () => {
  const userDataDir = '/tmp/session';
  const rows = parseChromeProcessesUsingUserDataDir(
    [
      `123 /usr/bin/python worker.py --user-data-dir=${userDataDir}`,
      `456 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir=${userDataDir}`,
    ].join('\n'),
    userDataDir,
  );
  assert.deepEqual(rows.map((entry) => entry.pid), [456]);
});
