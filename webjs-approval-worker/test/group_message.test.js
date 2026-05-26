const test = require('node:test');
const assert = require('node:assert/strict');

const { sendGroupMessageWithClient, handleGroupAtmosphereIncomingMessage, handleGroupAtmosphereTriggerEvent, scheduleGroupAtmosphereMemberJoinEvent, recordGroupAtmosphereOrdinaryMessage, checkGroupAtmosphereSilenceOnce, inferGroupAtmosphereAccountKey } = require('../src/server');

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
  assert.equal(own.result_code, 'non_group_message_ignored');

  const direct = await handleGroupAtmosphereIncomingMessage(fakeClient, {
    fromMe: false,
    body: 'apa',
    getChat: async () => ({ isGroup: false }),
  }, { enabled: true });
  assert.equal(direct.handled, false);
  assert.equal(direct.result_code, 'non_group_message_ignored');
});

test('handleGroupAtmosphereTriggerEvent posts member_join to backend and sends sequence', async () => {
  const sent = [];
  const fakeGroup = {
    id: { _serialized: '120363000000000000@g.us' },
    name: 'ID Registration Group 01',
    isGroup: true,
    sendMessage: async (text) => {
      sent.push(text);
      return { id: { _serialized: 'msg-join-1' }, timestamp: 1710000003 };
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
        trigger_type: 'member_join',
        reply_sequence: [{ type: 'text', text: 'Halo kak, welcome.', delay_seconds: 0 }],
      }),
    };
  };

  const result = await handleGroupAtmosphereTriggerEvent(fakeClient, 'member_join', {
    chatId: '120363000000000000@g.us',
    recipientIds: ['62812@c.us'],
  }, {
    enabled: true,
    accountKey: 'atmosphere-indo-01',
    backendUrl: 'http://127.0.0.1:8011',
    fetchImpl,
    skipDelay: true,
  });

  assert.equal(result.handled, true);
  assert.equal(calls[0].url, 'http://127.0.0.1:8011/api/internal/group-atmosphere/trigger-event');
  assert.equal(calls[0].body.trigger_type, 'member_join');
  assert.equal(calls[0].body.sender_id, '62812@c.us');
  assert.deepEqual(sent, ['Halo kak, welcome.']);
});

test('silence clock records own and media group messages without keyword reply', async () => {
  const fakeGroup = { id: { _serialized: '120363555555555555@g.us' }, isGroup: true, name: 'Group 55' };
  const fakeClient = { getChatById: async () => fakeGroup };
  const own = await handleGroupAtmosphereIncomingMessage(fakeClient, {
    fromMe: true,
    from: '120363555555555555@g.us',
    body: 'Halo welcome.',
    timestamp: Math.floor(Date.now() / 1000),
    getChat: async () => fakeGroup,
  }, { enabled: true, accountKey: 'atmosphere-indo-01' });
  assert.equal(own.handled, false);
  assert.equal(own.result_code, 'own_message_recorded');

  const media = await handleGroupAtmosphereIncomingMessage(fakeClient, {
    fromMe: false,
    from: '120363555555555555@g.us',
    author: '62813@c.us',
    body: '',
    hasMedia: true,
    type: 'image',
    timestamp: Math.floor(Date.now() / 1000),
    getChat: async () => fakeGroup,
  }, { enabled: true, accountKey: 'atmosphere-indo-01' });
  assert.equal(media.handled, false);
  assert.equal(media.result_code, 'ordinary_non_text_recorded');

  const system = await recordGroupAtmosphereOrdinaryMessage(fakeClient, {
    fromMe: false,
    from: '120363555555555555@g.us',
    type: 'system',
    event_type: 'member_join',
    getChat: async () => fakeGroup,
  });
  assert.equal(system.recorded, false);
  assert.equal(system.result_code, 'system_message_ignored');
});

