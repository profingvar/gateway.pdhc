"""#1047 — the spärr (PatientBlock) filter must fail CLOSED.

`IpsClient.fetch_active_blocks` returned `[]` for an unset eps address, a
network error and every HTTP >= 400, and read the list from `blocks`/`entry`
while eps sends `items`. So a refusal — and every successful answer — read as
"this patient has no blocks", the result was cached for 30 s, and
GET /api/v1/observations passed every row.

Every test here goes through the real route with a real bearer check (the SSO
validation is the only auth seam patched, as in the sibling modules) and
patches ONLY the outbound HTTP boundary: `requests.get` as the eps client
calls it. `fetch_blocks_for_patients` is deliberately NOT patched — the older
endpoint tests patch it, which is how the client's behaviour went untested.
"""
from unittest.mock import MagicMock, patch
import uuid

import pytest
import requests

from app.models import AuditLog, ServiceRequestStatus
from app.services import ips_client as ips_mod

ORG_REQ = str(uuid.uuid4())
PROV_A = str(uuid.uuid4())
PROV_B = str(uuid.uuid4())
CONTRACT = str(uuid.uuid4())
SR = str(uuid.uuid4())
PATIENT = str(uuid.uuid4())        # two rows: one from PROV_A, one from PROV_B
OTHER = str(uuid.uuid4())          # one row from PROV_B; eps always answers for them
EPS = 'https://eps.test'


def _entry(obs_id, patient, provider):
    return {'resource': {
        'resourceType': 'Observation', 'id': obs_id, 'status': 'final',
        'subject': {'reference': f'Patient/{patient}'},
        'effectiveDateTime': '2026-05-15T10:00:00+00:00',
        'performer': [{'identifier': {'value': provider}}],
        'code': {'coding': [{'system': 'urn:pdhc:concept', 'code': 'c-glucose'}]},
    }}


def _analyse(service_request_guids, *, patient=None, request_id=''):
    entries = [_entry('obs-A', PATIENT, PROV_A), _entry('obs-B', PATIENT, PROV_B),
               _entry('obs-other', OTHER, PROV_B)]
    return {'resourceType': 'Bundle', 'type': 'searchset',
            'total': len(entries), 'entry': entries}


def _eps_block(source, *, redacted=False):
    """One block exactly as eps.pdhc serialises it."""
    return {
        'guid': str(uuid.uuid4()), 'patient_guid': PATIENT,
        'source_scope_type': 'clinic',
        'source_scope_id': None if redacted else source,
        'is_active': True,
        'lift_kind': None, 'lift_concept_guids': None,
        'lift_from_date': None, 'lift_until_date': None,
        'redacted': redacted,
    }


def _resp(status, payload=None):
    r = MagicMock(status_code=status)
    if payload is None:
        r.json.side_effect = ValueError('no body')
    else:
        r.json.return_value = payload
    return r


