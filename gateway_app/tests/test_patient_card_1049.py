"""#1049 — POST /api/v1/patient-card, end to end (contract s7.3).

Every test posts to the real route with its real service-key check (there is
no AUTH_DISABLED bypass on this door), then — where it matters — runs the
real forwarding cycle. ONLY the outbound HTTP boundary is patched
(`requests.get` / `requests.post`): eps answers the clinics lookup, the CDR
receives `/api/v1/ingest`. The QuestionnaireResponse is built from the real
EPS form (tests/fixtures/eps_patient_v1.json, guarded in test_eps_review_1043).
Synthetic data only.
"""
import json
import uuid
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.models import AuditLog, CdrDeliveryLog
from app.services import cdr_forwarder, patient_sync
from app.services.fhir_observation_builder import build_fhir_observation
from tests.test_eps_review_1043 import _entry, _leaf

KEY = 'k-' + 'x' * 40
PATIENT = str(uuid.uuid4())
UNIT = str(uuid.uuid4())
OTHER_UNIT = str(uuid.uuid4())
EPS_URL = 'http://mock-eps'


def card_qr():
    """allergy: status + two entries = 1 + 3 + 2 answers."""
    return {'item': [
        _leaf('allergy.status', 'has'),
        _entry('allergy', substance='Penicillin', reaction='Utslag', severity='severe'),
        _entry('allergy', substance='Jordnötter', severity='mild'),
    ]}


N_ROWS = 6


def body(**over):
    b = {'patient_guid': PATIENT, 'care_unit_guid': UNIT, 'section': 'allergy',
         'card_version': str(uuid.uuid4()), 'questionnaire_response': card_qr()}
    b.update(over)
    return b


HEADERS = {'X-Source-Service': 'patientapp.pdhc', 'X-Service-Key': KEY}


class World:
    def __init__(self):
        self.ingested = []
        self.calls = []
        # eps: the patient's clinics, as GET /api/v1/patients/<g>/clinics answers
        self.clinics = [{'guid': str(uuid.uuid4()), 'organisation_guid': UNIT,
                         'care_unit_guid': UNIT, 'is_active': True}]
        self.eps_status = 200
        self.eps_down = False
        self.eps_resolved = None    # X-PDHC-Patient-Guid; default = the asked guid

    @staticmethod
    def _resp(status, payload, headers=None):
        r = MagicMock(status_code=status, text=json.dumps(payload))
        r.json.return_value = payload
        r.headers = headers or {}
        return r

    def get(self, url, **kw):
        self.calls.append(('GET', url, kw.get('headers') or {}))
        if url.startswith(EPS_URL) and url.endswith('/clinics'):
            if self.eps_down:
                raise requests.ConnectionError('eps is down')
            guid = url.split('/')[-2]
            return self._resp(self.eps_status, self.clinics if self.eps_status == 200
                              else {'error': 'x'},
                              {'X-PDHC-Patient-Guid': self.eps_resolved or guid})
        raise AssertionError(f'unexpected GET {url}')

    def post(self, url, json=None, **kw):
        self.calls.append(('POST', url, kw.get('headers') or {}))
        if url.endswith('/api/v1/ingest'):
            self.ingested.append(json)
            return self._resp(201, {'guid': str(uuid.uuid4())})
        if url.endswith('/provider/validate-token'):
            return self._resp(200, {'valid': True, 'provider_org_guid': str(uuid.uuid4()),
                                    'contract_guid': str(uuid.uuid4()),
                                    'scopes': 'read,write,patient_generated',
                                    'delivery_mode': 'poll'})
        raise AssertionError(f'unexpected POST {url}')

    @property
    def eps_calls(self):
        return [c for c in self.calls if c[1].startswith(EPS_URL)]


