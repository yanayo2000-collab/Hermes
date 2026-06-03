import json
from types import SimpleNamespace

from app.main import Database, Service
from app.registration_group_baileys_executor import BaileysRegistrationGroupApprovalExecutor
from app.whatsapp_approval_runtime import DefaultWhatsAppApprovalRuntimeAdapter


def test_service_bootstraps_wa_runtime_projection_tables():
    db = Database(':memory:')
    Service(db)

    with db.connect() as conn:
        table_names = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

    assert 'wa_accounts' in table_names
    assert 'wa_group_bindings' in table_names
    assert 'wa_truth_snapshots' in table_names
    assert 'wa_runtime_actions' in table_names
    assert 'wa_identity_map' in table_names
    assert 'truth_acquisition_logs' in table_names


def test_latest_probe_snapshot_mirrors_wa_truth_snapshot_and_identity_map():
    db = Database(':memory:')
    service = Service(db)
    binding = {
        'binding_id': 'binding-001',
        'group_id': '120363001@g.us',
        'group_name': 'RG-1',
        'registration_group': '120363001@g.us',
        'provider_mode': 'baileys_authoritative',
    }
    probe_result = {
        'trust_status': 'TRUSTED_CONFIRMED_PENDING',
        'reason_code': 'api_pending_confirmed',
        'trusted_pending_count': 3,
        'pending_count': 3,
        'requester_ids': ['628123456789@s.whatsapp.net', 'abc@lid'],
        'requesters': [{'requesterId': '628123456789@s.whatsapp.net'}],
        'source': {'provider_name': 'baileys', 'mode': 'authoritative'},
        'fingerprint': 'fp-1',
        'fingerprint_quality': 'strong',
        'strong_empty_evidence': False,
    }

    result = service.upsert_approval_queue_latest_probe(
        account_key='rg-01',
        binding=binding,
        probe_result=probe_result,
        observed_at='2026-06-02T12:00:00+00:00',
    )

    assert result['written'] is True
    with db.connect() as conn:
        snapshot = conn.execute(
            "SELECT snapshot_type, truth_status, trusted_pending_count, requester_ids_json, facts_json FROM wa_truth_snapshots WHERE binding_id = ?",
            ('binding-001',),
        ).fetchone()
        identities = conn.execute(
            "SELECT provider_name, provider_requester_id, normalized_requester_id, lid FROM wa_identity_map ORDER BY provider_requester_id ASC"
        ).fetchall()

    assert snapshot is not None
    assert snapshot['snapshot_type'] == 'latest_probe'
    assert snapshot['truth_status'] == 'TRUSTED_CONFIRMED_PENDING'
    assert snapshot['trusted_pending_count'] == 3
    assert json.loads(snapshot['requester_ids_json']) == ['628123456789@s.whatsapp.net', 'abc@lid']
    facts = json.loads(snapshot['facts_json'])
    assert facts['fingerprint'] == 'fp-1'

    assert len(identities) == 2
    assert identities[0]['provider_name'] == 'baileys'
    assert identities[1]['lid'] == 'abc@lid'


def test_projection_helpers_write_wa_account_and_group_binding_tables():
    db = Database(':memory:')
    service = Service(db)

    service._sync_wa_account_projection(
        {
            'account_key': 'rg-02',
            'account_name': 'RG Account 02',
            'responsible_type': 'registration_group',
            'provider_mode': 'baileys_manual_approve_gray',
            'runtime_generation': 7,
            'verification_status': 'ready',
        },
        runtime_state={
            'provider_name': 'baileys',
            'provider_mode': 'baileys_manual_approve_gray',
            'status': 'active',
            'runtime_generation': 7,
        },
    )
    service._sync_wa_group_binding_projection(
        'rg-02',
        {
            'binding_id': 'binding-002',
            'group_id': '120363002@g.us',
            'group_name': 'RG-2',
            'registration_group': '120363002@g.us',
            'identity_status': 'resolved',
            'config_fingerprint': 'cfg-002',
            'provider_mode': 'baileys_manual_approve_gray',
            'provider_capabilities': {
                'shadow_read': True,
                'advisory_verify': True,
                'authoritative_read': False,
                'manual_approve': True,
            },
        },
        responsible_type='registration_group',
    )

    with db.connect() as conn:
        account_row = conn.execute(
            "SELECT provider_name, provider_mode, health_status, runtime_generation FROM wa_accounts WHERE account_key = ?",
            ('rg-02',),
        ).fetchone()
        binding_row = conn.execute(
            "SELECT provider_mode, identity_status, config_fingerprint, provider_capabilities_json FROM wa_group_bindings WHERE binding_id = ?",
            ('binding-002',),
        ).fetchone()

    assert account_row is not None
    assert account_row['provider_name'] == 'baileys'
    assert account_row['provider_mode'] == 'baileys_manual_approve_gray'
    assert account_row['health_status'] == 'active'
    assert account_row['runtime_generation'] == 7

    assert binding_row is not None
    assert binding_row['provider_mode'] == 'baileys_manual_approve_gray'
    assert binding_row['identity_status'] == 'resolved'
    assert binding_row['config_fingerprint'] == 'cfg-002'
    caps = json.loads(binding_row['provider_capabilities_json'])
    assert caps['shadow_read'] is True
    assert caps['manual_approve'] is True


