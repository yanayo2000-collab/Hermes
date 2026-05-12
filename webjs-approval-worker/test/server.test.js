const test = require('node:test');
const assert = require('node:assert/strict');

const {
  buildAuthoritativeGroupState,
  resolveAuthoritativeGroupState,
} = require('../src/server');

test('buildAuthoritativeGroupState keeps positive group object results as direct evidence', () => {
  const result = buildAuthoritativeGroupState({
    group_id: '120@g.us',
    group_name: '注册测试1',
    pending_count: 3,
    member_count: 8,
    requester_ids: ['a', 'b', 'c'],
    requesters: [{ requesterId: 'a' }, { requesterId: 'b' }, { requesterId: 'c' }],
  }, null);

  assert.equal(result.pending_count, 3);
  assert.equal(result.source_layer, 'group_object');
  assert.equal(result.verification_stage, 'direct');
  assert.equal(result.zero_pending_unverified, false);
});

test('buildAuthoritativeGroupState promotes positive review-surface evidence over zero group object results', () => {
  const result = buildAuthoritativeGroupState({
    group_id: '120@g.us',
    group_name: '注册测试1',
    pending_count: 0,
    member_count: 5,
    requester_ids: [],
    requesters: [],
  }, {
    group_id: '120@g.us',
    group_name: '注册测试1',
    pending_count: 2,
    member_count: 5,
    requesters: [{ displayName: 'Alice' }, { displayName: 'Bob' }],
    requester_ids: [],
    review_surface_ready: true,
    has_pending_section: true,
    has_pending_request_row: true,
    empty_queue_visible: false,
    source: 'approval_review_surface',
  });

  assert.equal(result.pending_count, 2);
  assert.deepEqual(result.requesters, [{ displayName: 'Alice' }, { displayName: 'Bob' }]);
  assert.equal(result.source_layer, 'review_surface');
  assert.equal(result.verification_stage, 'review_surface_recheck');
  assert.equal(result.source, 'approval_review_surface_positive_override');
});

test('buildAuthoritativeGroupState confirms zero only when explicit empty-queue evidence exists', () => {
  const result = buildAuthoritativeGroupState({
    group_id: '120@g.us',
    group_name: '注册测试1',
    pending_count: 0,
    member_count: 5,
    requester_ids: [],
    requesters: [],
  }, {
    group_id: '120@g.us',
    group_name: '注册测试1',
    pending_count: 0,
    member_count: 5,
    requesters: [],
    requester_ids: [],
    review_surface_ready: true,
    has_pending_section: false,
    has_pending_request_row: false,
    empty_queue_visible: true,
    source: 'approval_review_surface',
  });

  assert.equal(result.pending_count, 0);
  assert.equal(result.source_layer, 'review_surface');
  assert.equal(result.verification_stage, 'review_surface_recheck');
  assert.equal(result.zero_pending_unverified, false);
  assert.equal(result.zero_pending_verified_by, 'dedicated_runtime_review_surface');
  assert.equal(result.source, 'approval_review_surface_zero_confirmed');
});

test('buildAuthoritativeGroupState does not promote suspicious review-surface residue over zero group object results', () => {
  const result = buildAuthoritativeGroupState({
    group_id: '120@g.us',
    group_name: '注册测试1',
    pending_count: 0,
    member_count: 5,
    requester_ids: [],
    requesters: [],
  }, {
    group_id: '120@g.us',
    group_name: '注册测试1',
    pending_count: 2,
    member_count: 5,
    requesters: [{ displayName: '~Jackson' }, { displayName: '1' }],
    requester_ids: [],
    review_surface_ready: true,
    has_pending_section: false,
    has_pending_request_row: true,
    empty_queue_visible: false,
    source: 'approval_review_surface',
  });

  assert.equal(result.pending_count, 0);
  assert.equal(result.zero_pending_unverified, true);
  assert.equal(result.zero_pending_unverified_reason, 'review_surface_positive_not_authoritative');
  assert.equal(result.source, 'approval_review_surface_positive_suspected_residue');
  assert.equal(result.suspected_review_surface_residue, true);
});