@pytest.fixture
def world(app, db):
    w = World()
    patient_sync.reset_cache()
    saved = {k: app.config.get(k) for k in (
        'CDR_FORWARDING_ENABLED', 'PATIENTAPP_PDHC_SERVICE_KEY',
        'IPS_BASE_URL', 'IPS_API_KEY')}
    app.config.update(CDR_FORWARDING_ENABLED=True, PATIENTAPP_PDHC_SERVICE_KEY=KEY,
                      IPS_BASE_URL=EPS_URL, IPS_API_KEY='gateway-own-eps-key')
    with patch.object(requests, 'post', side_effect=w.post), \
         patch.object(requests, 'get', side_effect=w.get):
        yield w
    app.config.update(saved)
    patient_sync.reset_cache()


def post(client, b=None, headers=HEADERS):
    return client.post('/api/v1/patient-card', json=b if b is not None else body(),
                       headers=headers)


def n_rows():
    return CdrDeliveryLog.query.count()


# ── 1. the door: this key, this route, nothing else ──────────────────────

@pytest.mark.parametrize('headers', [
    {},
    {'X-Service-Key': KEY},                                         # no source
    {'X-Source-Service': 'gateway.pdhc', 'X-Service-Key': KEY},     # wrong source
    {'X-Source-Service': 'patientapp.pdhc'},                        # no key
    {'X-Source-Service': 'patientapp.pdhc', 'X-Service-Key': KEY + 'y'},
    {'X-Source-Service': 'patientapp.pdhc', 'X-Service-Key': KEY[:-1]},
    {'X-Provider-Token': 'pat-patient-app'},                        # a PAT
    {'Authorization': 'Bearer sso-token'},                          # an SSO bearer
])
def test_only_the_patientapp_key_opens_the_route(client, world, headers):
    r = post(client, headers=headers)
    assert r.status_code == 401, r.get_json()
    assert r.get_json()['error'] == 'invalid_service_credentials'
    assert n_rows() == 0 and not world.eps_calls


def test_an_unset_key_admits_nobody(client, app, world):
    app.config['PATIENTAPP_PDHC_SERVICE_KEY'] = ''
    for h in (HEADERS, {'X-Source-Service': 'patientapp.pdhc', 'X-Service-Key': ''}):
        r = post(client, headers=h)
        assert r.status_code == 401
    assert n_rows() == 0 and not world.eps_calls
    reasons = [a.payload_snapshot['reason'] for a in
               AuditLog.query.filter_by(event_type='patient_card.auth_rejected')]
    assert reasons == ['not_configured', 'not_configured']


def test_auth_disabled_does_not_open_it(client, app, world):
    assert app.config['AUTH_DISABLED'] is True     # TestConfig
    assert post(client, headers={}).status_code == 401


@pytest.mark.parametrize('method,path', [
    ('GET', '/api/v1/provider/feed'),
    ('GET', f'/api/v1/provider/download/{uuid.uuid4()}'),
    ('POST', f'/api/v1/provider/report/{uuid.uuid4()}'),
    ('POST', f'/api/v1/provider/status/{uuid.uuid4()}'),
    ('POST', f'/api/v1/provider/receipt/{uuid.uuid4()}/ack'),
    ('GET', f'/api/v1/observations?organization={UNIT}'),
])
def test_the_key_opens_nothing_else(client, world, method, path):
    # Anything but a refusal — including the route running and failing
    # inside, which TESTING propagates — is the key having opened it.
    try:
        r = client.open(path, method=method, headers=HEADERS, json={'x': 1})
        got = r.status_code
    except Exception as e:   # noqa: BLE001
        got = f'route ran: {type(e).__name__}'
    assert got == 401, (path, got)
    assert n_rows() == 0


# ── 2. body and section ──────────────────────────────────────────────────

def test_an_item_outside_the_section_is_422_and_nothing_is_stored(client, world):
    qr = card_qr()
    qr['item'].append(_leaf('medication.status', 'has'))
    r = post(client, body(questionnaire_response=qr))
    assert r.status_code == 422, r.get_json()
    assert r.get_json()['error'] == 'validation'
    assert {'linkId': 'medication.status', 'code': 'outside_section'} in r.get_json()['details']
    assert n_rows() == 0 and not world.eps_calls