def test_projection_helpers_default_registration_group_to_baileys_primary_when_mode_missing():
    db = Database(':memory:')
    service = Service(db)

    service._sync_wa_account_projection(
        {
            'account_key': 'rg-default',
            'account_name': 'RG Default',
            'responsible_type': 'registration_group',
        },
        runtime_state={
            'status': 'active',
        },
    )
    service._sync_wa_group_binding_projection(
        'rg-default',
        {
            'binding_id': 'binding-rg-default',
            'group_id': '120363200@g.us',
            'group_name': 'RG Default Group',
            'registration_group': '120363200@g.us',
        },
        responsible_type='registration_group',
    )

    with db.connect() as conn:
        account_row = conn.execute(
            "SELECT provider_name, provider_mode FROM wa_accounts WHERE account_key = ?",
            ('rg-default',),
        ).fetchone()
        binding_row = conn.execute(
            "SELECT provider_mode FROM wa_group_bindings WHERE binding_id = ?",
            ('binding-rg-default',),
        ).fetchone()

    assert account_row['provider_name'] == 'baileys'
    assert account_row['provider_mode'] == 'baileys_primary'
    assert binding_row['provider_mode'] == 'baileys_primary'


def test_adapter_shadow_and_authoritative_modes_route_as_expected():
    adapter = DefaultWhatsAppApprovalRuntimeAdapter()

    class StubService:
        def _call_whatsapp_worker_full_queue_sync(self, **kwargs):
            return {'trust_status': 'TRUSTED_CONFIRMED_PENDING', 'pending_count': 5, 'source': {'mode': 'legacy_only'}}

        def _call_baileys_full_queue_sync(self, **kwargs):
            return {'trust_status': 'TRUSTED_CONFIRMED_PENDING', 'pending_count': 4, 'source': {'mode': 'baileys_authoritative'}}

        def _probe_whatsapp_binding_group_state(self, **kwargs):
            return {'group_id': 'legacy@g.us', 'pending_count': 5}

        def _probe_baileys_binding_group_state(self, **kwargs):
            return {'group_id': 'baileys@g.us', 'pending_count': 4}

    service = StubService()

    shadow_result = adapter.full_queue_sync(
        service=service,
        account={'provider_mode': 'baileys_shadow'},
        binding={'provider_mode': 'baileys_shadow'},
        timeout_seconds=5.0,
    )
    authoritative_result = adapter.full_queue_sync(
        service=service,
        account={'provider_mode': 'baileys_authoritative'},
        binding={'provider_mode': 'baileys_authoritative'},
        timeout_seconds=5.0,
    )
    advisory_probe = adapter.probe_binding_group_state(
        service=service,
        responsible_type='registration_group',
        binding={'provider_mode': 'baileys_advisory'},
        runtime_state={'provider_mode': 'baileys_advisory'},
        session_state={},
    )

    assert shadow_result['provider'] == 'legacy_playwright'
    assert shadow_result['primary_provider'] == 'legacy_playwright'
    assert shadow_result['shadow_compare']['provider'] == 'baileys'
    assert shadow_result['shadow_compare']['pending_count'] == 4

    assert authoritative_result['provider'] == 'baileys'
    assert authoritative_result['primary_provider'] == 'baileys'
    assert authoritative_result['pending_count'] == 4
    assert authoritative_result['legacy_result_meta']['pending_count'] == 5

    assert advisory_probe['provider'] == 'legacy_playwright'
    assert advisory_probe['shadow_compare']['provider'] == 'baileys'
    assert advisory_probe['shadow_compare']['group_id'] == 'baileys@g.us'


