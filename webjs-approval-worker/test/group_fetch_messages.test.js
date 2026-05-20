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

test('fetchGroupMessagesWithClient filters messages after previous learning cursor and returns next cursor', async () => {
  const fakeMessages = [
    { body: 'Old before cursor', from: '111@c.us', timestamp: 1710000000, id: { _serialized: 'm1' } },
    { body: 'Old cursor item', from: '111@c.us', timestamp: 1710000060, id: { _serialized: 'm2' } },
    { body: 'New after cursor', from: '222@c.us', timestamp: 1710000120, id: { _serialized: 'm3' } },
    { body: 'Newest after cursor', from: '333@c.us', timestamp: 1710000180, id: { _serialized: 'm4' } },
  ];
  const fakeGroup = {
    id: { _serialized: 'group@g.us' },
    name: 'ID Group',
    isGroup: true,
    fetchMessages: async (options) => {
      assert.deepEqual(options, { limit: 4 });
      return fakeMessages;
    },
  };
  const fakeClient = { getChatById: async () => fakeGroup };

  const result = await fetchGroupMessagesWithClient(fakeClient, {
    target_group: 'group@g.us',
    limit: 4,
    after_message_id: 'm2',
    after_timestamp: '2024-03-09T16:01:00.000Z',
  });

  assert.deepEqual(result.records.map((item) => item.message_id), ['m3', 'm4']);
  assert.deepEqual(result.records.map((item) => item.text), ['New after cursor', 'Newest after cursor']);
  assert.deepEqual(result.next_cursor, {
    last_message_id: 'm4',
    last_message_at: '2024-03-09T16:03:00.000Z',
  });
});