def test_a_nested_item_outside_the_section_is_caught_too(client, world):
    qr = card_qr()
    qr['item'][1]['item'].append(_leaf('history.condition', 'Astma'))
    r = post(client, body(questionnaire_response=qr))
    assert r.status_code == 422
    assert {'linkId': 'history.condition', 'code': 'outside_section'} in r.get_json()['details']


@pytest.mark.parametrize('over,field', [
    ({'section': 'diagnosis'}, 'section'),
    ({'section': None}, 'section'),
    ({'patient_guid': 'not-a-guid'}, 'patient_guid'),
    ({'care_unit_guid': None}, 'care_unit_guid'),
    ({'card_version': '1'}, 'card_version'),
    ({'questionnaire_response': None}, 'questionnaire_response'),
])
def test_a_malformed_body_is_422(client, world, over, field):
    r = post(client, body(**over))
    assert r.status_code == 422
    assert field in {d.get('field') for d in r.get_json()['details']}
    assert n_rows() == 0


def test_a_response_to_another_questionnaire_is_422(client, world):
    qr = card_qr()
    qr['questionnaire'] = 'https://forms.pdhc.se/Questionnaire/other'
    assert post(client, body(questionnaire_response=qr)).status_code == 422


def test_an_answer_gateway_cannot_store_is_refused_not_dropped(client, world):
    qr = {'item': [{'linkId': 'allergy.status', 'answer': [{'valueAttachment': {}}]}]}
    r = post(client, body(questionnaire_response=qr))
    assert r.status_code == 422
    assert r.get_json()['details'] == [{'linkId': 'allergy.status', 'code': 'wrong_type'}]
    assert n_rows() == 0


def test_a_section_with_no_answer_is_422(client, world):
    qr = {'item': [{'linkId': 'allergy.status'}]}
    assert post(client, body(questionnaire_response=qr)).status_code == 422
    assert n_rows() == 0


# ── 3. eps: listed, actively, at that unit ──────────────────────────────

def test_eps_is_asked_with_gateways_own_key(client, world):
    assert post(client).status_code == 202
    (_, url, headers), = world.eps_calls
    assert url == f'{EPS_URL}/api/v1/patients/{PATIENT}/clinics'
    assert headers.get('Authorization') == 'ApiKey gateway-own-eps-key'
    assert KEY not in json.dumps(headers)


@pytest.mark.parametrize('setup', [
    lambda w: setattr(w, 'clinics', [{'organisation_guid': OTHER_UNIT,
                                      'care_unit_guid': OTHER_UNIT, 'is_active': True}]),
    lambda w: setattr(w, 'clinics', []),
    lambda w: setattr(w, 'clinics', [{'organisation_guid': UNIT, 'care_unit_guid': UNIT,
                                      'is_active': False}]),
    # the eps CLINIC guid is not the unit: matching on it would be wrong
    lambda w: setattr(w, 'clinics', [{'guid': UNIT, 'organisation_guid': OTHER_UNIT,
                                      'care_unit_guid': OTHER_UNIT, 'is_active': True}]),
    lambda w: setattr(w, 'eps_status', 404),
    # eps resolved the value as a different patient (a FHIR resource_id)
    lambda w: setattr(w, 'eps_resolved', str(uuid.uuid4())),
])
def test_not_at_the_unit_is_403_and_nothing_is_stored(client, world, setup):
    setup(world)
    r = post(client)
    assert r.status_code == 403, r.get_json()
    assert r.get_json()['error'] == 'patient_not_at_unit'
    assert n_rows() == 0
    assert AuditLog.query.filter_by(event_type='patient_card.rejected').count() == 1


