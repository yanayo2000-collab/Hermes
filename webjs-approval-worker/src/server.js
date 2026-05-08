const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');
const qrcodeTerminal = require('qrcode-terminal');
const { Client, LocalAuth, NoAuth } = require('whatsapp-web.js');
const { createApprovalRunStore } = require('./approval_run_store');
const { withTimeout } = require('./promise_timeout');

const PORT = Number(process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_PORT || 8787);
const HOST = process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_HOST || '127.0.0.1';
const AUTH_DATA_PATH = process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_AUTH_DATA_PATH || path.join(process.cwd(), '.wwebjs_auth');
const CLIENT_ID = process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_CLIENT_ID || 'registration-group-approval';
const HEADLESS = String(process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_HEADLESS || 'true').trim().toLowerCase() !== 'false';
const QR_TIMEOUT_MS = Math.max(3000, Number(process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_QR_TIMEOUT_MS || 15000));
const APPROVAL_CALL_TIMEOUT_MS = Math.max(2000, Number(process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_APPROVE_CALL_TIMEOUT_MS || 15000));
const APPROVAL_PER_REQUESTER_TIMEOUT_MS = Math.max(1200, Number(process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_PER_REQUESTER_TIMEOUT_MS || 5000));
const APPROVAL_PER_REQUESTER_SLEEP_MS = Math.max(0, Number(process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_PER_REQUESTER_SLEEP_MS || 50));
const APPROVAL_VERIFY_WAIT_MS = Math.max(300, Number(process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_VERIFY_WAIT_MS || 1200));
const APPROVAL_VERIFY_RETRIES = Math.max(1, Number(process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_VERIFY_RETRIES || 4));
const CHROME_EXECUTABLE = process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_CHROME_EXECUTABLE || process.env.PUPPETEER_EXECUTABLE_PATH || undefined;
const WORKER_EVENT_LOG = process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_EVENT_LOG || path.join(process.cwd(), 'logs', 'registration_group_webjs_worker.jsonl');
const AUTH_MODE = String(process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_AUTH_MODE || '').trim().toLowerCase();
const CHROME_USER_DATA_ROOT = process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_CHROME_USER_DATA_ROOT || '';
const CHROME_PROFILE_DIR = process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_CHROME_PROFILE_DIR || '';
const REUSE_CHROME_PROFILE = AUTH_MODE
  ? AUTH_MODE === 'chrome_profile_copy'
  : Boolean(String(CHROME_USER_DATA_ROOT).trim() && String(CHROME_PROFILE_DIR).trim());
const SHARED_APPROVAL_CLIENT = !REUSE_CHROME_PROFILE;
const POST_APPROVE_PROBE_REFRESH_ENABLED = String(process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_POST_APPROVE_PROBE_REFRESH || 'false').trim().toLowerCase() === 'true';
const PUPPETEER_PROTOCOL_TIMEOUT_MS = Math.max(30000, Number(process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_PROTOCOL_TIMEOUT_MS || 180000));

const stateAuthStrategy = REUSE_CHROME_PROFILE ? 'ChromeProfileCopy+NoAuth' : 'LocalAuth';
const stateAuthPath = REUSE_CHROME_PROFILE
  ? `${CHROME_USER_DATA_ROOT} :: ${CHROME_PROFILE_DIR}`
  : AUTH_DATA_PATH;

const state = {
  provider: 'whatsapp_webjs_bridge',
  schema_version: 'registration-group-webjs-bridge-v1',
  supports: ['approve', 'strict_queue_and_member_verify', 'crm_batch_writeback_ready', 'dedicated_approval_client'],
  status: 'idle',
  ready: false,
  authenticated: false,
  auth_strategy: stateAuthStrategy,
  mode: 'real_webjs',
  client_id: CLIENT_ID,
  auth_path: stateAuthPath,
  host: HOST,
  port: PORT,
  last_error: null,
  last_started_at: new Date().toISOString(),
  last_action_at: null,
  last_qr_at: null,
  last_qr: null,
  last_disconnected_reason: null,
};
const approvalState = {
  status: 'idle',
  ready: false,
  authenticated: false,
  auth_strategy: stateAuthStrategy,
  mode: 'real_webjs',
  client_id: SHARED_APPROVAL_CLIENT ? CLIENT_ID : `${CLIENT_ID}-approval`,
  auth_path: stateAuthPath,
  last_error: null,
  last_started_at: new Date().toISOString(),
  last_action_at: null,
  last_qr_at: null,
  last_qr: null,
  last_disconnected_reason: null,
  chrome_profile_source: null,
  chrome_profile_mode: null,
};

let client = null;
let initPromise = null;
let approvalClient = null;
let approvalInitPromise = null;
let probeRefreshPromise = null;
let actionLock = Promise.resolve();
const approvalRunStore = createApprovalRunStore({ ttlMs: 10 * 60 * 1000 });
let readyWaiters = [];
let qrWaiters = [];
let approvalReadyWaiters = [];
let approvalQrWaiters = [];
let runtimeChromeUserDataDir = null;
let runtimeApprovalChromeUserDataDir = null;

function updateState(patch) {
  Object.assign(state, patch || {});
}

function updateApprovalState(patch) {
  Object.assign(approvalState, patch || {});
}

function syncApprovalStateFromPrimary() {
  if (!SHARED_APPROVAL_CLIENT) return;
  updateApprovalState({
    status: state.status,
    ready: state.ready,
    authenticated: state.authenticated,
    auth_strategy: state.auth_strategy,
    mode: state.mode,
    client_id: CLIENT_ID,
    auth_path: state.auth_path,
    last_error: state.last_error,
    last_started_at: state.last_started_at,
    last_action_at: state.last_action_at,
    last_qr_at: state.last_qr_at,
    last_qr: state.last_qr,
    last_disconnected_reason: state.last_disconnected_reason,
    chrome_profile_source: state.chrome_profile_source || null,
    chrome_profile_mode: state.chrome_profile_mode || null,
  });
}

function logEvent(kind, payload) {
  try {
    const record = JSON.stringify({
      ts: new Date().toISOString(),
      kind,
      ...payload,
    });
    fs.mkdirSync(path.dirname(WORKER_EVENT_LOG), { recursive: true });
    fs.appendFileSync(WORKER_EVENT_LOG, `${record}\n`);
  } catch (_) {}
}

function settleWaiters(waiters, payload) {
  const current = waiters.splice(0, waiters.length);
  current.forEach((entry) => {
    clearTimeout(entry.timer);
    entry.resolve(payload);
  });
}

function rejectWaiters(waiters, error) {
  const current = waiters.splice(0, waiters.length);
  current.forEach((entry) => {
    clearTimeout(entry.timer);
    entry.reject(error);
  });
}

function handleClientInitializeFailure(error) {
  const failure = error instanceof Error ? error : new Error(String(error || 'client_initialize_failed'));
  updateState({ status: 'failed', ready: false, authenticated: false, last_error: String(failure && failure.stack ? failure.stack : failure) });
  rejectWaiters(readyWaiters, failure);
  rejectWaiters(qrWaiters, failure);
  client = null;
  initPromise = null;
  cleanupRuntimeChromeUserDataDir('probe');
  return failure;
}