class Eps:
    """The outbound HTTP boundary. `answer` decides for PATIENT; OTHER always
    gets a clean 200-empty, so each test also proves the withholding is per
    patient and not a blanket refusal."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append(url)
        assert url.startswith(f'{EPS}/api/v1/patients/'), url
        if f'/patients/{PATIENT}/' in url:
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer
        return _resp(200, {'items': [], 'total': 0})

    def for_patient(self):
        return [u for u in self.calls if f'/patients/{PATIENT}/' in u]


@pytest.fixture
def seeded(app, db):
    db.session.add(ServiceRequestStatus(
        service_request_guid=SR, patient_guid=PATIENT,
        provider_org_guid=PROV_A, contract_guid=CONTRACT))
    db.session.commit()
    ips_mod.invalidate()
    old = {k: app.config.get(k) for k in
           ('IPS_BASE_URL', 'IPS_API_KEY', 'SPARR_FAIL_CLOSED')}
    app.config.update(IPS_BASE_URL=EPS, IPS_API_KEY='k', SPARR_FAIL_CLOSED=True)
    yield app
    for k, v in old.items():
        if v is None:
            app.config.pop(k, None)
        else:
            app.config[k] = v
    ips_mod.invalidate()


def _read(client, eps):
    blob = {'user_guid': str(uuid.uuid4()), 'user_type': 'professional',
            'is_su_admin': False, 'effective_phases': ['analysis'],
            'affiliations': [{'care_unit_guid': ORG_REQ}]}
    with patch('app.api.observations.validate_sso_token', return_value=blob), \
         patch('app.api.observations.ContractScopeService.fetch_parties',
               return_value={'requesting_org_guid': ORG_REQ}), \
         patch('app.api.observations.AnalyseClient.search_observations',
               side_effect=_analyse), \
         patch.object(ips_mod.requests, 'get', side_effect=eps):
        r = client.get(f'/api/v1/observations?organization={ORG_REQ}',
                       headers={'Authorization': 'Bearer t'})
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    return j, sorted(e['resource']['id'] for e in j['entry'])


def test_the_route_refuses_without_a_bearer(client, seeded):
    """Auth is ON for everything below: no bearer, no rows."""
    r = client.get(f'/api/v1/observations?organization={ORG_REQ}')
    assert r.status_code == 401


# ── the two answers that mean "no blocks" ────────────────────────────────

def test_200_with_an_empty_list_passes_the_rows(client, seeded):
    j, ids = _read(client, Eps(_resp(200, {'items': [], 'total': 0})))
    assert ids == ['obs-A', 'obs-B', 'obs-other']
    assert 'warnings' not in j


def test_404_patient_unknown_to_eps_passes_the_rows(client, seeded):
    j, ids = _read(client, Eps(_resp(404, {'error': 'not found'})))
    assert ids == ['obs-A', 'obs-B', 'obs-other']
    assert 'warnings' not in j


# ── a block, in the shape eps really sends ───────────────────────────────

@pytest.mark.parametrize('key', ['items', 'blocks', 'entry'])
def test_200_with_a_block_drops_the_blocked_source(client, seeded, key):
    """eps answers {"items": [...], "total": N}. The client read `blocks` /
    `entry` only, so this — the control working at all — never happened."""
    payload = {key: [_eps_block(PROV_A)], 'total': 1}
    j, ids = _read(client, Eps(_resp(200, payload)))
    assert ids == ['obs-B', 'obs-other']       # PROV_A's row is gone
    assert 'warnings' not in j


def test_items_wins_over_the_temporary_blocks_duplicate(client, seeded):
    payload = {'items': [_eps_block(PROV_A)], 'blocks': [], 'total': 1}
    _, ids = _read(client, Eps(_resp(200, payload)))
    assert ids == ['obs-B', 'obs-other']


def test_the_real_eps_item_parses_field_for_field(app):
    raw = _eps_block(PROV_A)
    raw.update(lift_kind='indispensable_care', lift_concept_guids=['c-1'],
               lift_from_date='2026-01-01', lift_until_date='2026-12-31')
    with app.app_context(), patch.object(
            ips_mod.requests, 'get',
            return_value=_resp(200, {'items': [raw], 'total': 1})):
        blocks = ips_mod.IpsClient(base_url=EPS, api_key='k').fetch_active_blocks(PATIENT)
    assert len(blocks) == 1, 'eps sends the list under "items"'
    b = blocks[0]
    assert (b.guid, b.patient_guid, b.source_scope_type, b.source_scope_id,
            b.is_active, b.lift_kind, b.lift_concept_guids,
            b.lift_from_date, b.lift_until_date) == (
        raw['guid'], PATIENT, 'clinic', PROV_A, True,
        'indispensable_care', ['c-1'], '2026-01-01', '2026-12-31')


# ── "could not find out" ─────────────────────────────────────────────────

UNKNOWN = [
    ('http_403', lambda: _resp(403, {'error': 'forbidden'})),
    ('http_401', lambda: _resp(401, {'error': 'unauthorized'})),
    ('http_500', lambda: _resp(500, None)),
    ('http_503', lambda: _resp(503, None)),
    ('network', lambda: requests.ConnectionError('refused')),
    ('network', lambda: requests.Timeout('slow')),
    ('unreadable_payload', lambda: _resp(200, {'total': 0})),
    ('unreadable_payload', lambda: _resp(200, None)),
    ('redacted_block', lambda: _resp(
        200, {'items': [_eps_block(PROV_A, redacted=True)], 'total': 1})),
]
IDS = ['403', '401', '500', '503', 'conn-refused', 'timeout',
       '200-no-list', '200-not-json', '200-redacted-block']


@pytest.mark.parametrize('reason,answer', UNKNOWN, ids=IDS)
def test_unknown_block_state_withholds_that_patients_rows(
        client, seeded, caplog, reason, answer):
    with caplog.at_level('WARNING'):
        j, ids = _read(client, Eps(answer()))
    # PATIENT's two rows are withheld; OTHER, whom eps answered for, is not.
    assert ids == ['obs-other']
    assert j['total'] == 1
    assert len(j.get('warnings') or []) == 1, 'the response says rows were withheld'
    w = j['warnings'][0]
    assert j.get('meta'), 'and tags the bundle'
    assert (w['code'], w['patients_withheld'], w['entries_withheld'],
            w['reasons']) == ('sparr_block_state_unknown', 1, 2, [reason])
    assert j['meta']['tag'][0]['code'] == 'sparr_block_state_unknown'
    assert PATIENT not in r_text(j['warnings']), 'the response names no patient'
    assert any('sparr.block_state_unknown ENFORCED' in m and reason in m
               for m in caplog.messages)
    audit = AuditLog.query.filter_by(event_type='observations.sparr_unknown').one()
    assert audit.payload_snapshot['patients'] == {PATIENT: reason}
    assert audit.payload_snapshot['enforced'] is True
    read = AuditLog.query.filter_by(event_type='observations.read').one()
    assert read.payload_snapshot['patient_guids'] == [OTHER]


def r_text(obj):
    import json
    return json.dumps(obj)


@pytest.mark.parametrize('reason,answer', UNKNOWN, ids=IDS)
def test_shadow_mode_passes_the_rows_and_logs(client, seeded, caplog, reason, answer):
    seeded.config['SPARR_FAIL_CLOSED'] = False
    with caplog.at_level('WARNING'):
        j, ids = _read(client, Eps(answer()))
    assert ids == ['obs-A', 'obs-B', 'obs-other']
    assert 'warnings' not in j and 'meta' not in j
    assert any('sparr.block_state_unknown SHADOW' in m and reason in m
               and 'entries=2' in m for m in caplog.messages)
    audit = AuditLog.query.filter_by(event_type='observations.sparr_unknown').one()
    assert audit.payload_snapshot['enforced'] is False
    assert audit.payload_snapshot['entries_affected'] == 2


def test_unset_eps_address_is_unknown_not_no_blocks(client, seeded, caplog):
    seeded.config['IPS_BASE_URL'] = ''
    eps = Eps(_resp(200, {'items': []}))
    with patch.dict('os.environ', {'EPS_BASE_URL': '', 'IPS_BASE_URL': ''}), \
            caplog.at_level('WARNING'):
        j, ids = _read(client, eps)
    assert ids == []                       # nobody's state is known
    assert eps.calls == [], 'nothing to call'
    assert (j.get('warnings') or [{}])[0].get('reasons') == ['not_configured']
    assert j['warnings'][0]['patients_withheld'] == 2


def test_unset_eps_address_in_shadow_passes_and_logs(client, seeded, caplog):
    seeded.config.update(IPS_BASE_URL='', SPARR_FAIL_CLOSED=False)
    with patch.dict('os.environ', {'EPS_BASE_URL': '', 'IPS_BASE_URL': ''}), \
            caplog.at_level('WARNING'):
        j, ids = _read(client, Eps(_resp(200, {'items': []})))
    assert ids == ['obs-A', 'obs-B', 'obs-other']
    assert sum('SHADOW' in m and 'not_configured' in m for m in caplog.messages) == 2


# ── a failure is not cached ──────────────────────────────────────────────

def test_a_failure_is_not_cached(client, seeded):
    """403 then 200-empty within the TTL: the second read must ASK AGAIN and
    show the rows. Before #1047 the 403 was cached as "no blocks" for 30 s."""
    eps = Eps(_resp(403, {'error': 'forbidden'}))
    _, ids = _read(client, eps)
    assert ids == ['obs-other']
    eps.answer = _resp(200, {'items': [], 'total': 0})
    _, ids = _read(client, eps)
    assert ids == ['obs-A', 'obs-B', 'obs-other']
    assert len(eps.for_patient()) == 2, 'eps was asked again for the failed patient'


