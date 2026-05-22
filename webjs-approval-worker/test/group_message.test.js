const test = require('node:test');
const assert = require('node:assert/strict');

const { sendGroupMessageWithClient } = require('../src/server');

test('sendGroupMessageWithClient sends text to resolved group and returns audit-safe result', async () => {
  const sent = [];
  const fakeGroup = {
    id: { _serialized: '120363000000000000@g.us' },
    name: 'ID Registration Group 01',
    isGroup: true,
    sendMessage: async (text) => {
      sent.push(text);
      return { id: { _serialized: 'msg-1' }, timestamp: 1710000000 };
    },
  };
  const fakeClient = {
    getChatById: async (id) => {
      assert.equal(id, '120363000000000000@g.us');
      return fakeGroup;
    },
  };

  const result = await sendGroupMessageWithClient(fakeClient, {
    target_group: '120363000000000000@g.us',
    message_text: 'Selamat datang semua.',
    metadata: { config_name: 'indo-reg-01' },
  });

  assert.equal(result.status, 'success');
  assert.equal(result.result_code, 'sent');
  assert.equal(result.group_id, '120363000000000000@g.us');
  assert.equal(result.group_name, 'ID Registration Group 01');
  assert.equal(result.message_id, 'msg-1');
  assert.deepEqual(sent, ['Selamat datang semua.']);
});

test('sendGroupMessageWithClient rejects empty text before sending', async () => {
  const fakeClient = {
    getChatById: async () => {
      throw new Error('should not resolve group for empty text');
    },
  };

  await assert.rejects(
    () => sendGroupMessageWithClient(fakeClient, { target_group: 'group@g.us', message_text: '   ' }),
    /message_text or media_path is required/,
  );
});

test('sendGroupMessageWithClient sends image media with caption when media is provided', async () => {
  const sent = [];
  const fakeGroup = {
    id: { _serialized: '120363000000000000@g.us' },
    name: 'ID Registration Group 01',
    isGroup: true,
    sendMessage: async (message, options) => {
      sent.push({ message, options });
      return { id: { _serialized: 'msg-media-1' }, timestamp: 1710000001 };
    },
  };
  const fakeClient = { getChatById: async () => fakeGroup };
  const result = await sendGroupMessageWithClient(fakeClient, {
    target_group: '120363000000000000@g.us',
    message_text: 'Poster baru.',
    media_path: '/tmp/poster.jpg',
    media_mime_type: 'image/jpeg',
  });

  assert.equal(result.status, 'success');
  assert.equal(result.message_id, 'msg-media-1');
  assert.equal(sent[0].options.caption, 'Poster baru.');
  assert.equal(sent[0].message.mimetype, 'image/jpeg');
});