function handleApprovalClientInitializeFailure(error) {
  const failure = error instanceof Error ? error : new Error(String(error || 'approval_client_initialize_failed'));
  updateApprovalState({ status: 'failed', ready: false, authenticated: false, last_error: String(failure && failure.stack ? failure.stack : failure) });
  rejectWaiters(approvalReadyWaiters, failure);
  rejectWaiters(approvalQrWaiters, failure);
  approvalClient = null;
  approvalInitPromise = null;
  cleanupRuntimeChromeUserDataDir('approval');
  return failure;
}

function waitForReady(timeoutMs) {
  if (state.ready) {
    return Promise.resolve({ kind: 'ready' });
  }
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(`ready timeout after ${timeoutMs}ms`));
    }, timeoutMs);
    readyWaiters.push({ resolve, reject, timer });
  });
}

function waitForApprovalReady(timeoutMs) {
  if (approvalState.ready) {
    return Promise.resolve({ kind: 'ready' });
  }
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(`approval ready timeout after ${timeoutMs}ms`));
    }, timeoutMs);
    approvalReadyWaiters.push({ resolve, reject, timer });
  });
}

function waitForQrOrReady(timeoutMs) {
  if (state.ready) {
    return Promise.resolve({ kind: 'ready' });
  }
  if (state.last_qr) {
    return Promise.resolve({ kind: 'qr', qr: state.last_qr });
  }
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(`qr/ready timeout after ${timeoutMs}ms`));
    }, timeoutMs);
    const done = (payload) => {
      clearTimeout(timer);
      resolve(payload);
    };
    readyWaiters.push({ resolve: () => done({ kind: 'ready' }), reject, timer });
    qrWaiters.push({ resolve: (payload) => done(payload), reject, timer });
  });
}

function waitForApprovalQrOrReady(timeoutMs) {
  if (approvalState.ready) {
    return Promise.resolve({ kind: 'ready' });
  }
  if (approvalState.last_qr) {
    return Promise.resolve({ kind: 'qr', qr: approvalState.last_qr });
  }
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(`approval qr/ready timeout after ${timeoutMs}ms`));
    }, timeoutMs);
    const done = (payload) => {
      clearTimeout(timer);
      resolve(payload);
    };
    approvalReadyWaiters.push({ resolve: () => done({ kind: 'ready' }), reject, timer });
    approvalQrWaiters.push({ resolve: (payload) => done(payload), reject, timer });
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isRecoverableApprovalClientError(error) {
  const message = String(error && error.message ? error.message : error || '');
  return (
    message.includes('Attempted to use detached Frame')
    || message.includes('Execution context was destroyed')
    || message.includes('Runtime.callFunctionOn')
  );
}

function isBrowserAlreadyRunningError(error) {
  const message = String(error && error.message ? error.message : error || '');
  return message.includes('The browser is already running for');
}

function normalizePhone(value) {
  return String(value || '').replace(/[^\d+]/g, '');
}

function safeString(value) {
  if (!value) return null;
  if (typeof value === 'string') return value;
  if (typeof value === 'object' && value._serialized) return value._serialized;
  if (typeof value === 'object' && value.user) return String(value.user);
  return String(value);
}

function resolveLocalAuthSessionDir(dataPath, clientId) {
  const basePath = path.resolve(String(dataPath || '').trim() || '.');
  const normalizedClientId = String(clientId || '').trim();
  return path.join(basePath, normalizedClientId ? `session-${normalizedClientId}` : 'session');
}

function parseChromeProcessesUsingUserDataDir(psOutput, userDataDir) {
  const normalizedDir = path.resolve(String(userDataDir || '').trim());
  if (!normalizedDir) return [];
  return String(psOutput || '')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const match = line.match(/^(\d+)\s+(.*)$/);
      if (!match) return null;
      const pid = Number(match[1]);
      const command = match[2] || '';
      if (!/(^|\s|\/)(chrome|chromium)(\s|$)/i.test(command)) {
        return null;
      }
      const dirMatch = command.match(/--user-data-dir(?:=(?:"([^"]+)"|'([^']+)'|(\S+))|\s+(?:"([^"]+)"|'([^']+)'|(\S+)))/);
      const processUserDataDir = path.resolve(String((dirMatch && (dirMatch[1] || dirMatch[2] || dirMatch[3] || dirMatch[4] || dirMatch[5] || dirMatch[6])) || '').trim() || '.');
      if (!dirMatch || processUserDataDir !== normalizedDir) {
        return null;
      }
      return { pid, command, userDataDir: processUserDataDir };
    })
    .filter((entry) => entry && Number.isFinite(entry.pid) && entry.pid > 0);
}

function recoverLocalAuthBrowserConflict(options = {}) {
  const userDataDir = path.resolve(String(options.userDataDir || '').trim() || '.');
  const fsApi = options.fsApi || fs;
  const runner = options.execFileSync || execFileSync;
  const psOutput = options.psOutput == null
    ? String(runner('ps', ['-Ao', 'pid=,command='], { encoding: 'utf8' }) || '')
    : String(options.psOutput || '');
  const cleanedLockFiles = [];
  const killedPids = [];
  const lockFileNames = ['SingletonLock', 'SingletonCookie', 'SingletonSocket'];
  lockFileNames.forEach((name) => {
    const filePath = path.join(userDataDir, name);
    if (!fsApi.existsSync(filePath)) return;
    try {
      fsApi.rmSync(filePath, { recursive: true, force: true });
      cleanedLockFiles.push(filePath);
    } catch (_) {}
  });

  const matchingProcesses = parseChromeProcessesUsingUserDataDir(psOutput, userDataDir);
  matchingProcesses.forEach((entry) => {
    try {
      runner('kill', ['-TERM', String(entry.pid)], { encoding: 'utf8' });
      killedPids.push(entry.pid);
    } catch (_) {}
  });

  return {
    user_data_dir: userDataDir,
    cleaned_lock_files: cleanedLockFiles,
    killed_pids: killedPids,
  };
}

async function runBrowserConflictRecoveryFlow(options = {}) {
  const error = options.error;
  const reuseChromeProfile = Boolean(options.reuseChromeProfile);
  const browserConflictRecoveryAttempted = Boolean(options.browserConflictRecoveryAttempted);
  if (!reuseChromeProfile && !browserConflictRecoveryAttempted && isBrowserAlreadyRunningError(error)) {
    const recovery = await options.recover();
    if (options.onRecovered) {
      await options.onRecovered(recovery);
    }
    return options.retry();
  }
  return options.onFinalFailure(error);
}

function cleanupRuntimeChromeUserDataDir(target = 'probe') {
  const currentDir = target === 'approval' ? runtimeApprovalChromeUserDataDir : runtimeChromeUserDataDir;
  if (!currentDir) return;
  try {
    fs.rmSync(currentDir, { recursive: true, force: true });
  } catch (_) {}
  if (target === 'approval') {
    runtimeApprovalChromeUserDataDir = null;
  } else {
    runtimeChromeUserDataDir = null;
  }
}

async function resetClientSession(reason) {
  const existing = client;
  client = null;
  initPromise = null;
  updateState({
    ready: false,
    authenticated: false,
    status: 'resetting',
    last_error: reason ? String(reason) : state.last_error,
  });
  if (existing) {
    try {
      await existing.destroy();
    } catch (_) {}
  }
  cleanupRuntimeChromeUserDataDir('probe');
}

