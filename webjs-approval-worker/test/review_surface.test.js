const test = require('node:test');
const assert = require('node:assert/strict');
const { parseReviewSurfaceBody, isUnconfirmedZeroSurface } = require('../src/server');

test('parseReviewSurfaceBody extracts pending count and requesters from Chinese review surface', () => {
  const body = [
    '群组信息',
    '待处理请求 7',
    'Alice',
    '请求加入。点击以审核。',
    'Bob',
    '请求加入。点击以审核。',
  ].join('\n');

  const result = parseReviewSurfaceBody(body);

  assert.equal(result.page_ready, true);
  assert.equal(result.has_pending_section, true);
  assert.equal(result.empty_queue_visible, false);
  assert.equal(result.pending_count, 7);
  assert.deepEqual(result.requesters, ['Alice', 'Bob']);
});

test('parseReviewSurfaceBody recognizes confirmed empty queue', () => {
  const body = ['群组信息', '待处理请求', '没有要审核的成员'].join('\n');

  const result = parseReviewSurfaceBody(body);

  assert.equal(result.page_ready, true);
  assert.equal(result.empty_queue_visible, true);
  assert.equal(result.pending_count, 0);
  assert.deepEqual(result.requesters, []);
  assert.equal(isUnconfirmedZeroSurface(result), false);
});

test('parseReviewSurfaceBody marks zero without empty-queue evidence as unconfirmed', () => {
  const body = ['群组信息', 'Some other group copy without request rows'].join('\n');

  const result = parseReviewSurfaceBody(body);

  assert.equal(result.page_ready, true);
  assert.equal(result.pending_count, 0);
  assert.equal(result.empty_queue_visible, false);
  assert.equal(result.has_pending_section, false);
  assert.equal(result.has_pending_request_row, false);
  assert.equal(isUnconfirmedZeroSurface(result), true);
});

test('parseReviewSurfaceBody ignores stale requester residue when review surface is not actually ready', () => {
  const body = ['~Chauncey', '请求加入。点击以审核。', '~G3 presonal', '请求加入。点击以审核。'].join('\n');

  const result = parseReviewSurfaceBody(body);

  assert.equal(result.page_ready, false);
  assert.equal(result.pending_count, 0);
  assert.equal(result.has_pending_request_row, false);
  assert.deepEqual(result.requesters, []);
  assert.equal(isUnconfirmedZeroSurface(result), false);
});