@pytest.mark.parametrize('setup', [
    lambda w, app: setattr(w, 'eps_down', True),
    lambda w, app: setattr(w, 'eps_status', 500),
    lambda w, app: setattr(w, 'eps_status', 401),
    lambda w, app: setattr(w, 'eps_status', 403),
    lambda w, app: setattr(w, 'clinics', {'unexpected': 'shape'}),
    lambda w, app: app.config.update(IPS_BASE_URL=''),
])
def test_eps_unreachable_fails_closed(client, app, world, setup, monkeypatch):
    monkeypatch.delenv('EPS_BASE_URL', raising=False)
    monkeypatch.delenv('IPS_BASE_URL', raising=False)
    setup(world, app)
    r = post(client)
    assert r.status_code == 503, r.get_json()
    assert r.get_json()['error'] == 'eps_unavailable'
    assert n_rows() == 0


# ── 4. the rows, and what the CDR receives ──────────────────────────────

def test_202_with_receipt_and_row_count(client, world, db):
    r = post(client)
    assert r.status_code == 202, r.get_json()
    j = r.get_json()
    assert set(j) == {'receipt_token', 'rows'} and j['rows'] == N_ROWS
    parent = db.session.get(CdrDeliveryLog, j['receipt_token'])
    assert parent.status == 'skipped' and parent.service_request_guid is None
    assert CdrDeliveryLog.query.filter_by(status='pending').count() == N_ROWS


def test_every_answer_reaches_the_cdr_as_a_card_review_row(client, app, world):
    b = body()
    receipt = post(client, b).get_json()['receipt_token']
    assert cdr_forwarder.run_forwarding_cycle(app) == N_ROWS
    assert len(world.ingested) == N_ROWS
    assert CdrDeliveryLog.query.filter_by(status='delivered').count() == N_ROWS
    for p in world.ingested:
        cc = p['clinical_context']
        assert p['patient_guid'] == PATIENT
        assert cc['review_required'] is True
        assert cc['data_origin'] == 'patient_generated'
        assert cc['patient_org_guid'] == UNIT
        assert cc.get('card_section') == 'allergy'
        assert cc.get('card_version') == b['card_version']
        assert cc.get('source_service') == 'patientapp.pdhc'
        assert cc['link_id'] == cc['transaction_guid'] and cc['link_id'].startswith('allergy.')
        # no ServiceRequest, no grant, no contract, no provider
        assert cc['service_request_guid'] is None
        assert cc['contract_guid'] is None and cc['provider_org_guid'] is None
        fr = p['fhir_resource']
        assert 'basedOn' not in fr
        assert fr.get('performer') == [{'reference': f'Patient/{PATIENT}',
                                    'type': 'Patient', 'display': 'Patient'}]
        assert fr['id'] and fr['code']['text']
        assert {'system': 'urn:pdhc:fhir:CodeSystem:data-origin',
                'code': 'patient-generated',
                'display': 'Patient-generated data'} in fr['meta']['tag']
    groups = {p['clinical_context'].get('group') for p in world.ingested}
    assert None in groups                       # allergy.status is outside a group
    entry_groups = groups - {None}
    assert len(entry_groups) == 2 and all(g.startswith(receipt + '/') for g in entry_groups)
    # no call to request.pdhc or contract.pdhc on the way: there is no SR
    assert not [c for c in world.calls if 'mock-request' in c[1] or 'mock-contract' in c[1]]


def test_a_coded_answer_goes_as_its_code_with_the_display(client, app, world):
    post(client)
    cdr_forwarder.run_forwarding_cycle(app)
    sev = next(p for p in world.ingested if p['observation']['value'] == 'severe')
    cc = sev['fhir_resource']['valueCodeableConcept']
    assert cc['coding'][0]['code'] == 'severe'
    assert cc['coding'][0]['display'] not in ('', 'severe') and cc['text'] == cc['coding'][0]['display']