async function resetApprovalClientSession(reason) {
  if (SHARED_APPROVAL_CLIENT) {
    await resetClientSession(reason);
    approvalClient = client;
    approvalInitPromise = initPromise;
    syncApprovalStateFromPrimary();
    return;
  }
  const existing = approvalClient;
  approvalClient = null;
  approvalInitPromise = null;
  updateApprovalState({
    ready: false,
    authenticated: false,
    status: 'resetting',
    last_error: reason ? String(reason) : approvalState.last_error,
  });
  if (existing) {
    try {
      await existing.destroy();
    } catch (_) {}
  }
  cleanupRuntimeChromeUserDataDir('approval');
}

function scheduleProbeClientRefresh(reason) {
  if (probeRefreshPromise) {
    return probeRefreshPromise;
  }
  probeRefreshPromise = (async () => {
    try {
      logEvent('probe_client_refresh_started', {
        reason: reason ? String(reason) : null,
      });
      await resetClientSession(reason || 'post_approval_probe_refresh');
      await ensureClientStarted();
      await waitForReady(QR_TIMEOUT_MS).catch(() => {
        if (!state.ready) {
          throw new Error(state.last_qr ? 'probe client awaiting qr scan after refresh' : 'probe client not ready after refresh');
        }
      });
      logEvent('probe_client_refresh_finished', {
        status: state.status,
        ready: state.ready,
        authenticated: state.authenticated,
      });
    } catch (error) {
      updateState({ last_error: String(error && error.message ? error.message : error) });
      logEvent('probe_client_refresh_failed', {
        error: String(error && error.stack ? error.stack : error || 'unknown_error'),
      });
    } finally {
      probeRefreshPromise = null;
    }
  })();
  return probeRefreshPromise;
}

function prepareCopiedChromeProfile(target = 'probe') {
  const sourceRoot = path.resolve(String(CHROME_USER_DATA_ROOT || '').trim());
  const profileDirName = String(CHROME_PROFILE_DIR || '').trim();
  if (!sourceRoot || !profileDirName) {
    throw new Error('chrome profile reuse requires REGISTRATION_GROUP_APPROVAL_WEBJS_CHROME_USER_DATA_ROOT and REGISTRATION_GROUP_APPROVAL_WEBJS_CHROME_PROFILE_DIR');
  }

  const localStatePath = path.join(sourceRoot, 'Local State');
  const sourceProfilePath = path.join(sourceRoot, profileDirName);
  if (!fs.existsSync(localStatePath)) {
    throw new Error(`Chrome Local State not found: ${localStatePath}`);
  }
  if (!fs.existsSync(sourceProfilePath)) {
    throw new Error(`Chrome profile directory not found: ${sourceProfilePath}`);
  }

  cleanupRuntimeChromeUserDataDir(target);
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), target === 'approval' ? 'webjs-approval-approve-profile-' : 'webjs-approval-profile-'));
  fs.copyFileSync(localStatePath, path.join(tempRoot, 'Local State'));
  fs.cpSync(sourceProfilePath, path.join(tempRoot, profileDirName), {
    recursive: true,
    dereference: false,
    force: true,
  });
  if (target === 'approval') {
    runtimeApprovalChromeUserDataDir = tempRoot;
  } else {
    runtimeChromeUserDataDir = tempRoot;
  }
  return {
    tempRoot,
    profileDirName,
    sourceProfilePath,
  };
}

async function ensureClientStarted(options = {}) {
  const browserConflictRecoveryAttempted = Boolean(options.browserConflictRecoveryAttempted);
  const returnInitPromise = Boolean(options.returnInitPromise);
  if (client) {
    return returnInitPromise ? Promise.resolve() : client;
  }
  if (initPromise) {
    if (returnInitPromise) {
      return initPromise;
    }
    await initPromise;
    return client;
  }

  updateState({ status: 'initializing', last_error: null });
  const puppeteer = {
    headless: HEADLESS,
    protocolTimeout: PUPPETEER_PROTOCOL_TIMEOUT_MS,
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
  };
  if (CHROME_EXECUTABLE) {
    puppeteer.executablePath = CHROME_EXECUTABLE;
  }

  let authStrategy = null;
  if (REUSE_CHROME_PROFILE) {
    const copied = prepareCopiedChromeProfile('probe');
    puppeteer.userDataDir = copied.tempRoot;
    puppeteer.args.push(`--profile-directory=${copied.profileDirName}`);
    authStrategy = new NoAuth();
    updateState({
      auth_strategy: 'ChromeProfileCopy+NoAuth',
      auth_path: copied.tempRoot,
      chrome_profile_source: copied.sourceProfilePath,
      chrome_profile_mode: 'copy',
    });
  } else {
    authStrategy = new LocalAuth({
      clientId: CLIENT_ID,
      dataPath: AUTH_DATA_PATH,
    });
    updateState({
      auth_strategy: 'LocalAuth',
      auth_path: AUTH_DATA_PATH,
      chrome_profile_source: null,
      chrome_profile_mode: null,
    });
  }

  client = new Client({
    authStrategy,
    puppeteer,
    takeoverOnConflict: true,
    qrMaxRetries: 0,
  });

  client.on('qr', (qr) => {
    updateState({
      status: 'awaiting_qr',
      ready: false,
      authenticated: false,
      last_qr: qr,
      last_qr_at: new Date().toISOString(),
      last_error: null,
    });
    try {
      qrcodeTerminal.generate(qr, { small: true });
    } catch (_) {}
    syncApprovalStateFromPrimary();
    settleWaiters(approvalQrWaiters, { kind: 'qr', qr });
    settleWaiters(qrWaiters, { kind: 'qr', qr });
  });

  client.on('authenticated', () => {
    updateState({ authenticated: true, status: 'authenticated', last_error: null });
    syncApprovalStateFromPrimary();
  });

  client.on('ready', () => {
    updateState({
      status: 'warm',
      ready: true,
      authenticated: true,
      last_error: null,
      last_qr: null,
      last_action_at: new Date().toISOString(),
    });
    syncApprovalStateFromPrimary();
    settleWaiters(approvalReadyWaiters, { kind: 'ready' });
    settleWaiters(readyWaiters, { kind: 'ready' });
  });

  client.on('auth_failure', (message) => {
    updateState({
      status: 'auth_failure',
      ready: false,
      authenticated: false,
      last_error: message || 'auth_failure',
    });
    const error = new Error(message || 'auth_failure');
    syncApprovalStateFromPrimary();
    rejectWaiters(approvalReadyWaiters, error);
    rejectWaiters(approvalQrWaiters, error);
    rejectWaiters(readyWaiters, error);
    rejectWaiters(qrWaiters, error);
  });

  client.on('disconnected', (reason) => {
    updateState({
      status: 'disconnected',
      ready: false,
      authenticated: false,
      last_disconnected_reason: String(reason || ''),
      last_error: String(reason || 'disconnected'),
    });
    syncApprovalStateFromPrimary();
  });

  initPromise = client.initialize()
    .catch((error) => runBrowserConflictRecoveryFlow({
      error,
      reuseChromeProfile: REUSE_CHROME_PROFILE,
      browserConflictRecoveryAttempted,
      recover: async () => {
        const localAuthSessionDir = resolveLocalAuthSessionDir(AUTH_DATA_PATH, CLIENT_ID);
        return recoverLocalAuthBrowserConflict({ userDataDir: localAuthSessionDir });
      },
      onRecovered: async (browserConflictRecovery) => {
        const localAuthSessionDir = resolveLocalAuthSessionDir(AUTH_DATA_PATH, CLIENT_ID);
        if (browserConflictRecovery.cleaned_lock_files.length || browserConflictRecovery.killed_pids.length) {
          logEvent('local_auth_browser_conflict_recovered', {
            target: 'probe',
            user_data_dir: localAuthSessionDir,
            cleaned_lock_files: browserConflictRecovery.cleaned_lock_files,
            killed_pids: browserConflictRecovery.killed_pids,
          });
        }
        client = null;
        cleanupRuntimeChromeUserDataDir('probe');
        updateState({ status: 'recovering_browser_conflict', ready: false, authenticated: false, last_error: String(error && error.message ? error.message : error) });
      },
      retry: () => ensureClientStarted({ browserConflictRecoveryAttempted: true, returnInitPromise: true }),
      onFinalFailure: (finalError) => {
        throw handleClientInitializeFailure(finalError);
      },
    }));
  const activeInitPromise = initPromise;
  activeInitPromise.then(
    () => {
      if (initPromise === activeInitPromise) {
        initPromise = null;
      }
    },
    () => {
      if (initPromise === activeInitPromise) {
        initPromise = null;
      }
    },
  );

  if (returnInitPromise) {
    return activeInitPromise;
  }
  await activeInitPromise;
  return client;
}