def test_a_failure_does_not_mask_a_block_that_then_arrives(client, seeded):
    """The dangerous direction: 403 (which used to cache `[]`) then a real
    block. The block must apply on the very next read."""
    eps = Eps(_resp(403, {'error': 'forbidden'}))
    _read(client, eps)
    eps.answer = _resp(200, {'items': [_eps_block(PROV_A)], 'total': 1})
    _, ids = _read(client, eps)
    assert ids == ['obs-B', 'obs-other']


def test_a_good_answer_is_still_cached(client, seeded):
    eps = Eps(_resp(200, {'items': [], 'total': 0}))
    _read(client, eps)
    _read(client, eps)
    assert len(eps.for_patient()) == 1


# ── the flag ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('raw,expected', [
    (None, True), ('', True), ('true', True), ('1', True), ('enforce', True),
    ('false', False), ('0', False), ('no', False), ('off', False), ('FALSE', False),
])
def test_the_flag_defaults_to_enforce(raw, expected):
    """Only an explicit off turns it off; unset, empty or a typo enforces."""
    import importlib
    import config as config_mod
    env = {} if raw is None else {'SPARR_FAIL_CLOSED': raw}
    with patch.dict('os.environ', env):
        if raw is None:
            import os
            os.environ.pop('SPARR_FAIL_CLOSED', None)
        got = getattr(importlib.reload(config_mod).Config, 'SPARR_FAIL_CLOSED', None)
    importlib.reload(config_mod)
    assert got is expected