test('buildAuthoritativeGroupState preserves ambiguous zero as unverified with review-surface evidence fields', () => {
  const result = buildAuthoritativeGroupState({
    group_id: '120@g.us',
    group_name: '注册测试1',
    pending_count: 0,
    member_count: 5,
    requester_ids: [],
    requesters: [],
  }, {
    group_id: '120@g.us',
    group_name: '注册测试1',
    pending_count: 0,
    member_count: 5,
    requesters: [],
    requester_ids: [],
    review_surface_ready: true,
    has_pending_section: false,
    has_pending_request_row: false,
    empty_queue_visible: false,
    source: 'approval_review_surface',
    zero_pending_unverified: true,
  });

  assert.equal(result.pending_count, 0);
  assert.equal(result.source_layer, 'review_surface');
  assert.equal(result.verification_stage, 'review_surface_recheck');
  assert.equal(result.zero_pending_unverified, true);
  assert.equal(result.zero_pending_unverified_reason, 'review_surface_zero_not_confirmed');
  assert.equal(result.source, 'approval_review_surface_zero_unconfirmed');
  assert.equal(result.review_surface_ready, true);
});

test('resolveAuthoritativeGroupState overturns ambiguous zero when a zero recheck finds pending requests', async () => {
  const statePayloads = [
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
  const reviewPayloads = [
    {
      group_id: '120@g.us',
      group_name: '注册测试1',
      pending_count: 0,
      member_count: 5,
      requesters: [],
      requester_ids: [],
      review_surface_ready: true,
      has_pending_section: false,
      has_pending_request_row: false,
      empty_queue_visible: false,
      source: 'approval_review_surface',
      zero_pending_unverified: true,
    },
    null,
  ];
  const reloadReasons = [];
  const waits = [];

  const result = await resolveAuthoritativeGroupState({ registration_group: '120@g.us' }, {
    stateLoader: async () => statePayloads.shift(),
    reviewSurfaceLoader: async () => reviewPayloads.shift(),
    freshStateLoader: async (reason) => {
      reloadReasons.push(reason);
      return statePayloads.shift();
    },
    waitFn: async (ms) => {
      waits.push(ms);
    },
    zeroPendingRetryCount: 2,
    zeroPendingRetryWaitMs: 5,
  });

  assert.equal(result.pending_count, 2);
  assert.equal(result.zero_pending_unverified, false);
  assert.equal(result.source_layer, 'group_object');
  assert.equal(result.verification_stage, 'direct');
  assert.equal(result.zero_pending_recheck_attempted, true);
  assert.equal(result.zero_pending_recheck_resolved, true);
  assert.equal(result.zero_pending_recheck_count, 1);
  assert.deepEqual(result.zero_pending_rechecks, [
    {
      attempt: 1,
      source: 'group_object',
      pending_count: 2,
      zero_pending_unverified: false,
      zero_pending_verified_by: null,
      error: null,
    },
  ]);
  assert.deepEqual(reloadReasons, ['authoritative_zero_pending_recheck_1']);
  assert.deepEqual(waits, [5]);
});

test('resolveAuthoritativeGroupState keeps ambiguous zero when all zero rechecks stay unconfirmed', async () => {
  const waits = [];
  const result = await resolveAuthoritativeGroupState({ registration_group: '120@g.us' }, {
    stateLoader: async () => ({
      group_id: '120@g.us',
      group_name: '注册测试1',
      pending_count: 0,
      member_count: 5,
      requester_ids: [],
      requesters: [],
    }),
    reviewSurfaceLoader: async () => ({
      group_id: '120@g.us',
      group_name: '注册测试1',
      pending_count: 0,
      member_count: 5,
      requesters: [],
      requester_ids: [],
      review_surface_ready: true,
      has_pending_section: false,
      has_pending_request_row: false,
      empty_queue_visible: false,
      source: 'approval_review_surface',
      zero_pending_unverified: true,
    }),
    freshStateLoader: async () => ({
      group_id: '120@g.us',
      group_name: '注册测试1',
      pending_count: 0,
      member_count: 5,
      requester_ids: [],
      requesters: [],
    }),
    waitFn: async (ms) => {
      waits.push(ms);
    },
    zeroPendingRetryCount: 2,
    zeroPendingRetryWaitMs: 7,
  });

  assert.equal(result.pending_count, 0);
  assert.equal(result.zero_pending_unverified, true);
  assert.equal(result.zero_pending_unverified_reason, 'review_surface_zero_not_confirmed');
  assert.equal(result.zero_pending_recheck_attempted, true);
  assert.equal(result.zero_pending_recheck_resolved, false);
  assert.equal(result.zero_pending_recheck_count, 2);
  assert.equal(result.zero_pending_rechecks.length, 2);
  assert.deepEqual(waits, [7, 7]);
});