async function ensureApprovalClientStarted(options = {}) {
  const browserConflictRecoveryAttempted = Boolean(options.browserConflictRecoveryAttempted);
  const returnInitPromise = Boolean(options.returnInitPromise);
  if (SHARED_APPROVAL_CLIENT) {
    const activeClient = await ensureClientStarted();
    approvalClient = activeClient;
    approvalInitPromise = initPromise;
    syncApprovalStateFromPrimary();
    return returnInitPromise ? approvalInitPromise : approvalClient;
  }
  if (approvalClient) {
    return returnInitPromise ? Promise.resolve() : approvalClient;
  }
  if (approvalInitPromise) {
    if (returnInitPromise) {
      return approvalInitPromise;
    }
    await approvalInitPromise;
    return approvalClient;
  }

  updateApprovalState({ status: 'initializing', last_error: null });
  const puppeteer = {
    headless: HEADLESS,
    protocolTimeout: PUPPETEER_PROTOCOL_TIMEOUT_MS,
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
  };
  if (CHROME_EXECUTABLE) {
    puppeteer.executablePath = CHROME_EXECUTABLE;
  }

  let authStrategy = null;
  if (REUSE_CHROME_PROFILE) {
    const copied = prepareCopiedChromeProfile('approval');
    puppeteer.userDataDir = copied.tempRoot;
    puppeteer.args.push(`--profile-directory=${copied.profileDirName}`);
    authStrategy = new NoAuth();
    updateApprovalState({
      auth_strategy: 'ChromeProfileCopy+NoAuth',
      auth_path: copied.tempRoot,
      chrome_profile_source: copied.sourceProfilePath,
      chrome_profile_mode: 'copy',
    });
  } else {
    authStrategy = new LocalAuth({
      clientId: `${CLIENT_ID}-approval`,
      dataPath: AUTH_DATA_PATH,
    });
    updateApprovalState({
      auth_strategy: 'LocalAuth',
      auth_path: AUTH_DATA_PATH,
      chrome_profile_source: null,
      chrome_profile_mode: null,
    });
  }

  approvalClient = new Client({
    authStrategy,
    puppeteer,
    takeoverOnConflict: true,
    qrMaxRetries: 0,
  });

  approvalClient.on('qr', (qr) => {
    updateApprovalState({
      status: 'awaiting_qr',
      ready: false,
      authenticated: false,
      last_qr: qr,
      last_qr_at: new Date().toISOString(),
      last_error: null,
    });
    settleWaiters(approvalQrWaiters, { kind: 'qr', qr });
  });

  approvalClient.on('authenticated', () => {
    updateApprovalState({ authenticated: true, status: 'authenticated', last_error: null });
  });

  approvalClient.on('ready', () => {
    updateApprovalState({
      status: 'warm',
      ready: true,
      authenticated: true,
      last_error: null,
      last_qr: null,
      last_action_at: new Date().toISOString(),
    });
    settleWaiters(approvalReadyWaiters, { kind: 'ready' });
  });

  approvalClient.on('auth_failure', (message) => {
    updateApprovalState({
      status: 'auth_failure',
      ready: false,
      authenticated: false,
      last_error: message || 'auth_failure',
    });
    const error = new Error(message || 'auth_failure');
    rejectWaiters(approvalReadyWaiters, error);
    rejectWaiters(approvalQrWaiters, error);
  });

  approvalClient.on('disconnected', (reason) => {
    updateApprovalState({
      status: 'disconnected',
      ready: false,
      authenticated: false,
      last_disconnected_reason: String(reason || ''),
      last_error: String(reason || 'disconnected'),
    });
  });

  approvalInitPromise = approvalClient.initialize()
    .catch((error) => runBrowserConflictRecoveryFlow({
      error,
      reuseChromeProfile: REUSE_CHROME_PROFILE,
      browserConflictRecoveryAttempted,
      recover: async () => {
        const approvalLocalAuthSessionDir = resolveLocalAuthSessionDir(AUTH_DATA_PATH, `${CLIENT_ID}-approval`);
        return recoverLocalAuthBrowserConflict({ userDataDir: approvalLocalAuthSessionDir });
      },
      onRecovered: async (browserConflictRecovery) => {
        const approvalLocalAuthSessionDir = resolveLocalAuthSessionDir(AUTH_DATA_PATH, `${CLIENT_ID}-approval`);
        if (browserConflictRecovery.cleaned_lock_files.length || browserConflictRecovery.killed_pids.length) {
          logEvent('local_auth_browser_conflict_recovered', {
            target: 'approval',
            user_data_dir: approvalLocalAuthSessionDir,
            cleaned_lock_files: browserConflictRecovery.cleaned_lock_files,
            killed_pids: browserConflictRecovery.killed_pids,
          });
        }
        approvalClient = null;
        cleanupRuntimeChromeUserDataDir('approval');
        updateApprovalState({ status: 'recovering_browser_conflict', ready: false, authenticated: false, last_error: String(error && error.message ? error.message : error) });
      },
      retry: () => ensureApprovalClientStarted({ browserConflictRecoveryAttempted: true, returnInitPromise: true }),
      onFinalFailure: (finalError) => {
        throw handleApprovalClientInitializeFailure(finalError);
      },
    }));
  const activeApprovalInitPromise = approvalInitPromise;
  activeApprovalInitPromise.then(
    () => {
      if (approvalInitPromise === activeApprovalInitPromise) {
        approvalInitPromise = null;
      }
    },
    () => {
      if (approvalInitPromise === activeApprovalInitPromise) {
        approvalInitPromise = null;
      }
    },
  );

  if (returnInitPromise) {
    return activeApprovalInitPromise;
  }
  await activeApprovalInitPromise;
  return approvalClient;
}

function withActionLock(fn) {
  const run = actionLock.then(() => fn());
  actionLock = run.catch(() => undefined);
  return run;
}

function extractInviteCode(targetValue) {
  const match = String(targetValue || '').trim().match(/chat\.whatsapp\.com\/([A-Za-z0-9_-]+)/i);
  return match ? String(match[1] || '').trim() : '';
}

function inviteInfoGroupId(inviteInfo) {
  const candidates = [
    inviteInfo && inviteInfo.id,
    inviteInfo && inviteInfo.gid,
    inviteInfo && inviteInfo.groupId,
    inviteInfo && inviteInfo.chatId,
    inviteInfo && inviteInfo.groupWid,
  ];
  for (const candidate of candidates) {
    if (!candidate) continue;
    if (typeof candidate === 'string') {
      const normalized = String(candidate).trim();
      if (normalized) return normalized;
      continue;
    }
    if (candidate._serialized) {
      const normalized = String(candidate._serialized || '').trim();
      if (normalized) return normalized;
    }
  }
  return '';
}