test('scheduleGroupAtmosphereMemberJoinEvent debounces same-group joins into one backend trigger', async () => {
  const sent = [];
  const fakeGroup = {
    id: { _serialized: '120363777777777777@g.us' },
    name: 'ID Registration Group 77',
    isGroup: true,
    sendMessage: async (text) => {
      sent.push(text);
      return { id: { _serialized: 'msg-join-batch-1' }, timestamp: 1710000005 };
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
        trigger_type: 'member_join',
        reply_sequence: [{ type: 'text', text: 'Welcome batch.', delay_seconds: 0 }],
      }),
    };
  };

  const first = await scheduleGroupAtmosphereMemberJoinEvent(fakeClient, {
    chatId: '120363777777777777@g.us',
    recipientIds: ['62811@c.us'],
  }, {
    enabled: true,
    accountKey: 'atmosphere-indo-01',
    backendUrl: 'http://127.0.0.1:8011',
    fetchImpl,
    skipDelay: true,
    debounceMs: 25,
    maxWaitMs: 200,
    keepTimerRef: true,
  });
  assert.equal(first.scheduled, true);
  await new Promise((resolve) => setTimeout(resolve, 10));
  const second = await scheduleGroupAtmosphereMemberJoinEvent(fakeClient, {
    chatId: '120363777777777777@g.us',
    recipientIds: ['62812@c.us'],
  }, {
    enabled: true,
    accountKey: 'atmosphere-indo-01',
    backendUrl: 'http://127.0.0.1:8011',
    fetchImpl,
    skipDelay: true,
    debounceMs: 25,
    maxWaitMs: 200,
    keepTimerRef: true,
  });
  assert.equal(second.batch_member_count, 2);
  const finalResult = await second.promise;

  assert.equal(finalResult.handled, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].body.trigger_type, 'member_join');
  assert.equal(calls[0].body.event_payload.batch_member_count, 2);
  assert.deepEqual(calls[0].body.event_payload.member_ids.sort(), ['62811@c.us', '62812@c.us']);
  assert.deepEqual(sent, ['Welcome batch.']);
});

test('checkGroupAtmosphereSilenceOnce posts observed silence seconds and sends matched response', async () => {
  const sent = [];
  const nowSeconds = Math.floor(Date.now() / 1000);
  const fakeGroup = {
    id: { _serialized: '120363999999999999@g.us' },
    name: 'ID Registration Group 99',
    isGroup: true,
    timestamp: nowSeconds - 180,
    sendMessage: async (text) => {
      sent.push(text);
      return { id: { _serialized: 'msg-silence-1' }, timestamp: 1710000004 };
    },
  };
  const fakeClient = {
    getChats: async () => [fakeGroup, { id: { _serialized: 'direct@c.us' }, isGroup: false }],
    getChatById: async () => fakeGroup,
  };
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url, body: JSON.parse(options.body) });
    return {
      ok: true,
      status: 200,
      text: async () => JSON.stringify({
        should_respond: true,
        result_code: 'trigger_rule_matched',
        trigger_type: 'group_silence',
        reply_sequence: [{ type: 'text', text: 'Sepi ya kak?', delay_seconds: 0 }],
      }),
    };
  };

  const result = await checkGroupAtmosphereSilenceOnce(fakeClient, {
    enabled: true,
    accountKey: 'atmosphere-indo-01',
    backendUrl: 'http://127.0.0.1:8011',
    fetchImpl,
    skipDelay: true,
  });

  assert.equal(result.checked, true);
  assert.equal(result.count, 1);
  assert.equal(calls[0].body.trigger_type, 'group_silence');
  assert.equal(calls[0].body.target_group, '120363999999999999@g.us');
  assert.ok(calls[0].body.event_payload.silence_seconds >= 170);
  assert.deepEqual(sent, ['Sepi ya kak?']);
});

test('inferGroupAtmosphereAccountKey strips worker client prefix', () => {
  assert.equal(inferGroupAtmosphereAccountKey('wa-approval-atmosphere-indo-01'), 'atmosphere-indo-01');
  assert.equal(inferGroupAtmosphereAccountKey('wa-approval-atmosphere-indo-01-approval'), 'atmosphere-indo-01');
});


