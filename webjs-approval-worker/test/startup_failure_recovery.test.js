const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const fs = require('node:fs');
const { spawn } = require('node:child_process');

function waitForServerReady(child, timeoutMs = 15000) {
  return new Promise((resolve, reject) => {
    let stdout = '';
    let stderr = '';
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error(`server did not become ready within ${timeoutMs}ms\nstdout:\n${stdout}\nstderr:\n${stderr}`));
    }, timeoutMs);
    const finish = (value, isError = false) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (isError) reject(value);
      else resolve({ stdout, stderr, value });
    };
    child.stdout.on('data', (chunk) => {
      stdout += String(chunk);
      if (stdout.includes('"status":"listening"')) {
        finish(true);
      }
    });
    child.stderr.on('data', (chunk) => {
      stderr += String(chunk);
    });
    child.once('exit', (code, signal) => {
      finish(new Error(`server exited before ready code=${code} signal=${signal}\nstdout:\n${stdout}\nstderr:\n${stderr}`), true);
    });
  });
}

function postJson(port, route, body) {
  return new Promise((resolve, reject) => {
    const req = http.request({
      hostname: '127.0.0.1',
      port,
      path: route,
      method: 'POST',
      headers: { 'content-type': 'application/json' },
    }, (res) => {
      let data = '';
      res.on('data', (chunk) => { data += String(chunk); });
      res.on('end', () => {
        resolve({ statusCode: res.statusCode, body: data });
      });
    });
    req.on('error', reject);
    req.write(JSON.stringify(body || {}));
    req.end();
  });
}

function getJson(port, route) {
  return new Promise((resolve, reject) => {
    const req = http.request({
      hostname: '127.0.0.1',
      port,
      path: route,
      method: 'GET',
    }, (res) => {
      let data = '';
      res.on('data', (chunk) => { data += String(chunk); });
      res.on('end', () => resolve({ statusCode: res.statusCode, body: data }));
    });
    req.on('error', reject);
    req.end();
  });
}

test('warmup surfaces initialize failure without crashing the worker process', async () => {
  const workerDir = path.resolve(__dirname, '..');
  const authDir = fs.mkdtempSync(path.join(os.tmpdir(), 'webjs-worker-auth-'));
  const port = 18787;
  const child = spawn(process.execPath, ['src/server.js'], {
    cwd: workerDir,
    env: {
      ...process.env,
      REGISTRATION_GROUP_APPROVAL_WEBJS_PORT: String(port),
      REGISTRATION_GROUP_APPROVAL_WEBJS_AUTH_MODE: 'dedicated_localauth',
      REGISTRATION_GROUP_APPROVAL_WEBJS_AUTH_DATA_PATH: authDir,
      REGISTRATION_GROUP_APPROVAL_WEBJS_CHROME_EXECUTABLE: '/definitely/missing-chrome-binary',
      REGISTRATION_GROUP_APPROVAL_WEBJS_QR_TIMEOUT_MS: '3000',
      REGISTRATION_GROUP_APPROVAL_WEBJS_PROTOCOL_TIMEOUT_MS: '30000',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  try {
    await waitForServerReady(child);
    const warmup = await postJson(port, '/warmup', {});
    assert.equal(warmup.statusCode, 500);
    assert.match(warmup.body, /bridge_internal_error|missing-chrome-binary|ENOENT|Browser was not found/i);

    await new Promise((resolve) => setTimeout(resolve, 1200));
    assert.equal(child.exitCode, null, 'worker should stay alive after warmup init failure');

    const health = await getJson(port, '/health');
    assert.equal(health.statusCode, 200);
    assert.match(health.body, /"status":"failed"|"last_error"/i);
  } finally {
    child.kill('SIGTERM');
  }
});