async function resolveGroupWithClient(activeClient, target) {
  const targetValue = String(target || '').trim();
  if (!targetValue) {
    throw new Error('registration_group is required');
  }
  if (/@g\.us$/.test(targetValue)) {
    return await activeClient.getChatById(targetValue);
  }
  const inviteCode = extractInviteCode(targetValue);
  if (inviteCode) {
    try {
      const inviteInfo = await activeClient.getInviteInfo(inviteCode);
      const groupId = inviteInfoGroupId(inviteInfo);
      if (groupId) {
        const inviteChat = await activeClient.getChatById(groupId);
        if (inviteChat && inviteChat.isGroup) return inviteChat;
      }
    } catch (_) {
      // fall through to chat list resolution
    }
  }
  const chats = await activeClient.getChats();
  const exact = chats.find((chat) => chat.isGroup && String(chat.name || '').trim() === targetValue);
  if (exact) return exact;
  const fuzzy = chats.find((chat) => chat.isGroup && String(chat.name || '').includes(targetValue));
  if (fuzzy) return fuzzy;
  if (inviteCode) {
    const inviteByCode = chats.find((chat) => chat.isGroup && String(chat.groupInviteCode || '').trim() === inviteCode);
    if (inviteByCode) return inviteByCode;
  }
  throw new Error(`group not found: ${targetValue}`);
}

async function resolveGroup(target) {
  return resolveGroupWithClient(client, target);
}

async function resolveApprovalGroup(target) {
  return resolveGroupWithClient(approvalClient, target);
}

async function getRequestEnrichedWithClient(activeClient, group) {
  const requests = await group.getGroupMembershipRequests();
  const requesterIds = requests.map((item) => safeString(item && item.id));
  const contacts = await Promise.all(
    requesterIds.map(async (requesterId) => {
      if (!requesterId) return null;
      try {
        return await activeClient.getContactById(requesterId);
      } catch (_) {
        return null;
      }
    }),
  );
  let lidPhoneMap = new Map();
  if (activeClient && typeof activeClient.getContactLidAndPhone === 'function') {
    try {
      const lidPhoneRows = await activeClient.getContactLidAndPhone(requesterIds.filter(Boolean));
      lidPhoneMap = new Map(
        (Array.isArray(lidPhoneRows) ? lidPhoneRows : []).map((item) => [
          safeString(item && item.lid),
          safeString(item && item.pn),
        ]),
      );
    } catch (_) {
      lidPhoneMap = new Map();
    }
  }

  return requests.map((item, index) => {
    const requesterId = requesterIds[index] || '';
    const contact = contacts[index] || null;
    const contactNumberRaw = (safeString(contact && contact.number) || '').replace(/^\+/, '');
    const lidPhoneRaw = (safeString(lidPhoneMap.get(requesterId)) || '').replace(/@(c|lid)\.us$/, '').replace(/^\+/, '');
    const contactNumberMasked = /\*/.test(contactNumberRaw);
    const requesterDigits = requesterId ? requesterId.replace(/@(c|lid)\.us$/, '').replace(/@lid$/, '') : '';
    const contactNumberLooksLikeLid = Boolean(requesterId.endsWith('@lid') && contactNumberRaw && contactNumberRaw === requesterDigits);
    const phone = ((!contactNumberMasked && !contactNumberLooksLikeLid && contactNumberRaw) || lidPhoneRaw || contactNumberRaw || requesterDigits || null);
    const displayName = contact ? (contact.pushname || contact.name || contact.shortName || null) : null;

    return {
      requesterId,
      phoneRaw: phone ? `+${phone}` : null,
      phoneNormalized: phone ? `+${phone}` : null,
      displayName,
      requestMethod: item && item.requestMethod ? item.requestMethod : null,
      requestedAtUnix: item && item.t ? item.t : null,
      requestedAtIso: item && item.t ? new Date(item.t * 1000).toISOString() : null,
      debugContactNumberRaw: contactNumberRaw || null,
      debugLidPhoneRaw: lidPhoneRaw || null,
      debugContactNumberMasked: contactNumberMasked,
    };
  });
}

async function getRequestEnriched(group) {
  return getRequestEnrichedWithClient(client, group);
}

async function getApprovalRequestEnriched(group) {
  return getRequestEnrichedWithClient(approvalClient, group);
}

function scoreRequest(entry, hints) {
  const normalizedHint = String(normalizePhone(hints.targetPhoneHint) || '').replace(/\D/g, '');
  const normalizedEntryPhone = String(
    normalizePhone(
      entry.debugLidPhoneRaw
      || entry.debugContactNumberRaw
      || entry.phoneNormalized
      || entry.phoneRaw
      || ''
    )
  ).replace(/\D/g, '');
  const targetNameHint = String(hints.targetNameHint || '').trim().toLowerCase();
  const displayName = String(entry.displayName || '').trim().toLowerCase();
  const phoneExactMatch = Boolean(normalizedHint && normalizedEntryPhone && normalizedHint === normalizedEntryPhone);
  const nameExactMatch = Boolean(targetNameHint && displayName && displayName === targetNameHint);
  const nameContainsMatch = Boolean(targetNameHint && displayName && displayName.includes(targetNameHint));
  let score = 0;
  if (phoneExactMatch) score += 100;
  if (nameExactMatch) score += 60;
  if (nameContainsMatch) score += 20;
  return {
    score,
    phoneExactMatch,
    nameExactMatch,
    nameContainsMatch,
  };
}

function selectRequests(requests, context) {
  const approvedCount = Math.max(1, Number(context.approved_count || 1));
  const hasPhoneHint = Boolean(normalizePhone(context.target_phone_hint));
  const hasNameHint = Boolean(String(context.target_name_hint || '').trim());
  const hasIdentityHints = hasPhoneHint || hasNameHint;
  const ranked = requests
    .map((entry, index) => {
      const match = scoreRequest(entry, {
        targetPhoneHint: context.target_phone_hint,
        targetNameHint: context.target_name_hint,
      });
      return { entry, index, ...match };
    })
    .sort((a, b) => b.score - a.score || a.index - b.index);
  if (ranked.length === 0) return [];
  if (!hasIdentityHints) {
    return ranked.slice(0, approvedCount);
  }
  if (hasPhoneHint) {
    return ranked.filter((item) => item.phoneExactMatch).slice(0, approvedCount);
  }
  return ranked.filter((item) => item.nameExactMatch).slice(0, approvedCount);
}

async function buildGroupStateFromGroup(context, group) {
  const groupId = safeString(group.id);
  const groupName = group.name || context.registration_group;
  const memberCount = Array.isArray(group.participants) ? group.participants.length : null;
  const requests = await getApprovalRequestEnriched(group);
  return {
    group_id: groupId,
    group_name: groupName,
    pending_count: requests.length,
    member_count: memberCount,
    requester_ids: requests.map((row) => row.requesterId).filter(Boolean),
    requesters: requests,
  };
}

async function groupState(context) {
  if (!approvalState.ready) {
    throw new Error(approvalState.last_qr ? 'approval client awaiting qr scan' : 'approval client is not ready');
  }
  const group = await resolveApprovalGroup(context.registration_group);
  return buildGroupStateFromGroup(context, group);
}