def test_manual_approve_gray_routes_to_baileys_executor_and_runtime_aliases_project():
    adapter = DefaultWhatsAppApprovalRuntimeAdapter()

    class Payload:
        def __init__(self):
            self.__dict__['provider_mode'] = 'baileys_manual_approve_gray'
            self.__dict__['official_group_runtime'] = 'baileys_manual_approve_gray'

    class StubService:
        def _registration_group_baileys_approval_decision_sync(self, payload, approval_run_id=None):
            return {'approval_run_id': approval_run_id or 'run-1', 'approved_count': 2}

        def _registration_group_approval_decision_sync(self, payload, approval_run_id=None):
            raise AssertionError('legacy approval path should not run')

    result = adapter.execute_registration_group_approval(
        service=StubService(),
        payload=Payload(),
        approval_run_id='run-1',
    )

    assert result['provider'] == 'baileys'
    assert result['manual_approve_enabled'] is True

    db = Database(':memory:')
    service = Service(db)
    service._sync_wa_group_binding_projection(
        'official-01',
        {
            'binding_id': 'binding-official-01',
            'group_id': '120363100@g.us',
            'group_name': 'Official G1',
            'registration_group': '120363100@g.us',
            'identity_status': 'resolved',
            'official_group_runtime': 'baileys_authoritative',
        },
        responsible_type='official_group',
    )
    with db.connect() as conn:
        binding_row = conn.execute(
            "SELECT provider_mode FROM wa_group_bindings WHERE binding_id = ?",
            ('binding-official-01',),
        ).fetchone()
    assert binding_row['provider_mode'] == 'baileys_authoritative'


def test_adapter_defaults_registration_group_flows_to_baileys_but_explicit_legacy_still_works():
    adapter = DefaultWhatsAppApprovalRuntimeAdapter()

    class EmptyPayload:
        pass

    class LegacyPayload:
        def __init__(self):
            self.__dict__['provider_mode'] = 'legacy_only'
            self.__dict__['registration_group_runtime'] = 'legacy_only'

    class StubService:
        def _call_whatsapp_worker_full_queue_sync(self, **kwargs):
            return {'trust_status': 'TRUSTED_CONFIRMED_PENDING', 'pending_count': 8, 'source': {'mode': 'legacy_only'}}

        def _call_baileys_full_queue_sync(self, **kwargs):
            return {'trust_status': 'TRUSTED_CONFIRMED_PENDING', 'pending_count': 6, 'source': {'mode': 'baileys_primary'}}

        def _probe_whatsapp_binding_group_state(self, **kwargs):
            return {'group_id': 'legacy@g.us', 'pending_count': 8}

        def _probe_baileys_binding_group_state(self, **kwargs):
            return {'group_id': 'baileys@g.us', 'pending_count': 6}

        def _registration_group_baileys_approval_decision_sync(self, payload, approval_run_id=None):
            return {'approval_run_id': approval_run_id or 'run-default', 'approved_count': 6}

        def _registration_group_approval_decision_sync(self, payload, approval_run_id=None):
            return {'approval_run_id': approval_run_id or 'run-legacy', 'approved_count': 8}

    service = StubService()

    default_sync = adapter.full_queue_sync(
        service=service,
        account={'responsible_type': 'registration_group'},
        binding={},
        timeout_seconds=5.0,
    )
    default_probe = adapter.probe_binding_group_state(
        service=service,
        responsible_type='registration_group',
        binding={},
        runtime_state={},
        session_state={},
    )
    default_approval = adapter.execute_registration_group_approval(
        service=service,
        payload=EmptyPayload(),
        approval_run_id='run-default',
    )
    legacy_approval = adapter.execute_registration_group_approval(
        service=service,
        payload=LegacyPayload(),
        approval_run_id='run-legacy',
    )

    assert default_sync['provider'] == 'baileys'
    assert default_sync['provider_mode'] == 'baileys_primary'
    assert default_sync['pending_count'] == 6

    assert default_probe['provider'] == 'baileys'
    assert default_probe['provider_mode'] == 'baileys_primary'
    assert default_probe['group_id'] == 'baileys@g.us'

    assert default_approval['provider'] == 'baileys'
    assert default_approval['provider_mode'] == 'baileys_primary'
    assert default_approval['approved_count'] == 6

    assert legacy_approval['provider'] == 'legacy_playwright'
    assert legacy_approval['provider_mode'] == 'legacy_only'
    assert legacy_approval['approved_count'] == 8


