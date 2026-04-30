const test = require('node:test');
const assert = require('node:assert/strict');
const { selectRequests, scoreRequest, getRequestEnrichedWithClient } = require('../src/server');

test('scoreRequest marks exact phone match strongly', () => {
  const result = scoreRequest(
    { phoneNormalized: '+628123456789', displayName: 'Alice' },
    { targetPhoneHint: '+62 812-3456-789', targetNameHint: 'Alice' },
  );
  assert.equal(result.phoneExactMatch, true);
  assert.equal(result.nameExactMatch, true);
  assert.equal(result.score, 180);
});

test('selectRequests requires exact phone match when phone hint is present', () => {
  const requests = [
    { requesterId: 'req-1@c.us', phoneNormalized: '+628100000001', displayName: 'Wrong One' },
    { requesterId: 'req-2@c.us', phoneNormalized: '+628100000002', displayName: 'Also Wrong' },
  ];
  const selected = selectRequests(requests, {
    approved_count: 1,
    target_phone_hint: '+62 8100000003',
    target_name_hint: 'Wrong One',
  });
  assert.deepEqual(selected, []);
});

test('selectRequests returns only the exact phone-matched requester for single-target approval', () => {
  const requests = [
    { requesterId: 'req-1@c.us', phoneNormalized: '+628100000001', displayName: 'Near Match' },
    { requesterId: 'req-2@c.us', phoneNormalized: '+628100000003', displayName: 'Target User' },
  ];
  const selected = selectRequests(requests, {
    approved_count: 1,
    target_phone_hint: '+62 8100000003',
    target_name_hint: 'Target User',
  });
  assert.equal(selected.length, 1);
  assert.equal(selected[0].entry.requesterId, 'req-2@c.us');
  assert.equal(selected[0].phoneExactMatch, true);
});

test('selectRequests keeps batch behavior when no identity hints are present', () => {
  const requests = [
    { requesterId: 'req-1@c.us', phoneNormalized: '+628****0001', displayName: 'A' },
    { requesterId: 'req-2@c.us', phoneNormalized: '+628****0002', displayName: 'B' },
    { requesterId: 'req-3@c.us', phoneNormalized: '+628****0003', displayName: 'C' },
  ];
  const selected = selectRequests(requests, {
    approved_count: 2,
  });
  assert.equal(selected.length, 2);
  assert.equal(selected[0].entry.requesterId, 'req-1@c.us');
  assert.equal(selected[1].entry.requesterId, 'req-2@c.us');
});

test('getRequestEnrichedWithClient prefers contact number for lid requester ids', async () => {
  const group = {
    async getGroupMembershipRequests() {
      return [
        {
          id: '156973186195687@lid',
          requestMethod: 'InviteLink',
          t: 1777445614,
        },
      ];
    },
  };
  const activeClient = {
    async getContactById(requesterId) {
      assert.equal(requesterId, '156973186195687@lid');
      return {
        number: '628123456789',
        pushname: 'Chauncey',
      };
    },
  };

  const rows = await getRequestEnrichedWithClient(activeClient, group);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].requesterId, '156973186195687@lid');
  assert.equal(rows[0].phoneRaw, '+628123456789');
  assert.equal(rows[0].phoneNormalized, '+628123456789');
  assert.equal(rows[0].displayName, 'Chauncey');
});

test('getRequestEnrichedWithClient falls back to lid-to-phone lookup when contact number is masked', async () => {
  const group = {
    async getGroupMembershipRequests() {
      return [
        {
          id: '156973186195687@lid',
          requestMethod: 'InviteLink',
          t: 1777445614,
        },
      ];
    },
  };
  const activeClient = {
    async getContactById() {
      return {
        number: '156****5687',
        pushname: 'Chauncey',
      };
    },
    async getContactLidAndPhone(userIds) {
      assert.deepEqual(userIds, ['156973186195687@lid']);
      return [{ lid: '156973186195687@lid', pn: '628765432100@c.us' }];
    },
  };

  const rows = await getRequestEnrichedWithClient(activeClient, group);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].phoneRaw, '+628765432100');
  assert.equal(rows[0].phoneNormalized, '+628765432100');
});

test('getRequestEnrichedWithClient prefers lid-to-phone lookup when contact number is only lid digits', async () => {
  const group = {
    async getGroupMembershipRequests() {
      return [
        {
          id: '156973186195687@lid',
          requestMethod: 'InviteLink',
          t: 1777445614,
        },
      ];
    },
  };
  const activeClient = {
    async getContactById() {
      return {
        number: '156973186195687',
        pushname: 'Chauncey',
      };
    },
    async getContactLidAndPhone() {
      return [{ lid: '156973186195687@lid', pn: '85267755475@c.us' }];
    },
  };

  const rows = await getRequestEnrichedWithClient(activeClient, group);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].phoneRaw, '+85267755475');
  assert.equal(rows[0].phoneNormalized, '+85267755475');
});