async function probeGroupState(context) {
  if (!state.ready) {
    throw new Error(state.last_qr ? 'probe client awaiting qr scan' : 'probe client is not ready');
  }
  const group = await resolveGroup(context.registration_group);
  return buildGroupStateFromGroup(context, group);
}

async function groupStateWithRecovery(context) {
  try {
    return await groupState(context);
  } catch (error) {
    if (isRecoverableApprovalClientError(error)) {
      const group = await reloadApprovalGroupFromFreshSession(context, error && error.stack ? error.stack : error);
      return await buildGroupStateFromGroup(context, group);
    }
    throw error;
  }
}

async function probeGroupStateWithRecovery(context) {
  try {
    return await probeGroupState(context);
  } catch (error) {
    if (isRecoverableClientError(error)) {
      const group = await reloadGroupFromFreshSession(context, error && error.stack ? error.stack : error);
      return await buildGroupStateFromGroup(context, group);
    }
    throw error;
  }
}

async function reloadGroupFromFreshSession(context, reason) {
  await resetClientSession(reason || 'forced_group_reload');
  await ensureClientStarted();
  await waitForReady(QR_TIMEOUT_MS).catch(() => {
    if (!state.ready) {
      throw new Error(state.last_qr ? 'client awaiting qr scan after reset' : 'client not ready after reset');
    }
  });
  return resolveGroup(context.registration_group);
}

async function reloadApprovalGroupFromFreshSession(context, reason) {
  await resetApprovalClientSession(reason || 'forced_approval_group_reload');
  await ensureApprovalClientStarted();
  await waitForApprovalReady(QR_TIMEOUT_MS).catch(() => {
    if (!approvalState.ready) {
      throw new Error(approvalState.last_qr ? 'approval client awaiting qr scan after reset' : 'approval client not ready after reset');
    }
  });
  return resolveApprovalGroup(context.registration_group);
}

function isApprovalResultSuccess(item) {
  return Boolean(item && (!item.error || Number(item.error) === 409));
}

function mergeApprovalResults(existingResults, nextResults) {
  const merged = [];
  const byRequester = new Map();
  for (const item of [...(existingResults || []), ...(nextResults || [])]) {
    const requesterId = safeString(item && item.requesterId);
    if (!requesterId) continue;
    byRequester.set(requesterId, { ...item, requesterId });
  }
  for (const value of byRequester.values()) {
    merged.push(value);
  }
  return merged;
}

async function approveRequesterBatch({
  groupId,
  requesterIds,
  approvalRunId,
  registrationGroup,
  attemptLabel,
  startedAt,
  timeoutMs = APPROVAL_PER_REQUESTER_TIMEOUT_MS,
}) {
  const results = [];
  for (let index = 0; index < requesterIds.length; index += 1) {
    const requesterId = requesterIds[index];
    logEvent('approve_stage', {
      approval_run_id: approvalRunId || null,
      registration_group: registrationGroup || null,
      stage: 'approve_requester_started',
      attempt: attemptLabel,
      requester_id: requesterId,
      requester_index: index,
      batch_size: requesterIds.length,
      elapsed_seconds: Number(((Date.now() - startedAt) / 1000).toFixed(3)),
    });
    try {
      const response = await withTimeout(
        approvalClient.approveGroupMembershipRequests(groupId, {
          requesterIds: [requesterId],
          sleep: APPROVAL_PER_REQUESTER_SLEEP_MS,
        }),
        { timeoutMs, label: `approve_requester_${attemptLabel}_${index + 1}` },
      );
      const normalized = Array.isArray(response) && response.length > 0
        ? response.map((item) => ({ ...item, requesterId: safeString(item && item.requesterId) || requesterId }))
        : [{ requesterId, message: 'EmptyApprovalResult', error: -1 }];
      results.push(...normalized);
      logEvent('approve_stage', {
        approval_run_id: approvalRunId || null,
        registration_group: registrationGroup || null,
        stage: 'approve_requester_finished',
        attempt: attemptLabel,
        requester_id: requesterId,
        requester_index: index,
        batch_size: requesterIds.length,
        result: normalized[0] || null,
        elapsed_seconds: Number(((Date.now() - startedAt) / 1000).toFixed(3)),
      });
    } catch (error) {
      const failure = {
        requesterId,
        error: -1,
        message: String(error && error.message ? error.message : error || 'unknown_error'),
      };
      results.push(failure);
      logEvent('approve_stage', {
        approval_run_id: approvalRunId || null,
        registration_group: registrationGroup || null,
        stage: 'approve_requester_failed',
        attempt: attemptLabel,
        requester_id: requesterId,
        requester_index: index,
        batch_size: requesterIds.length,
        error: String(error && error.stack ? error.stack : error || 'unknown_error'),
        elapsed_seconds: Number(((Date.now() - startedAt) / 1000).toFixed(3)),
      });
      error.partialResults = results;
      error.failedRequesterId = requesterId;
      throw error;
    }
  }
  return results;
}