test('resolveAuthoritativeGroupState confirms pending only when requester ids exist', async () => {
  const { resolveAuthoritativeGroupState } = require('../src/server');
  const calls = [];
  const result = await resolveAuthoritativeGroupState({ registration_group: '120363API@g.us' }, {
    zeroPendingStableReadCount: 3,
    zeroPendingRetryWaitMs: 0,
    waitFn: async () => {},
    stateLoader: async (meta) => {
      calls.push(meta);
      return {
        group_id: '120363API@g.us',
        group_name: 'API Group',
        pending_count: 7,
        requester_ids: [],
        member_count: 99,
      };
    },
  });

  assert.equal(calls.length, 1);
  assert.equal(result.approval_state_status, 'unverified_pending');
  assert.equal(result.zero_pending_unverified, false);
  assert.equal(result.pending_count, 7);
  assert.deepEqual(result.requester_ids, []);
  assert.equal(result.unverified_pending_reason, 'pending_without_requester_ids');
});

test('resolveAuthoritativeGroupState returns confirmed pending immediately when requester ids exist', async () => {
  const { resolveAuthoritativeGroupState } = require('../src/server');
  const calls = [];
  const result = await resolveAuthoritativeGroupState({ registration_group: '120363API@g.us' }, {
    zeroPendingStableReadCount: 3,
    zeroPendingRetryWaitMs: 0,
    waitFn: async () => {},
    stateLoader: async (meta) => {
      calls.push(meta);
      return {
        group_id: '120363API@g.us',
        group_name: 'API Group',
        pending_count: 1,
        requester_ids: ['user-1@c.us'],
        member_count: 99,
      };
    },
  });

  assert.equal(calls.length, 1);
  assert.equal(result.approval_state_status, 'confirmed_pending');
  assert.equal(result.pending_count, 1);
  assert.deepEqual(result.requester_ids, ['user-1@c.us']);
});

test('resolveAuthoritativeGroupState confirms empty only after three identity-stable empty reads', async () => {
  const { resolveAuthoritativeGroupState } = require('../src/server');
  const calls = [];
  const result = await resolveAuthoritativeGroupState({ registration_group: '120363API@g.us' }, {
    zeroPendingStableReadCount: 3,
    zeroPendingRetryWaitMs: 0,
    waitFn: async () => {},
    stateLoader: async (meta) => {
      calls.push(meta);
      return {
        group_id: '120363API@g.us',
        group_name: 'API Group',
        pending_count: 0,
        requester_ids: [],
        member_count: 99,
      };
    },
  });

  assert.equal(calls.length, 3);
  assert.equal(result.approval_state_status, 'confirmed_empty');
  assert.equal(result.pending_count, 0);
  assert.equal(result.zero_pending_unverified, false);
  assert.equal(result.zero_pending_verified_by, 'consecutive_group_state_refresh');
});

test('resolveAuthoritativeGroupState keeps empty unverified when group identity changes during empty reads', async () => {
  const { resolveAuthoritativeGroupState } = require('../src/server');
  const rows = [
    { group_id: '120363API@g.us', group_name: 'API Group', pending_count: 0, requester_ids: [], member_count: 99 },
    { group_id: '120363OTHER@g.us', group_name: 'Other Group', pending_count: 0, requester_ids: [], member_count: 99 },
  ];
  const result = await resolveAuthoritativeGroupState({ registration_group: '120363API@g.us' }, {
    zeroPendingStableReadCount: 3,
    zeroPendingRetryWaitMs: 0,
    waitFn: async () => {},
    stateLoader: async () => rows.shift(),
  });

  assert.equal(result.approval_state_status, 'unverified_empty');
  assert.equal(result.zero_pending_unverified, true);
  assert.equal(result.zero_pending_unverified_reason, 'group_identity_changed_during_zero_recheck');
});