def test_baileys_executor_falls_back_to_provider_specific_routes_on_404():
    calls = []

    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def raise_for_status(self):
            if self.status_code >= 400:
                error = RuntimeError(f'http {self.status_code}')
                error.response = self
                raise error

        def json(self):
            return self._payload

    class Session:
        def request(self, method, url, json=None, headers=None, timeout=None):
            calls.append((method, url, json, timeout))
            if url.endswith('/ops/baileys/approve'):
                return Response(200, {'status': 'ok', 'verified': True, 'approved_count': 2})
            if url.endswith('/approve'):
                return Response(404, {'detail': 'not found'})
            raise AssertionError(url)

    executor = BaileysRegistrationGroupApprovalExecutor(
        base_url='http://127.0.0.1:8790',
        token='token-1',
        session=Session(),
        timeout_seconds=9.0,
    )

    result = executor.approve({'approval_run_id': 'run-9', 'approved_count': 2})

    assert result['verified'] is True
    assert result['provider'] == 'baileys'
    assert result['provider_endpoint'] == '/ops/baileys/approve'
    assert [url for _, url, _, _ in calls] == [
        'http://127.0.0.1:8790/approve',
        'http://127.0.0.1:8790/ops/baileys/approve',
    ]


def test_service_routes_baileys_authoritative_runtime_to_baileys_executor():
    db = Database(':memory:')
    service = Service(db)
    service.registration_group_approval_executor = SimpleNamespace(timeout_seconds=11.0)
    service._build_whatsapp_approval_runtime_state = lambda account_key, allow_shared_fallback=False: {
        'active': True,
        'status': 'active',
        'login_verified': True,
        'base_url': 'http://legacy-worker:3100',
        'baileys_base_url': 'http://baileys-live:8790',
        'provider_mode': 'baileys_authoritative',
    }
    service._find_whatsapp_approval_account_binding = lambda **kwargs: {
        'account_key': 'rg-live-01',
        'account_name': 'RG Live 01',
        'responsible_type': 'registration_group',
        'binding': {
            'binding_id': 'binding-live-01',
            'group_id': '120363777@g.us',
            'group_name': 'RG Live',
            'registration_group': '120363777@g.us',
            'provider_mode': 'baileys_authoritative',
        },
    }

    routed = service._resolve_whatsapp_approval_runtime_executor(
        target_group='120363777@g.us',
        responsible_type='registration_group',
    )

    assert routed is not None
    assert routed['provider_decision']['provider_name'] == 'baileys'
    assert routed['provider_decision']['authoritative_read'] is True
    assert getattr(routed['executor'], 'base_url', '') == 'http://baileys-live:8790'


def test_service_calls_baileys_full_queue_sync_with_binding_context():
    db = Database(':memory:')
    service = Service(db)
    captured = {}

    class StubExecutor:
        def full_queue_sync(self, payload, timeout_seconds=None):
            captured['payload'] = dict(payload)
            captured['timeout_seconds'] = timeout_seconds
            return {'ok': True, 'pending_count': 2, 'provider': 'baileys'}

    service._build_runtime_baileys_registration_group_executor = lambda **kwargs: StubExecutor()

    result = service._call_baileys_full_queue_sync(
        account={
            'account_key': 'rg-01',
            'provider_mode': 'baileys_authoritative',
            'runtime_state': {'provider_mode': 'baileys_authoritative'},
        },
        binding={
            'binding_id': 'binding-01',
            'group_id': '120363888@g.us',
            'group_name': 'RG-88',
            'link': 'https://chat.whatsapp.com/abc',
        },
        timeout_seconds=18.0,
    )

    assert result['provider'] == 'baileys'
    assert captured['timeout_seconds'] == 18.0
    assert captured['payload']['registration_group'] == '120363888@g.us'
    assert captured['payload']['binding_id'] == 'binding-01'
    assert captured['payload']['account_key'] == 'rg-01'