async function approveMembershipRequests(context) {
  if (!approvalState.ready) {
    throw new Error(approvalState.last_qr ? 'approval client awaiting qr scan' : 'approval client is not ready');
  }
  let group = await resolveApprovalGroup(context.registration_group);
  let groupId = safeString(group.id);
  let groupName = group.name || context.registration_group;
  let memberCountBefore = Array.isArray(group.participants) ? group.participants.length : null;
  let requestsBefore = await getApprovalRequestEnriched(group);
  let pendingBefore = requestsBefore.length;
  let selected = selectRequests(requestsBefore, context);

  if (pendingBefore <= 0 || selected.length <= 0) {
    logEvent('approve_stage', {
      approval_run_id: context.approval_run_id || null,
      registration_group: context.registration_group || null,
      stage: 'initial_empty_queue_detected',
      pending_before: pendingBefore,
      member_count_before: memberCountBefore,
      requester_ids: requestsBefore.map((row) => row.requesterId).filter(Boolean),
    });
    group = await reloadApprovalGroupFromFreshSession(context, 'empty_start_snapshot_recheck');
    groupId = safeString(group.id);
    groupName = group.name || context.registration_group;
    memberCountBefore = Array.isArray(group.participants) ? group.participants.length : null;
    requestsBefore = await getApprovalRequestEnriched(group);
    pendingBefore = requestsBefore.length;
    selected = selectRequests(requestsBefore, context);
    logEvent('approve_stage', {
      approval_run_id: context.approval_run_id || null,
      registration_group: context.registration_group || null,
      stage: 'empty_queue_recheck_finished',
      pending_before: pendingBefore,
      member_count_before: memberCountBefore,
      requester_ids: requestsBefore.map((row) => row.requesterId).filter(Boolean),
    });
  }

  if (pendingBefore <= 0 || selected.length <= 0) {
    return {
      status: 'failed',
      verified: false,
      result_code: 'no_pending_request',
      result_reason: 'no pending request in registration group',
      approved_count: Number(context.approved_count || 1),
      elapsed_seconds: 0,
      target_member: {},
      raw_result: {
        approval_run_id: context.approval_run_id || null,
        start_snapshot: {
          pending_count: pendingBefore,
          member_count: memberCountBefore,
          pending_candidates: {
            phones: requestsBefore.map((row) => row.phoneRaw).filter(Boolean),
            requesters: requestsBefore.map((row) => row.displayName).filter(Boolean),
          },
        },
        selected_candidates: [],
        group_id: groupId,
        group_name: groupName,
        empty_queue_rechecked: true,
      },
    };
  }

  const requesterIds = selected.map((item) => item.entry.requesterId).filter(Boolean);
  const targetMember = selected[0] ? {
    name: selected[0].entry.displayName || null,
    phone_raw: selected[0].entry.phoneRaw || null,
    phone_normalized: selected[0].entry.phoneNormalized || null,
    requester_id: selected[0].entry.requesterId || null,
  } : {};

  const startedAt = Date.now();
  logEvent('approve_stage', {
    approval_run_id: context.approval_run_id || null,
    registration_group: context.registration_group || null,
    stage: 'selected_requesters',
    pending_before: pendingBefore,
    member_count_before: memberCountBefore,
    requester_ids: requesterIds,
  });
  let approvalResults = [];
  let approvalAttempt = 'per_requester_primary';
  try {
    logEvent('approve_stage', {
      approval_run_id: context.approval_run_id || null,
      registration_group: context.registration_group || null,
      stage: 'approve_call_started',
      attempt: approvalAttempt,
      requester_ids: requesterIds,
    });
    approvalResults = await approveRequesterBatch({
      groupId,
      requesterIds,
      approvalRunId: context.approval_run_id || null,
      registrationGroup: context.registration_group || null,
      attemptLabel: approvalAttempt,
      startedAt,
    });
    logEvent('approve_stage', {
      approval_run_id: context.approval_run_id || null,
      registration_group: context.registration_group || null,
      stage: 'approve_call_finished',
      attempt: approvalAttempt,
      requester_ids: requesterIds,
      elapsed_seconds: Number(((Date.now() - startedAt) / 1000).toFixed(3)),
    });
  } catch (error) {
    const partialResults = Array.isArray(error && error.partialResults) ? error.partialResults : [];
    approvalResults = mergeApprovalResults(approvalResults, partialResults);
    const completedRequesterIds = new Set(
      approvalResults.filter((item) => isApprovalResultSuccess(item)).map((item) => safeString(item.requesterId)).filter(Boolean),
    );
    const remainingRequesterIds = requesterIds.filter((requesterId) => !completedRequesterIds.has(requesterId));
    approvalAttempt = 'per_requester_retry_after_client_reset';
    logEvent('approve_stage', {
      approval_run_id: context.approval_run_id || null,
      registration_group: context.registration_group || null,
      stage: 'primary_attempt_failed',
      attempt: 'per_requester_primary',
      requester_ids: requesterIds,
      remaining_requester_ids: remainingRequesterIds,
      error: String(error && error.stack ? error.stack : error || 'unknown_error'),
      elapsed_seconds: Number(((Date.now() - startedAt) / 1000).toFixed(3)),
    });
    if (remainingRequesterIds.length > 0) {
      const retryGroup = await reloadApprovalGroupFromFreshSession(context, error && error.stack ? error.stack : error);
      groupId = safeString(retryGroup.id) || groupId;
      groupName = retryGroup.name || groupName;
      logEvent('approve_stage', {
        approval_run_id: context.approval_run_id || null,
        registration_group: context.registration_group || null,
        stage: 'client_reset_finished',
        attempt: approvalAttempt,
        requester_ids: remainingRequesterIds,
        elapsed_seconds: Number(((Date.now() - startedAt) / 1000).toFixed(3)),
      });
      logEvent('approve_stage', {
        approval_run_id: context.approval_run_id || null,
        registration_group: context.registration_group || null,
        stage: 'approve_call_started',
        attempt: approvalAttempt,
        requester_ids: remainingRequesterIds,
        elapsed_seconds: Number(((Date.now() - startedAt) / 1000).toFixed(3)),
      });
      const retryResults = await approveRequesterBatch({
        groupId,
        requesterIds: remainingRequesterIds,
        approvalRunId: context.approval_run_id || null,
        registrationGroup: context.registration_group || null,
        attemptLabel: approvalAttempt,
        startedAt,
      });
      approvalResults = mergeApprovalResults(approvalResults, retryResults);
      logEvent('approve_stage', {
        approval_run_id: context.approval_run_id || null,
        registration_group: context.registration_group || null,
        stage: 'approve_call_finished',
        attempt: approvalAttempt,
        requester_ids: remainingRequesterIds,
        elapsed_seconds: Number(((Date.now() - startedAt) / 1000).toFixed(3)),
      });
    }
  }

  let requestsAfter = [];
  let memberCountAfter = memberCountBefore;
  for (let attempt = 0; attempt < APPROVAL_VERIFY_RETRIES; attempt += 1) {
    await sleep(APPROVAL_VERIFY_WAIT_MS);
    const refreshedGroup = await approvalClient.getChatById(groupId);
    requestsAfter = await refreshedGroup.getGroupMembershipRequests();
    memberCountAfter = Array.isArray(refreshedGroup.participants) ? refreshedGroup.participants.length : memberCountAfter;
    if (requestsAfter.length < pendingBefore) {
      break;
    }
  }

  const pendingAfter = Array.isArray(requestsAfter) ? requestsAfter.length : null;
  const approvedSucceeded = Array.isArray(approvalResults) ? approvalResults.filter((item) => !item.error || Number(item.error) === 409).length : 0;
  const queueDelta = pendingAfter !== null ? pendingAfter < pendingBefore : false;
  const memberCountDelta = memberCountBefore !== null && memberCountAfter !== null ? memberCountAfter - memberCountBefore : null;
  const verified = Boolean(queueDelta || (memberCountDelta !== null && memberCountDelta >= approvedSucceeded && approvedSucceeded > 0));

  return {
    status: verified ? 'success' : 'failed',
    verified,
    result_code: verified ? 'approved' : 'approval_not_verified',
    result_reason: verified ? 'membership request approved via whatsapp-web.js bridge' : 'approval call returned but strict queue verification did not converge',
    approved_count: approvedSucceeded || requesterIds.length || Number(context.approved_count || 1),
    approved_at: new Date().toISOString(),
    elapsed_seconds: Number(((Date.now() - startedAt) / 1000).toFixed(3)),
    queue_delta: queueDelta,
    member_confirmed: memberCountDelta !== null ? memberCountDelta >= 1 : false,
    target_member: targetMember,
    raw_result: {
      approval_run_id: context.approval_run_id || null,
      start_snapshot: {
        pending_count: pendingBefore,
        member_count: memberCountBefore,
        pending_candidates: {
          phones: requestsBefore.map((row) => row.phoneRaw).filter(Boolean),
          requesters: requestsBefore.map((row) => row.displayName).filter(Boolean),
        },
      },
      pending_before: pendingBefore,
      pending_after: pendingAfter,
      member_count_before: memberCountBefore,
      member_count_after: memberCountAfter,
      selected_candidates: selected.map(({ entry, score }) => ({ ...entry, score })),
      approval_results: approvalResults,
      approval_attempt: approvalAttempt,
      group_id: groupId,
      group_name: groupName,
      verification_strategy: 'queue_delta_or_member_count_delta',
    },
  };
}

function sendJson(res, statusCode, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
  });
  res.end(body);
}

function collectJson(req) {
  return new Promise((resolve, reject) => {
    let raw = '';
    req.on('data', (chunk) => {
      raw += chunk;
      if (raw.length > 1024 * 1024) {
        reject(new Error('request body too large'));
        req.destroy();
      }
    });
    req.on('end', () => {
      if (!raw) {
        resolve({});
        return;
      }
      try {
        resolve(JSON.parse(raw));
      } catch (err) {
        reject(err);
      }
    });
    req.on('error', reject);
  });
}

