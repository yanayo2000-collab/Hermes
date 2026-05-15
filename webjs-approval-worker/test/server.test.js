const test = require('node:test');
const assert = require('node:assert/strict');

const {
  buildAuthoritativeGroupState,
  buildGroupStateFromGroup,
  normalizeGroupStateMode,
  resolveAuthoritativeGroupState,
} = require('../src/server');

test('buildAuthoritativeGroupState keeps positive group-state requester ids as the only authoritative evidence', () => {
  const result = buildAuthoritativeGroupState({
    group_id: '120@g.us',
    group_name: '注册测试1',
    pending_count: 3,
    member_count: 8,
    requester_ids: ['a', 'b', 'c'],
    requesters: [{ requesterId: 'a' }, { requesterId: 'b' }, { requesterId: 'c' }],
  });

  assert.equal(result.pending_count, 3);
  assert.equal(result.source_layer, 'group_object');
  assert.equal(result.verification_stage, 'direct');
  assert.equal(result.approval_state_status, 'confirmed_pending');
  assert.equal(result.zero_pending_unverified, false);
  assert.deepEqual(result.requester_ids, ['a', 'b', 'c']);
});

test('buildAuthoritativeGroupState treats a single zero group-state read as unverified empty', () => {
  const result = buildAuthoritativeGroupState({
    group_id: '120@g.us',
    group_name: '注册测试1',
    pending_count: 0,
    member_count: 5,
    requester_ids: [],
    requesters: [],
  });

  assert.equal(result.pending_count, 0);
  assert.equal(result.approval_state_status, 'unverified_empty');
  assert.equal(result.zero_pending_unverified, true);
  assert.equal(result.zero_pending_unverified_reason, 'group_state_zero_not_stable');
  assert.equal(result.pending_zero_confidence, 'unverified');
});

test('resolveAuthoritativeGroupState confirms pending immediately when any refreshed read finds requesters', async () => {
  const reads = [
    {
      group_id: '120@g.us',
      group_name: '注册测试1',
      pending_count: 0,
      member_count: 5,
      requester_ids: [],
      requesters: [],
    },
    {
      group_id: '120@g.us',
      group_name: '注册测试1',
      pending_count: 2,
      member_count: 5,
      requester_ids: ['r1', 'r2'],
      requesters: [{ requesterId: 'r1' }, { requesterId: 'r2' }],
    },
  ];
  const waits = [];
  const reasons = [];

  const result = await resolveAuthoritativeGroupState({ registration_group: '120@g.us' }, {
    stateLoader: async (meta) => {
      reasons.push(meta.reason);
      return reads.shift();
    },
    waitFn: async (ms) => waits.push(ms),
    zeroPendingStableReadCount: 3,
    zeroPendingRetryWaitMs: 5,
  });

  assert.equal(result.pending_count, 2);
  assert.equal(result.approval_state_status, 'confirmed_pending');
  assert.equal(result.zero_pending_unverified, false);
  assert.deepEqual(result.requester_ids, ['r1', 'r2']);
  assert.equal(result.zero_pending_recheck_attempted, true);
  assert.equal(result.zero_pending_recheck_resolved, true);
  assert.equal(result.zero_pending_recheck_count, 1);
  assert.deepEqual(waits, [5]);
  assert.deepEqual(reasons, ['initial_group_state_read', 'stable_zero_recheck_2']);
});

test('resolveAuthoritativeGroupState confirms empty only after three stable refreshed zero reads', async () => {
  const waits = [];
  const attempts = [];
  const result = await resolveAuthoritativeGroupState({ registration_group: '120@g.us' }, {
    stateLoader: async (meta) => {
      attempts.push(meta.attempt);
      return {
        group_id: '120@g.us',
        group_name: '注册测试1',
        pending_count: 0,
        member_count: 5,
        requester_ids: [],
        requesters: [],
      };
    },
    waitFn: async (ms) => waits.push(ms),
    zeroPendingStableReadCount: 3,
    zeroPendingRetryWaitMs: 7,
  });

  assert.equal(result.pending_count, 0);
  assert.equal(result.approval_state_status, 'confirmed_empty');
  assert.equal(result.zero_pending_unverified, false);
  assert.equal(result.zero_pending_verified_by, 'consecutive_group_state_refresh');
  assert.equal(result.pending_zero_confidence, 'confirmed');
  assert.equal(result.zero_pending_recheck_attempted, true);
  assert.equal(result.zero_pending_recheck_resolved, true);
  assert.equal(result.zero_pending_recheck_count, 2);
  assert.deepEqual(waits, [7, 7]);
  assert.deepEqual(attempts, [1, 2, 3]);
});