def test_p4_capabilities_project_and_route_for_official_and_group_assistant_runtime():
    adapter = DefaultWhatsAppApprovalRuntimeAdapter()

    decision = adapter.provider_decision(
        account={'provider_mode': 'baileys_manual_approve_gray'},
        binding={'group_assistant_runtime': 'baileys_shadow'},
    ).to_dict()

    assert decision['provider_name'] == 'baileys'
    assert decision['provider_capabilities']['official_group_approval'] is True
    assert decision['provider_capabilities']['group_member_lookup'] is True
    assert decision['provider_capabilities']['group_metadata'] is True
    assert decision['provider_capabilities']['assistant_group_runtime'] is True

    db = Database(':memory:')
    service = Service(db)
    service._sync_wa_group_binding_projection(
        'official-02',
        {
            'binding_id': 'binding-official-02',
            'group_id': '120363200@g.us',
            'group_name': 'Official G2',
            'registration_group': '120363200@g.us',
            'identity_status': 'resolved',
            'official_group_runtime': 'baileys_manual_approve_gray',
            'group_assistant_runtime': 'baileys_shadow',
            'provider_capabilities': {
                'officialGroupApproval': True,
                'groupMemberLookup': True,
                'groupMetadata': True,
                'assistantGroupRuntime': True,
            },
        },
        responsible_type='official_group',
    )
    with db.connect() as conn:
        binding_row = conn.execute(
            "SELECT provider_mode, provider_capabilities_json FROM wa_group_bindings WHERE binding_id = ?",
            ('binding-official-02',),
        ).fetchone()

    assert binding_row['provider_mode'] == 'baileys_manual_approve_gray'
    caps = json.loads(binding_row['provider_capabilities_json'])
    assert caps['official_group_approval'] is True
    assert caps['group_member_lookup'] is True
    assert caps['group_metadata'] is True
    assert caps['assistant_group_runtime'] is True


def test_record_runtime_action_defaults_registration_group_manual_approve_to_baileys():
    db = Database(':memory:')
    service = Service(db)

    action_id = service._record_wa_runtime_action(
        account_key='rg-action-01',
        binding={
            'binding_id': 'binding-action-01',
            'approval_scope': 'registration_group',
        },
        action_type='manual_approve',
        status='started',
        request_payload={
            'request_id': 'req-001',
            'responsible_type': 'registration_group',
        },
        result_payload={},
    )

    with db.connect() as conn:
        row = conn.execute(
            "SELECT provider_name, provider_mode FROM wa_runtime_actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()

    assert row is not None
    assert row['provider_name'] == 'baileys'
    assert row['provider_mode'] == 'baileys_primary'


def test_baileys_executor_supports_p4_endpoint_fallbacks():
    calls = []

    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def raise_for_status(self):
            if self.status_code >= 400:
                error = RuntimeError(f'http {self.status_code}')
                error.response = self
                raise error

        def json(self):
            return self._payload

    class Session:
        def request(self, method, url, json=None, headers=None, timeout=None):
            calls.append((method, url, json, timeout))
            if url.endswith('/ops/baileys/group-member-lookup'):
                return Response(200, {'ok': True, 'members': [{'wa_id': '62811'}]})
            if url.endswith('/group-member-lookup'):
                return Response(404, {'detail': 'not found'})
            if url.endswith('/ops/baileys/group-metadata'):
                return Response(200, {'ok': True, 'metadata': {'subject': 'G-1'}})
            if url.endswith('/group-metadata'):
                return Response(404, {'detail': 'not found'})
            if url.endswith('/ops/baileys/official-group/approve'):
                return Response(200, {'status': 'ok', 'verified': True, 'approved_count': 1})
            if url.endswith('/official-group/approve'):
                return Response(404, {'detail': 'not found'})
            raise AssertionError(url)

    executor = BaileysRegistrationGroupApprovalExecutor(
        base_url='http://127.0.0.1:8790',
        token='token-2',
        session=Session(),
        timeout_seconds=9.0,
    )

    members = executor.group_member_lookup({'group_id': '120363300@g.us'})
    metadata = executor.group_metadata({'group_id': '120363300@g.us'})
    approval = executor.official_group_approve({'approval_run_id': 'run-og-1', 'approved_count': 1})

    assert members['ok'] is True
    assert members['provider_endpoint'] == '/ops/baileys/group-member-lookup'
    assert members['members'][0]['wa_id'] == '62811'

    assert metadata['ok'] is True
    assert metadata['provider_endpoint'] == '/ops/baileys/group-metadata'
    assert metadata['metadata']['subject'] == 'G-1'

    assert approval['verified'] is True
    assert approval['provider_endpoint'] == '/ops/baileys/official-group/approve'

    assert [url for _, url, _, _ in calls] == [
        'http://127.0.0.1:8790/group-member-lookup',
        'http://127.0.0.1:8790/ops/baileys/group-member-lookup',
        'http://127.0.0.1:8790/group-metadata',
        'http://127.0.0.1:8790/ops/baileys/group-metadata',
        'http://127.0.0.1:8790/official-group/approve',
        'http://127.0.0.1:8790/ops/baileys/official-group/approve',
    ]