function buildHealthPayload() {
  return {
    configured: true,
    ...state,
    approval_client: { ...approvalState },
    hostname: os.hostname(),
  };
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.method === 'GET' && req.url === '/health') {
      sendJson(res, 200, buildHealthPayload());
      return;
    }

    if (req.method === 'POST' && req.url === '/warmup') {
      await ensureClientStarted();
      await ensureApprovalClientStarted();
      let outcome = { kind: 'initializing' };
      let approvalOutcome = { kind: 'initializing' };
      try {
        outcome = await waitForQrOrReady(QR_TIMEOUT_MS);
      } catch (error) {
        updateState({ last_error: String(error && error.message ? error.message : error) });
      }
      try {
        approvalOutcome = await waitForApprovalQrOrReady(QR_TIMEOUT_MS);
      } catch (error) {
        updateApprovalState({ last_error: String(error && error.message ? error.message : error) });
      }
      state.last_action_at = new Date().toISOString();
      approvalState.last_action_at = new Date().toISOString();
      sendJson(res, 200, {
        ...buildHealthPayload(),
        warmup_outcome: outcome.kind,
        qr_available: Boolean(state.last_qr),
        approval_warmup_outcome: approvalOutcome.kind,
        approval_qr_available: Boolean(approvalState.last_qr),
      });
      return;
    }

    if (req.method === 'POST' && req.url === '/group-state') {
      const payload = await collectJson(req);
      state.last_action_at = new Date().toISOString();
      approvalState.last_action_at = new Date().toISOString();
      const result = await withActionLock(async () => {
        await ensureApprovalClientStarted();
        await waitForApprovalReady(QR_TIMEOUT_MS).catch(() => {
          if (!approvalState.ready) {
            throw new Error(approvalState.last_qr ? 'approval client awaiting qr scan' : 'approval client not ready');
          }
        });
        return await groupStateWithRecovery(payload);
      });
      logEvent('group_state', {
        registration_group: payload.registration_group || null,
        auth_strategy: approvalState.auth_strategy,
        group_id: result.group_id || null,
        group_name: result.group_name || payload.registration_group || null,
        pending_count: result.pending_count,
        member_count: result.member_count,
        requester_ids: result.requester_ids || [],
      });
      sendJson(res, 200, result);
      return;
    }

    if (req.method === 'POST' && req.url === '/probe-group-state') {
      const payload = await collectJson(req);
      state.last_action_at = new Date().toISOString();
      const result = await withActionLock(async () => {
        await ensureClientStarted();
        await waitForReady(QR_TIMEOUT_MS).catch(() => {
          if (!state.ready) {
            throw new Error(state.last_qr ? 'probe client awaiting qr scan' : 'probe client not ready');
          }
        });
        return await probeGroupStateWithRecovery(payload);
      });
      logEvent('probe_group_state', {
        registration_group: payload.registration_group || null,
        auth_strategy: state.auth_strategy,
        group_id: result.group_id || null,
        group_name: result.group_name || payload.registration_group || null,
        pending_count: result.pending_count,
        member_count: result.member_count,
        requester_ids: result.requester_ids || [],
      });
      sendJson(res, 200, result);
      return;
    }

    if (req.method === 'POST' && req.url === '/approve') {
      const payload = await collectJson(req);
      state.last_action_at = new Date().toISOString();
      approvalState.last_action_at = new Date().toISOString();
      const result = await withActionLock(async () => {
        await ensureApprovalClientStarted();
        await waitForApprovalReady(QR_TIMEOUT_MS).catch(() => {
          if (!approvalState.ready) {
            throw new Error(approvalState.last_qr ? 'approval client awaiting qr scan' : 'approval client not ready');
          }
        });
        const approvalRunId = payload && payload.approval_run_id ? String(payload.approval_run_id).trim() : '';
        return approvalRunStore.run(approvalRunId, async () => approveMembershipRequests(payload));
      });
      const rawResult = result.raw_result || {};
      const targetMember = result.target_member || {};
      logEvent('approve', {
        approval_run_id: rawResult.approval_run_id || payload.approval_run_id || null,
        registration_group: payload.registration_group || null,
        auth_strategy: approvalState.auth_strategy,
        result_code: result.result_code || null,
        verified: Boolean(result.verified),
        approved_count: result.approved_count,
        elapsed_seconds: result.elapsed_seconds,
        pending_before: rawResult.pending_before ?? (rawResult.start_snapshot || {}).pending_count ?? null,
        pending_after: rawResult.pending_after ?? null,
        member_count_before: rawResult.member_count_before ?? (rawResult.start_snapshot || {}).member_count ?? null,
        member_count_after: rawResult.member_count_after ?? null,
        requester_ids: (rawResult.selected_candidates || []).map((row) => row.requesterId).filter(Boolean),
        target_requester_id: targetMember.requester_id || null,
      });
      if (POST_APPROVE_PROBE_REFRESH_ENABLED) {
        scheduleProbeClientRefresh(`post_approve:${rawResult.approval_run_id || payload.approval_run_id || 'unknown'}`);
      } else {
        logEvent('probe_client_refresh_skipped', {
          reason: `post_approve:${rawResult.approval_run_id || payload.approval_run_id || 'unknown'}`,
        });
      }
      sendJson(res, 200, result);
      return;
    }

    sendJson(res, 404, {
      status: 'failed',
      result_code: 'not_found',
      result_reason: `Route not found: ${req.method} ${req.url}`,
    });
  } catch (error) {
    const reason = String(error && error.stack ? error.stack : error);
    updateState({ last_error: reason });
    logEvent('worker_error', {
      auth_strategy: state.auth_strategy,
      result_code: 'bridge_internal_error',
      result_reason: String(error && error.message ? error.message : error),
      route: req && req.url ? req.url : null,
      method: req && req.method ? req.method : null,
    });
    sendJson(res, 500, {
      status: 'failed',
      result_code: 'bridge_internal_error',
      result_reason: String(error && error.message ? error.message : error),
      raw_result: {
        execution_disposition: 'failed',
      },
    });
  }
});

if (require.main === module) {
  server.listen(PORT, HOST, () => {
    const startupRecord = {
      status: 'listening',
      host: HOST,
      port: PORT,
      provider: state.provider,
      mode: state.mode,
      auth_strategy: state.auth_strategy,
      chrome_profile_source: state.chrome_profile_source || null,
      event_log: WORKER_EVENT_LOG,
    };
    logEvent('startup', startupRecord);
    console.log(JSON.stringify(startupRecord));
  });
}

module.exports = {
  normalizePhone,
  scoreRequest,
  selectRequests,
  getRequestEnrichedWithClient,
  resolveLocalAuthSessionDir,
  parseChromeProcessesUsingUserDataDir,
  recoverLocalAuthBrowserConflict,
  runBrowserConflictRecoveryFlow,
};

process.on('exit', () => {
  cleanupRuntimeChromeUserDataDir('probe');
  cleanupRuntimeChromeUserDataDir('approval');
});
process.on('SIGINT', () => {
  cleanupRuntimeChromeUserDataDir('probe');
  cleanupRuntimeChromeUserDataDir('approval');
  process.exit(0);
});
process.on('SIGTERM', () => {
  cleanupRuntimeChromeUserDataDir('probe');
  cleanupRuntimeChromeUserDataDir('approval');
  process.exit(0);
});