def test_the_submission_is_audited(client, world):
    b = body()
    j = post(client, b).get_json()
    found = AuditLog.query.filter_by(event_type='patient_card.received').all()
    assert len(found) == 1, found
    a = found[0]
    assert (a.actor_guid, a.data_subject_guid, a.resource_guid) == (
        'patientapp.pdhc', PATIENT, b['card_version'])
    assert a.payload_snapshot['rows'] == N_ROWS
    assert a.payload_snapshot['receipt_token'] == j['receipt_token']
    assert a.payload_snapshot['care_unit_guid'] == UNIT


def test_a_resent_version_is_one_receipt_and_a_reused_one_is_409(client, world):
    b = body()
    first = post(client, b).get_json()
    again = post(client, b)
    assert again.status_code == 202 and again.get_json() == first
    assert n_rows() == 1 + N_ROWS
    changed = dict(b, questionnaire_response={'item': [_leaf('allergy.status', 'none')]})
    r = post(client, changed)
    assert r.status_code == 409 and r.get_json()['error'] == 'card_version_reused'
    assert n_rows() == 1 + N_ROWS


def test_the_device_cannot_set_who_the_response_is_about(client, world, db):
    qr = card_qr()
    qr.update({'subject': {'reference': 'Patient/someone-else'},
               'resourceType': 'QuestionnaireResponse'})
    j = post(client, body(questionnaire_response=qr)).get_json()
    parent = db.session.get(CdrDeliveryLog, j['receipt_token'])
    stored = parent.fhir_observation_json['questionnaire_response']
    assert stored['subject'] == {'reference': f'Patient/{PATIENT}'}
    assert stored['questionnaire'].startswith('https://plan.pdhc.se/fhir/Questionnaire/eps-patient')


# ── 5. the forwarder's relaxation is for card rows only ─────────────────

def test_an_order_row_without_a_service_request_still_fails(client, app, world, db):
    """The NULL-SR allowance must not leak to rows that merely look similar:
    a pending row with the card keys but no `performer: patient` marker, and
    an ordinary row with no SR, are both refused as before."""
    for raw in ({'value': 1, 'response_type': 'numeric'},
                {'value': 'x', 'response_type': 'text', 'card_version': str(uuid.uuid4()),
                 'card_section': 'allergy', 'patient_org_guid': UNIT}):
        db.session.add(CdrDeliveryLog(patient_guid=PATIENT, concept_guid='c',
                                      fhir_observation_json=raw, status='pending'))
    db.session.commit()
    cdr_forwarder.run_forwarding_cycle(app)
    assert world.ingested == []
    rows = CdrDeliveryLog.query.all()
    assert {r.status for r in rows} == {'failed'}
    assert all('service_request_guid' in r.last_error for r in rows)


def test_a_card_row_without_its_care_unit_is_not_forwarded(client, app, world, db):
    post(client)
    for r in CdrDeliveryLog.query.filter_by(status='pending'):
        doc = dict(r.fhir_observation_json)
        doc.pop('patient_org_guid')
        r.fhir_observation_json = doc
    db.session.commit()
    cdr_forwarder.run_forwarding_cycle(app)
    assert world.ingested == []
    assert {r.last_error for r in CdrDeliveryLog.query.filter_by(status='failed')} == {
        'Production-path canonical fields NULL: patient_org_guid'}


def test_an_order_row_keeps_its_provider_as_performer():
    row = MagicMock(patient_guid=PATIENT, service_request_guid=str(uuid.uuid4()),
                    provider_org_guid=UNIT, concept_guid='c', contract_guid=None,
                    received_at=None, author_org_guid=None, data_origin=None,
                    transaction_guid=None, guid='g',
                    fhir_observation_json={'value': 1, 'response_type': 'numeric',
                                           'performer': 'patient'})
    obs = build_fhir_observation(row)
    assert obs['performer'][0]['type'] == 'Organization'