test('resolveAuthoritativeGroupState keeps zero unverified when stable zero reads are interrupted', async () => {
  const result = await resolveAuthoritativeGroupState({ registration_group: '120@g.us' }, {
    stateLoader: async (meta) => {
      if (meta.attempt === 2) throw new Error('transient_refresh_failure');
      return {
        group_id: '120@g.us',
        group_name: '注册测试1',
        pending_count: 0,
        member_count: 5,
        requester_ids: [],
        requesters: [],
      };
    },
    waitFn: async () => {},
    zeroPendingStableReadCount: 3,
    zeroPendingRetryWaitMs: 1,
  });

  assert.equal(result.pending_count, 0);
  assert.equal(result.approval_state_status, 'unverified_empty');
  assert.equal(result.zero_pending_unverified, true);
  assert.equal(result.zero_pending_unverified_reason, 'group_state_zero_recheck_failed');
  assert.equal(result.zero_pending_recheck_resolved, false);
  assert.equal(result.zero_pending_rechecks[0].error, 'transient_refresh_failure');
});

test('resolveAuthoritativeGroupState ignores review-surface diagnostics for automatic decisions', async () => {
  let reviewCalled = false;
  const result = await resolveAuthoritativeGroupState({ registration_group: '120@g.us' }, {
    stateLoader: async () => ({
      group_id: '120@g.us',
      group_name: '注册测试1',
      pending_count: 0,
      member_count: 5,
      requester_ids: [],
      requesters: [],
    }),
    reviewSurfaceLoader: async () => {
      reviewCalled = true;
      return {
        pending_count: 99,
        requesters: [{ displayName: 'stale row' }],
        has_pending_request_row: true,
      };
    },
    waitFn: async () => {},
    zeroPendingStableReadCount: 3,
    zeroPendingRetryWaitMs: 1,
  });

  assert.equal(reviewCalled, false);
  assert.equal(result.approval_state_status, 'confirmed_empty');
  assert.equal(result.pending_count, 0);
});


test('normalizeGroupStateMode defaults to full_verify and accepts fast mode only when explicit', () => {
  assert.equal(normalizeGroupStateMode({}), 'full_verify');
  assert.equal(normalizeGroupStateMode({ mode: 'fast' }), 'fast');
  assert.equal(normalizeGroupStateMode({ mode: 'full_verify' }), 'full_verify');
  assert.equal(normalizeGroupStateMode({ mode: 'unknown' }), 'full_verify');
});

test('buildGroupStateFromGroup fast mode skips contact enrichment but keeps lightweight refresh', async () => {
  let refreshCalled = false;
  let contactLookupCalled = false;
  const group = {
    id: '120@g.us',
    name: '注册测试1',
    participants: [{ id: 'p1' }, { id: 'p2' }],
    getGroupMembershipRequests: async () => ([
      { id: 'r1@lid', t: 1710000000, requestMethod: 'invite_link' },
      { id: 'r2@lid', t: 1710000060, requestMethod: 'invite_link' },
    ]),
  };

  const result = await buildGroupStateFromGroup({ registration_group: '注册测试1', mode: 'fast' }, group, {
    mode: 'fast',
    refreshFn: async () => {
      refreshCalled = true;
      return ['refresh'];
    },
    activeClient: {
      getContactById: async () => {
        contactLookupCalled = true;
        return null;
      },
    },
  });

  assert.equal(result.probe_mode, 'fast');
  assert.equal(result.pending_count, 2);
  assert.equal(result.member_count, null);
  assert.deepEqual(result.requester_ids, ['r1@lid', 'r2@lid']);
  assert.deepEqual(result.requesters, []);
  assert.equal(result.refresh_attempted, true);
  assert.equal(refreshCalled, true);
  assert.equal(contactLookupCalled, false);
  assert.ok(result.fingerprint);
});

test('buildGroupStateFromGroup full_verify keeps refresh and enrichment', async () => {
  let refreshCalled = false;
  const group = {
    id: '120@g.us',
    name: '注册测试1',
    participants: [{ id: 'p1' }, { id: 'p2' }, { id: 'p3' }],
    getGroupMembershipRequests: async () => ([
      { id: 'r1@lid', t: 1710000000, requestMethod: 'invite_link' },
    ]),
  };

  const result = await buildGroupStateFromGroup({ registration_group: '注册测试1', mode: 'full_verify' }, group, {
    mode: 'full_verify',
    refreshFn: async () => {
      refreshCalled = true;
      return ['refresh'];
    },
    activeClient: {
      getContactById: async (id) => ({ number: '628123456789', pushname: `name:${id}` }),
    },
  });

  assert.equal(result.probe_mode, 'full_verify');
  assert.equal(result.pending_count, 1);
  assert.equal(result.member_count, 3);
  assert.deepEqual(result.requester_ids, ['r1@lid']);
  assert.equal(result.requesters[0].displayName, 'name:r1@lid');
  assert.equal(result.refresh_attempted, true);
  assert.equal(refreshCalled, true);
});
