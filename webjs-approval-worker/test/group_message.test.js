const test = require('node:test');
const assert = require('node:assert/strict');

const { sendGroupMessageWithClient, handleGroupAtmosphereIncomingMessage, inferGroupAtmosphereAccountKey } = require('../src/server');

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


test('handleGroupAtmosphereIncomingMessage posts group id to backend and sends matched keyword response', async () => {
  const sent = [];
  const fakeGroup = {
    id: { _serialized: '120363000000000000@g.us' },
    name: 'ID Registration Group 01',
    isGroup: true,
    sendMessage: async (text) => {
      sent.push(text);
      return { id: { _serialized: 'msg-trigger-1' }, timestamp: 1710000002 };
    },
  };
  const fakeClient = { getChatById: async () => fakeGroup };
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url, body: JSON.parse(options.body) });
    return {
      ok: true,
      status: 200,
      text: async () => JSON.stringify({
        should_respond: true,
        result_code: 'trigger_rule_matched',
        relationship_key: 'role-id-000001',
        trigger_type: 'keyword_match',
        matched_keyword: 'apa',
        matched_rule: { rule_id: 'rule-1' },
        reply_sequence: [{ type: 'text', text: 'Balasan otomatis.', delay_seconds: 0 }],
      }),
    };
  };

  const result = await handleGroupAtmosphereIncomingMessage(fakeClient, {
    fromMe: false,
    from: '120363000000000000@g.us',
    author: '62812@c.us',
    body: 'apa',
    getChat: async () => fakeGroup,
  }, {
    enabled: true,
    accountKey: 'atmosphere-indo-01',
    backendUrl: 'http://127.0.0.1:8011',
    fetchImpl,
    skipDelay: true,
  });

  assert.equal(result.handled, true);
  assert.equal(calls[0].url, 'http://127.0.0.1:8011/api/internal/group-atmosphere/inbound-message');
  assert.equal(calls[0].body.account_key, 'atmosphere-indo-01');
  assert.equal(calls[0].body.target_group, '120363000000000000@g.us');
  assert.equal(calls[0].body.text, 'apa');
  assert.deepEqual(sent, ['Balasan otomatis.']);
});

test('handleGroupAtmosphereIncomingMessage ignores own and non-group messages', async () => {
  const fakeClient = { getChatById: async () => { throw new Error('should not send'); } };
  const own = await handleGroupAtmosphereIncomingMessage(fakeClient, { fromMe: true, body: 'apa' }, { enabled: true });
  assert.equal(own.handled, false);
  assert.equal(own.result_code, 'own_or_empty_message_ignored');

  const direct = await handleGroupAtmosphereIncomingMessage(fakeClient, {
    fromMe: false,
    body: 'apa',
    getChat: async () => ({ isGroup: false }),
  }, { enabled: true });
  assert.equal(direct.handled, false);
  assert.equal(direct.result_code, 'non_group_message_ignored');
});

test('inferGroupAtmosphereAccountKey strips worker client prefix', () => {
  assert.equal(inferGroupAtmosphereAccountKey('wa-approval-atmosphere-indo-01'), 'atmosphere-indo-01');
  assert.equal(inferGroupAtmosphereAccountKey('wa-approval-atmosphere-indo-01-approval'), 'atmosphere-indo-01');
});
