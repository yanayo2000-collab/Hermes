const assert = require('node:assert/strict');
const test = require('node:test');

const { fetchGroupMessagesWithClient } = require('../src/server');

test('fetchGroupMessagesWithClient returns recent text messages from resolved group without sending', async () => {
  const fakeMessages = [
    { body: 'Halo kak, gmn mulai?', from: '111@c.us', author: '111@c.us', timestamp: 1710000000, id: { _serialized: 'm1' } },
    { body: 'Jgn lupa krm ID ya kak', from: '222@c.us', timestamp: 1710000060, id: { _serialized: 'm2' } },
    { body: '', from: '333@c.us', timestamp: 1710000120, id: { _serialized: 'm3' } },
  ];
  const fakeGroup = {
    id: { _serialized: 'group@g.us' },
    name: 'ID Group',
    isGroup: true,
    fetchMessages: async (options) => {
      assert.deepEqual(options, { limit: 2 });
      return fakeMessages.slice(0, 2);
    },
    sendMessage: async () => {
      throw new Error('learning fetch must not send messages');
    },
  };
  const fakeClient = {
    getChatById: async (id) => {
      assert.equal(id, 'group@g.us');
      return fakeGroup;
    },
  };

  const result = await fetchGroupMessagesWithClient(fakeClient, {
    target_group: 'group@g.us',
    limit: 2,
  });

  assert.equal(result.status, 'success');
  assert.equal(result.result_code, 'messages_fetched');
  assert.equal(result.group_id, 'group@g.us');
  assert.equal(result.group_name, 'ID Group');
  assert.deepEqual(result.records.map((item) => item.text), ['Halo kak, gmn mulai?', 'Jgn lupa krm ID ya kak']);
});

test('fetchGroupMessagesWithClient rejects empty target group', async () => {
  await assert.rejects(
    () => fetchGroupMessagesWithClient({}, { target_group: '', limit: 10 }),
    /target_group is required/,
  );
});
