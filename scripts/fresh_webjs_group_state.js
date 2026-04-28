#!/usr/bin/env node
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');
const { Client, NoAuth } = require('../webjs-approval-worker/node_modules/whatsapp-web.js');

const registrationGroup = String(process.argv[2] || '').trim();
const profileDir = String(process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_CHROME_PROFILE_DIR || 'Profile 25').trim();
const chromeRoot = path.resolve(process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_CHROME_USER_DATA_ROOT || path.join(process.env.HOME, 'Library/Application Support/Google/Chrome'));

if (!registrationGroup) {
  console.error('usage: fresh_webjs_group_state.js <registration_group>');
  process.exit(2);
}

const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'webjs-fresh-state-'));
let finished = false;

function cleanup() {
  if (finished) return;
  finished = true;
  try { fs.rmSync(tmpRoot, { recursive: true, force: true }); } catch (_) {}
}

process.on('exit', cleanup);
process.on('SIGINT', () => { cleanup(); process.exit(130); });
process.on('SIGTERM', () => { cleanup(); process.exit(143); });

execFileSync('cp', ['-R', path.join(chromeRoot, 'Local State'), path.join(tmpRoot, 'Local State')]);
execFileSync('cp', ['-R', path.join(chromeRoot, profileDir), path.join(tmpRoot, profileDir)]);

const client = new Client({
  authStrategy: new NoAuth(),
  puppeteer: {
    headless: true,
    channel: 'chrome',
    userDataDir: tmpRoot,
    args: [`--profile-directory=${profileDir}`, '--no-sandbox', '--disable-setuid-sandbox'],
  },
});

let readyTimeout = null;

async function main() {
  const chats = await client.getChats();
  const group = chats.find((chat) => chat && chat.isGroup && String(chat.name || '').trim() === registrationGroup);
  if (!group) throw new Error(`group not found: ${registrationGroup}`);
  const requests = await group.getGroupMembershipRequests();
  const requesterIds = requests
    .map((item) => item && item.id && (item.id._serialized || item.id.user || String(item.id)))
    .filter(Boolean);
  const requesters = requests.map((item) => ({
    requesterId: item && item.id ? (item.id._serialized || item.id.user || String(item.id)) : null,
    requestedAtUnix: item && item.t ? Number(item.t) : null,
    requestedAtIso: item && item.t ? new Date(item.t * 1000).toISOString() : null,
  })).filter((row) => row.requesterId);
  const result = {
    group_id: group.id && group.id._serialized ? group.id._serialized : null,
    group_name: group.name || registrationGroup,
    pending_count: requests.length,
    member_count: Array.isArray(group.participants) ? group.participants.length : null,
    requester_ids: requesterIds,
    requesters,
  };
  console.log(JSON.stringify(result, null, 2));
}

client.on('ready', async () => {
  if (readyTimeout) {
    clearTimeout(readyTimeout);
    readyTimeout = null;
  }
  try {
    await main();
  } catch (error) {
    console.error(String(error && error.stack ? error.stack : error));
    process.exitCode = 1;
  } finally {
    try { await client.destroy(); } catch (_) {}
    cleanup();
  }
});

client.on('auth_failure', (msg) => {
  console.error(`auth_failure ${msg || ''}`.trim());
  process.exitCode = 1;
  cleanup();
  process.exit();
});

client.initialize();
readyTimeout = setTimeout(() => {
  console.error('timeout waiting ready');
  process.exit(2);
}, 90000);
