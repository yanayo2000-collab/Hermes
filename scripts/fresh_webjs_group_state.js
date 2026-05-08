#!/usr/bin/env node

const registrationGroup = String(process.argv[2] || '').trim();
const explicitWorkerBaseUrl = String(process.argv[3] || '').trim();
const apiBaseUrl = String(process.env.PRODUCTION_OPS_API_BASE_URL || 'http://127.0.0.1:8011').trim();

if (!registrationGroup) {
  console.error('usage: fresh_webjs_group_state.js <registration_group> [worker_base_url]');
  process.exit(2);
}

function normalizeBaseUrl(value) {
  const raw = String(value || '').trim();
  return raw ? raw.replace(/\/$/, '') : '';
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
  });
  const text = await response.text();
  let payload;
  try {
    payload = text ? JSON.parse(text) : {};
  } catch (error) {
    throw new Error(`invalid JSON from ${url}: ${text || '<empty>'}`);
  }
  if (!response.ok) {
    const reason = payload && payload.result_reason ? payload.result_reason : `${response.status} ${response.statusText}`;
    throw new Error(reason);
  }
  return payload;
}

function bindingMatches(binding, targetValue) {
  if (!binding || typeof binding !== 'object') return false;
  const candidates = [
    binding.link,
    binding.group_id,
    binding.registration_group,
    binding.group_name,
  ].map((item) => String(item || '').trim()).filter(Boolean);
  return candidates.includes(targetValue);
}

async function resolveWorkerBaseUrl(targetValue) {
  const envCandidates = [
    explicitWorkerBaseUrl,
    process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_BASE_URL,
    process.env.REGISTRATION_GROUP_APPROVAL_WEBJS_WORKER_BASE_URL,
    process.env.PRODUCTION_OPS_WORKER_BASE_URL,
  ].map(normalizeBaseUrl).filter(Boolean);
  if (envCandidates.length > 0) {
    return envCandidates[0];
  }

  try {
    const daemon = await fetchJson(`${normalizeBaseUrl(apiBaseUrl)}/api/ops/production-ops-daemon`);
    const runtimeStatus = (((daemon || {}).runtime || {}).status) || {};
    const monitorTarget = runtimeStatus.monitor_target || {};
    const daemonCandidates = [
      runtimeStatus.registration_group,
      monitorTarget.registration_group,
      monitorTarget.binding_link,
      monitorTarget.group_name,
      monitorTarget.binding_group_name,
    ].map((item) => String(item || '').trim()).filter(Boolean);
    const workerBaseUrl = normalizeBaseUrl(monitorTarget.worker_base_url);
    if (workerBaseUrl && daemonCandidates.includes(targetValue)) {
      return workerBaseUrl;
    }
  } catch (_) {}

  const accounts = await fetchJson(`${normalizeBaseUrl(apiBaseUrl)}/api/ops/whatsapp-approval-accounts`);
  const rows = Array.isArray(accounts.rows) ? accounts.rows : [];
  for (const row of rows) {
    if (!row || row.enabled === false) continue;
    const runtime = row.runtime_state || {};
    const workerBaseUrl = normalizeBaseUrl(runtime.base_url);
    if (!workerBaseUrl || runtime.active !== true) continue;
    const bindings = Array.isArray(row.group_link_bindings) ? row.group_link_bindings : [];
    if (bindings.some((binding) => binding && binding.enabled !== false && bindingMatches(binding, targetValue))) {
      return workerBaseUrl;
    }
  }

  throw new Error(`worker base url not found for ${targetValue}`);
}

(async () => {
  try {
    const workerBaseUrl = await resolveWorkerBaseUrl(registrationGroup);
    const payload = await fetchJson(`${workerBaseUrl}/probe-group-state`, {
      method: 'POST',
      body: JSON.stringify({ registration_group: registrationGroup }),
    });
    console.log(JSON.stringify(payload, null, 2));
  } catch (error) {
    console.error(String(error && error.stack ? error.stack : error));
    process.exit(1);
  }
})();
