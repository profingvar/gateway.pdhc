"""#1043 — the EPS review marker, end to end through gateway.

Contract: plans/patient_app_contract.md s3.2 and its Deviations ([cdr], [ips]).

A patient's answers to the EPS form reach the CDR only as rows awaiting care's
review. Each test posts to the real `POST /api/v1/provider/report/<sr>` with
auth ON — the PAT is validated by the real decorator against a (fake)
request.pdhc — and then runs the real forwarding cycle. ONLY the outbound HTTP
boundary is patched: `requests.get` / `requests.post`. What the CDR would
receive is read off the POST to `/api/v1/ingest`.

The form is the real one: tests/fixtures/eps_patient_v1.json is a byte copy of
plans/PROMs/eps_patient_v1.json (guarded below).
"""
import copy
import hashlib
import json
import pathlib
import uuid
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.models import AuditLog, CdrDeliveryLog, GuidResolutionCache
from app.services import cdr_forwarder, patient_sync, review

FIXTURE = pathlib.Path(__file__).parent / 'fixtures' / 'eps_patient_v1.json'
FORM = json.loads(FIXTURE.read_text(encoding='utf-8'))
EPS = 'https://plan.pdhc.se/fhir/Questionnaire/eps-patient'

PATIENT = str(uuid.uuid4())
PATIENT_ORG = str(uuid.uuid4())
APP_ORG = str(uuid.uuid4())          # the patient app's provider organisation
CONTRACT = str(uuid.uuid4())
PAT_PATIENT_APP = 'pat-patient-app'  # scope read,write,patient_generated
PAT_HEALTHCARE = 'pat-healthcare'    # scope read,write


def test_the_fixture_is_the_real_form():
    assert (FORM['url'], str(FORM['version'])) == (EPS, '1')
    original = pathlib.Path(__file__).parents[3] / 'plans' / 'PROMs' / 'eps_patient_v1.json'
    if not original.exists():
        pytest.skip('workspace copy not present')
    assert hashlib.sha256(original.read_bytes()).hexdigest() == \
        hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), \
        'the EPS form changed: re-copy it into tests/fixtures'


def test_the_real_form_has_the_shape_these_tests_rely_on():
    """No item carries a `code`, and each section nests one repeating group."""
    def walk(items):
        for i in items:
            yield i
            yield from walk(i.get('item') or [])
    every = list(walk(FORM['item']))
    assert not [i['linkId'] for i in every if i.get('code')]
    groups = [i for i in FORM['item'] if i['type'] == 'group']
    assert {g['linkId'] for g in groups} == {
        f'{s}.entry' for s in ('allergy', 'medication', 'history', 'vaccination', 'contact')}
    assert all(g.get('repeats') and g.get('item') for g in groups)


# ── a QuestionnaireResponse built FROM the form ──────────────────────────

def _answer(item, value):
    kind = item['type']
    if kind in ('choice', 'coding'):
        opt = next(o['valueCoding'] for o in item['answerOption']
                   if o['valueCoding']['code'] == value)
        return {'valueCoding': opt}
    return {{'string': 'valueString', 'text': 'valueString', 'integer': 'valueInteger',
             'date': 'valueDate', 'boolean': 'valueBoolean'}[kind]: value}


def _form_item(link_id, items=None):
    for i in items if items is not None else FORM['item']:
        if i['linkId'] == link_id:
            return i
        hit = _form_item(link_id, i.get('item') or [])
        if hit:
            return hit
    return None


def _leaf(link_id, value):
    item = _form_item(link_id)
    return {'linkId': link_id, 'text': item.get('text', ''), 'answer': [_answer(item, value)]}


def _entry(section, **fields):
    return {'linkId': f'{section}.entry',
            'item': [_leaf(f'{section}.{k}', v) for k, v in fields.items()]}


def eps_response():
    """Two allergies, one medicine, no known vaccinations."""
    return {
        'resourceType': 'QuestionnaireResponse', 'status': 'completed',
        'questionnaire': f'{EPS}|1',
        'meta': {'tag': [{'system': 'https://pdhc.se/fhir/tag', 'code': 'eps-review'}]},
        'item': [
            _leaf('allergy.status', 'has'),
            _entry('allergy', substance='Penicillin', reaction='Utslag', severity='severe'),
            _entry('allergy', substance='Jordnötter', severity='mild'),
            _leaf('medication.status', 'has'),
            _entry('medication', name='Metformin', dose='500 mg', prescribed='yes'),
            _leaf('vaccination.status', 'none'),
        ],
    }


N_ANSWERS = 3 + 3 + 2 + 3      # statuses + allergy 1 + allergy 2 + medication


# ── the world outside gateway ────────────────────────────────────────────

class World:
    def __init__(self):
        self.ingested = []            # bodies POSTed to the CDR's /api/v1/ingest
        self.patient_org = PATIENT_ORG
        self.request_down = False     # request.pdhc unreachable (context fetch)
        self.calls = []

    @staticmethod
    def _resp(status, payload):
        r = MagicMock(status_code=status, text=json.dumps(payload))
        r.json.return_value = payload
        return r

    def post(self, url, json=None, **kw):
        self.calls.append(('POST', url))
        if url.endswith('/provider/validate-token'):
            scopes = {PAT_PATIENT_APP: 'read,write,patient_generated',
                      PAT_HEALTHCARE: 'read,write'}.get(json.get('token'))
            if not scopes:
                return self._resp(401, {'message': 'unknown token'})
            return self._resp(200, {'valid': True, 'provider_org_guid': APP_ORG,
                                    'contract_guid': CONTRACT, 'scopes': scopes,
                                    'delivery_mode': 'poll'})
        if url.endswith('/internal/grant/validate'):
            return self._resp(200, {'valid': True, 'contract_guid': CONTRACT})
        if url.endswith('/api/v1/fhir/Patient'):
            return self._resp(200, {})
        if url.endswith('/api/v1/ingest'):
            self.ingested.append(json)
            return self._resp(201, {'guid': str(uuid.uuid4())})
        if '/internal/' in url:        # completion tracking etc.
            return self._resp(200, {})
        raise AssertionError(f'unexpected POST {url}')

    def get(self, url, **kw):
        self.calls.append(('GET', url))
        if '/internal/service-request/' in url and url.endswith('/context'):
            if self.request_down:
                raise requests.ConnectionError('request.pdhc is down')
            ctx = {'service_request_guid': url.split('/')[-2], 'status': 'active',
                   'patient_guid': PATIENT, 'contract_guid': CONTRACT,
                   'requesting_org_guid': str(uuid.uuid4()),
                   'plan_definition_guid': str(uuid.uuid4()),
                   'transactions': [{'transaction_guid': 'tx-bp', 'concept_guid': 'c-bp',
                                     'concept_name': 'Blodtryck', 'response_type': 'numeric',
                                     'unit': 'mmHg'}],
                   'goals': []}
            if self.patient_org:
                ctx['patient_org_guid'] = self.patient_org
            return self._resp(200, ctx)
        if 'mock-contract-service' in url:      # no scope defined
            return self._resp(404, {})
        raise AssertionError(f'unexpected GET {url}')


@pytest.fixture
def world(app, db):
    w = World()
    patient_sync.reset_cache()
    old = app.config.get('CDR_FORWARDING_ENABLED')
    app.config['CDR_FORWARDING_ENABLED'] = True
    with patch.object(requests, 'post', side_effect=w.post), \
         patch.object(requests, 'get', side_effect=w.get):
        yield w
    app.config['CDR_FORWARDING_ENABLED'] = old
    patient_sync.reset_cache()


def _report(client, payload, pat=PAT_PATIENT_APP, sr=None, **body):
    sr = sr or str(uuid.uuid4())
    r = client.post(f'/api/v1/provider/report/{sr}',
                    json={'grant_token': 'g', 'status': 'completed',
                          'report_payload': payload, **body},
                    headers={'X-Provider-Token': pat} if pat else {})
    return r, sr


def _forward(app, world):
    cdr_forwarder.run_forwarding_cycle(app)
    return {(p['clinical_context'].get('link_id'), p['clinical_context'].get('group')): p
            for p in world.ingested}


def _rows(sr):
    return CdrDeliveryLog.query.filter_by(service_request_guid=sr).all()


def test_the_report_route_needs_a_pat(client, world):
    r, _ = _report(client, eps_response(), pat=None)
    assert r.status_code == 401
    r, _ = _report(client, eps_response(), pat='not-a-token')
    assert r.status_code == 401


# ── 1. every EPS answer reaches the CDR, marked for review ───────────────

def test_every_eps_answer_is_forwarded_marked_for_review(client, app, world):
    r, sr = _report(client, eps_response())
    assert r.status_code == 202, r.get_json()
    assert r.get_json().get('review_required') is True
    assert r.get_json()['items_extracted'] == N_ANSWERS
    sent = _forward(app, world)
    # the form's items carry no `code`, and all of them still arrive
    assert len(world.ingested) == N_ANSWERS
    for p in world.ingested:
        cc = p['clinical_context']
        assert cc.get('review_required') is True, cc
        assert cc['data_origin'] == 'patient_generated'
        assert cc['patient_org_guid'] == PATIENT_ORG
        assert cc['provider_org_guid'] == APP_ORG
        assert cc['service_request_guid'] == sr
        # the linkId, section prefix intact, where the CDR reads it
        assert cc['transaction_guid'] == cc.get('link_id')
        assert cc['link_id'].split('.')[0] in ('allergy', 'medication', 'vaccination')
        assert p['fhir_resource'].get('id'), 'the CDR needs an Observation id'
        assert p['fhir_resource']['code']['text'], 'a label for the reviewer'
    assert sorted(k[0] for k in sent) == sorted([
        'allergy.status', 'medication.status', 'vaccination.status',
        'allergy.substance', 'allergy.reaction', 'allergy.severity',
        'medication.name', 'medication.dose', 'medication.prescribed'] + [
        'allergy.substance', 'allergy.severity'])


def test_values_arrive_typed_and_choices_as_their_code(client, app, world):
    _report(client, eps_response())
    sent = _forward(app, world)
    by_link = {}
    for (link, _), p in sent.items():
        by_link.setdefault(link, []).append(p['observation']['value'])
    # eps reads has/none/unknown and mild/severe: the CODE, not the Swedish display
    assert by_link.get('allergy.status') == ['has']
    assert by_link.get('vaccination.status') == ['none']
    assert sorted(by_link.get('allergy.severity') or []) == ['mild', 'severe']
    assert sorted(by_link.get('allergy.substance') or []) == ['Jordnötter', 'Penicillin']
    pen = next(p for p in world.ingested if p['observation']['value'] == 'Penicillin')
    assert pen['fhir_resource'].get('valueString') == 'Penicillin'
    sev = next(p for p in world.ingested if p['observation']['value'] == 'severe')
    assert sev['fhir_resource']['valueCodeableConcept']['coding'][0]['code'] == 'severe'


def test_a_coded_review_answer_keeps_the_display_the_patient_picked(client, app, world):
    """#1044: the CODE is the value (eps reads it), and the words the patient
    chose ride along, so the patient's status page can show them."""
    _report(client, eps_response())
    _forward(app, world)
    sev = next(p for p in world.ingested if p['observation']['value'] == 'severe')
    cc = sev['fhir_resource']['valueCodeableConcept']
    shown = cc['coding'][0]['display']
    assert cc['coding'][0]['code'] == 'severe'
    assert shown and shown != 'severe' and cc['text'] == shown
    # a free-text answer is untouched by this
    pen = next(p for p in world.ingested if p['observation']['value'] == 'Penicillin')
    assert 'valueCodeableConcept' not in pen['fhir_resource']


def test_time_and_url_answers_reach_the_cdr_as_text(client, app, world):
    """#1043: the PWA sends valueTime / valueUri; they used to become no row."""
    qr = {'resourceType': 'QuestionnaireResponse', 'status': 'completed',
          'questionnaire': 'https://forms.pdhc.se/Questionnaire/demo',
          'item': [{'linkId': 't', 'code': [{'code': 't.code', 'display': 'T'}],
                    'answer': [{'valueTime': '07:30:00'}]},
                   {'linkId': 'u', 'code': [{'code': 'u.code', 'display': 'U'}],
                    'answer': [{'valueUri': 'https://example.org/x'}]}]}
    r, _ = _report(client, qr)
    assert r.status_code == 202 and r.get_json()['items_extracted'] == 2
    _forward(app, world)
    got = {p['clinical_context']['link_id']: p['fhir_resource'].get('valueString')
           for p in world.ingested}
    assert got == {'t': '07:30:00', 'u': 'https://example.org/x'}


# ── 2. nested items and the repeating-group instance ─────────────────────

def test_the_fields_of_one_entry_share_a_group_and_entries_differ(client, app, world):
    _report(client, eps_response())
    sent = _forward(app, world)
    groups = {}
    for (link, group), p in sent.items():
        groups.setdefault(group, set()).add((link, p['observation']['value']))
    # the three section statuses are outside any group: no `group` key at all
    assert {l for l, _ in groups.get(None, set())} == {
        'allergy.status', 'medication.status', 'vaccination.status'}
    assert all('group' not in p['clinical_context']
               for (l, g), p in sent.items() if g is None)
    entries = {frozenset(v) for g, v in groups.items() if g is not None}
    assert entries == {
        frozenset({('allergy.substance', 'Penicillin'), ('allergy.reaction', 'Utslag'),
                   ('allergy.severity', 'severe')}),
        frozenset({('allergy.substance', 'Jordnötter'), ('allergy.severity', 'mild')}),
        frozenset({('medication.name', 'Metformin'), ('medication.dose', '500 mg'),
                   ('medication.prescribed', 'yes')}),
    }
    assert all(isinstance(g, str) and len(g) <= 64 for g in groups if g is not None)


def test_two_submissions_never_share_a_group(client, app, world):
    first = eps_response()
    second = eps_response()
    second['item'][1]['item'][0]['answer'] = [{'valueString': 'Latex'}]
    _report(client, first)
    _report(client, second)
    sent = _forward(app, world)
    owners = {}
    for (link, group), p in sent.items():
        if group is not None and link == 'allergy.substance':
            owners.setdefault(group, []).append(p['observation']['value'])
    assert sorted(len(v) for v in owners.values()) == [1, 1, 1, 1]


def test_nested_coded_items_of_an_ordinary_form_are_extracted_too(client, app, world):
    """Not review: an ordinary PROM that happens to nest. Its coded fields
    used to be dropped with the group; uncoded ones still are not forwarded."""
    qr = {'resourceType': 'QuestionnaireResponse', 'status': 'completed',
          'questionnaire': 'https://forms.pdhc.se/Questionnaire/demo',
          'item': [{'linkId': 'g', 'item': [
              {'linkId': 'g.q1', 'code': [{'code': 'q1.code', 'display': 'Q1'}],
               'answer': [{'valueInteger': 3}]},
              {'linkId': 'g.q2', 'answer': [{'valueString': 'no code'}]}]}]}
    r, sr = _report(client, qr)
    assert r.status_code == 202 and 'review_required' not in r.get_json()
    assert r.get_json()['items_extracted'] == 2
    _forward(app, world)
    assert [p['clinical_context']['link_id'] for p in world.ingested] == ['g.q1']
    assert 'review_required' not in world.ingested[0]['clinical_context']
    assert {x.transaction_guid: x.status for x in _rows(sr) if x.transaction_guid} == {
        'g.q1': 'delivered', 'g.q2': 'skipped'}


# ── which reports ask for review ─────────────────────────────────────────

def _variant(**change):
    qr = eps_response()
    for k, v in change.items():
        if v is None:
            qr.pop(k, None)
        else:
            qr[k] = v
    return qr


@pytest.mark.parametrize('qr,body', [
    (_variant(meta=None), {}),                                   # canonical with |1
    (_variant(meta=None, questionnaire=EPS), {}),                # canonical, bare
    (_variant(questionnaire='https://forms.pdhc.se/Questionnaire/other'), {}),  # tag only
    (_variant(meta=None, questionnaire='https://forms.pdhc.se/Questionnaire/other'),
     {'review_required': True}),                                 # the body says so
], ids=['canonical-versioned', 'canonical-bare', 'tag-only', 'body-flag'])
def test_each_trigger_marks_the_rows(client, app, world, qr, body):
    r, _ = _report(client, qr, **body)
    assert r.status_code == 202, r.get_json()
    _forward(app, world)
    assert len(world.ingested) == N_ANSWERS
    assert all(p['clinical_context'].get('review_required') is True
               for p in world.ingested)


def test_a_patient_form_that_is_not_the_eps_form_is_not_marked(client, app, world):
    qr = _variant(meta=None, questionnaire='https://forms.pdhc.se/Questionnaire/paid-5')
    r, sr = _report(client, qr)
    assert r.status_code == 202 and 'review_required' not in r.get_json()
    _forward(app, world)
    assert world.ingested == []        # no item code, no review: not forwarded
    assert {x.data_origin for x in _rows(sr)} == {'patient_generated'}


# ── refused by name, never silently downgraded ───────────────────────────

@pytest.mark.parametrize('qr,body', [
    (_variant(meta=None), {}),
    (_variant(questionnaire='https://forms.pdhc.se/Questionnaire/other'), {}),
    (_variant(meta=None, questionnaire='https://forms.pdhc.se/Questionnaire/other'),
     {'review_required': True}),
    ({'observations': [{'transaction_guid': 'tx-bp', 'value': 120,
                        'recorded_at': '2026-11-22T08:00:00Z'}]}, {'review_required': True}),
], ids=['canonical', 'tag', 'body-flag-qr', 'body-flag-observations'])
def test_a_review_claim_without_patient_generated_is_refused_by_name(
        client, app, world, qr, body):
    r, sr = _report(client, qr, pat=PAT_HEALTHCARE, **body)
    assert r.status_code == 403, r.get_json()
    assert r.get_json().get('code') == 'REVIEW_REQUIRES_PATIENT_GENERATED'
    assert 'patient_generated' in r.get_json()['message']
    assert _rows(sr) == [], 'nothing is stored'
    _forward(app, world)
    assert world.ingested == []
    audit = AuditLog.query.filter_by(event_type='report.rejected').one()
    assert audit.payload_snapshot['rejection_code'] == 'REVIEW_REQUIRES_PATIENT_GENERATED'


def test_a_healthcare_pat_still_submits_an_ordinary_form(client, app, world):
    """The control for the refusals above: same PAT, no review claim."""
    qr = _variant(meta=None, questionnaire='https://forms.pdhc.se/Questionnaire/paid-5')
    r, sr = _report(client, qr, pat=PAT_HEALTHCARE)
    assert r.status_code == 202
    assert {x.data_origin for x in _rows(sr)} == {None}


@pytest.mark.parametrize('flag', ['true', 1, 'yes'])
def test_a_review_flag_that_is_not_a_boolean_is_refused(client, world, flag):
    r, sr = _report(client, eps_response(), review_required=flag)
    assert (r.status_code, r.get_json().get('code')) == (422, 'INVALID_REVIEW_REQUIRED')
    assert _rows(sr) == []


def test_review_on_plain_observations_is_refused_not_ignored(client, world):
    obs = {'observations': [{'transaction_guid': 'tx-bp', 'value': 120,
                             'recorded_at': '2026-11-22T08:00:00Z'}]}
    r, sr = _report(client, obs, review_required=True)
    assert (r.status_code, r.get_json().get('code')) == (
        422, 'REVIEW_REQUIRES_QUESTIONNAIRE_RESPONSE')
    assert _rows(sr) == []


def test_a_link_id_too_long_for_the_key_is_refused(client, world):
    qr = eps_response()
    qr['item'].append({'linkId': 'contact.' + 'x' * 40,
                       'answer': [{'valueString': 'v'}]})
    r, sr = _report(client, qr)
    assert (r.status_code, r.get_json().get('code')) == (422, 'REVIEW_LINK_ID_UNUSABLE')
    assert _rows(sr) == []


# ── 3. never forwarded without the patient's organisation ────────────────

def test_a_review_report_is_refused_when_the_patient_org_is_unknown(client, app, world):
    world.patient_org = None
    r, sr = _report(client, eps_response())
    assert (r.status_code, r.get_json().get('code')) == (503, 'REVIEW_PATIENT_ORG_UNAVAILABLE')
    assert _rows(sr) == [], 'retryable: nothing was stored'
    _forward(app, world)
    assert world.ingested == []
    # and the same report is accepted once request.pdhc names the organisation
    GuidResolutionCache.query.filter_by(source_type='sr_context').delete()
    world.patient_org = PATIENT_ORG
    r2 = client.post(f'/api/v1/provider/report/{sr}',
                     json={'grant_token': 'g', 'status': 'completed',
                           'report_payload': eps_response()},
                     headers={'X-Provider-Token': PAT_PATIENT_APP})
    assert r2.status_code == 202, r2.get_json()


def test_an_ordinary_report_is_still_accepted_without_a_patient_org(client, app, world):
    """The control: the refusal above is for review rows only; the
    gateway -> cdr1 flow for everything else is unchanged."""
    world.patient_org = None
    obs = {'observations': [{'transaction_guid': 'tx-bp', 'value': 120,
                             'recorded_at': '2026-11-22T08:00:00Z'}]}
    r, _ = _report(client, obs)
    assert r.status_code == 202, r.get_json()
    _forward(app, world)
    [p] = world.ingested
    assert p['clinical_context']['patient_org_guid'] is None
    assert set(p['clinical_context']) == LEGACY_CONTEXT_KEYS


LEGACY_CONTEXT_KEYS = {
    'service_request_guid', 'transaction_guid', 'contract_guid', 'provider_org_guid',
    'author_org_guid', 'data_origin', 'plan_definition_guid', 'requesting_org_guid',
    'requester_user_guid', 'care_plan_guid', 'patient_org_guid'}


def test_delivery_survives_request_pdhc_being_down(client, app, world):
    """The organisation is captured on the row at ingest, so a delivery made
    while request.pdhc cannot be asked still carries it."""
    r, _ = _report(client, eps_response())
    assert r.status_code == 202
    GuidResolutionCache.query.filter_by(source_type='sr_context').delete()
    world.request_down = True
    _forward(app, world)
    assert len(world.ingested) == N_ANSWERS
    assert {p['clinical_context']['patient_org_guid'] for p in world.ingested} == {PATIENT_ORG}


def test_a_review_row_with_no_organisation_at_all_is_held_and_retried(client, app, world):
    """Belt and braces at the last hop: a review row that carries none and
    cannot get one is NOT posted to the CDR; it stays queued, with the reason
    named, and goes out once request.pdhc answers."""
    r, sr = _report(client, eps_response())
    assert r.status_code == 202
    for row in _rows(sr):
        if row.status == 'pending':
            doc = dict(row.fhir_observation_json)
            doc.pop('patient_org_guid')
            row.fhir_observation_json = doc
    from app.extensions import db as _db
    _db.session.commit()
    GuidResolutionCache.query.filter_by(source_type='sr_context').delete()
    _db.session.commit()
    world.request_down = True
    _forward(app, world)
    assert world.ingested == [], 'not forwarded without the patient org'
    held = [x for x in _rows(sr) if x.transaction_guid]
    assert {x.status for x in held} == {'pending'}
    assert all('review_patient_org_unavailable' in (x.last_error or '') for x in held)
    world.request_down = False
    for x in held:                      # skip the backoff wait
        x.last_attempt_at = None
        x.attempt_count = 0
    _db.session.commit()
    _forward(app, world)
    assert len(world.ingested) == N_ANSWERS
    assert {p['clinical_context']['patient_org_guid'] for p in world.ingested} == {PATIENT_ORG}


def test_a_review_row_that_is_not_patient_generated_is_never_sent(client, app, world):
    r, sr = _report(client, eps_response())
    for row in _rows(sr):
        row.data_origin = None
    from app.extensions import db as _db
    _db.session.commit()
    _forward(app, world)
    assert world.ingested == []
    assert {x.status for x in _rows(sr) if x.transaction_guid} == {'failed'}


def test_the_claim_rules_in_one_place():
    assert review.claim({'report_payload': eps_response()}) == (True, None)
    assert review.claim({'report_payload': {'observations': []}}) == (False, None)
    assert review.claim({'review_required': False,
                         'report_payload': _variant(meta=None, questionnaire='x')}) == (False, None)


# ── the feed and download proxies (request.pdhc owns their content) ──────

#: One feed item exactly as request.pdhc emitted it before #1043.
LEGACY_FEED_ITEM = {
    'service_request_guid': 'sr-1', 'match_guid': 'm-1', 'status': 'sent',
    'sr_status': 'active', 'title': 'CGM', 'intent': 'order', 'priority': 'routine',
    'contract_guid': 'c-1', 'created_at': '2026-11-20T08:00:00',
    'updated_at': '2026-11-20T08:00:00', 'period_start': None, 'period_end': None,
    'download_url': '/api/v1/provider/download/sr-1'}


class Feed:
    """request.pdhc behind the feed proxy. `leaky=True` is a request.pdhc that
    wrongly puts the patient guid on every item, whoever asks."""

    def __init__(self, leaky=False):
        self.leaky = leaky
        self.gets = []

    def post(self, url, json=None, **kw):
        assert url.endswith('/provider/validate-token')
        scopes = {PAT_PATIENT_APP: 'read,write,patient_generated',
                  PAT_HEALTHCARE: 'read,write'}.get(json.get('token'))
        if not scopes:
            return World._resp(401, {'message': 'unknown token'})
        return World._resp(200, {'valid': True, 'provider_org_guid': APP_ORG,
                                 'contract_guid': CONTRACT, 'scopes': scopes,
                                 'delivery_mode': 'poll'})

    def get(self, url, params=None, headers=None, **kw):
        self.gets.append({'url': url, 'params': params,
                          'token': headers.get('X-Provider-Token')})
        item = dict(LEGACY_FEED_ITEM)
        if self.leaky or headers.get('X-Provider-Token') == PAT_PATIENT_APP:
            item['patient_guid'] = PATIENT
        return World._resp(200, {'items': [item], 'total': 1})


def _feed(client, feed, pat, query=''):
    with patch.object(requests, 'post', side_effect=feed.post), \
         patch.object(requests, 'get', side_effect=feed.get):
        return client.get(f'/api/v1/provider/feed{query}',
                          headers={'X-Provider-Token': pat} if pat else {})


def test_the_feed_needs_a_pat(client, app, db):
    assert _feed(client, Feed(), None).status_code == 401
    assert _feed(client, Feed(), 'not-a-token').status_code == 401


def test_the_patient_app_sees_the_guid_and_can_filter(client, app, db):
    """`?patient_guid=` narrows the feed in request.pdhc; gateway must pass
    it on, or the patient app would be handed every patient's orders."""
    feed = Feed()
    r = _feed(client, feed, PAT_PATIENT_APP, f'?patient_guid={PATIENT}&limit=5')
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['items'][0].get('patient_guid') == PATIENT
    [seen] = feed.gets
    assert seen['url'].endswith('/provider/feed')
    assert seen['params'] == {'patient_guid': PATIENT, 'limit': '5'}
    assert seen['token'] == PAT_PATIENT_APP
    audit = AuditLog.query.filter_by(event_type='provider.feed.polled').one()
    assert audit.payload_snapshot.get('patient_guid') == PATIENT
    assert audit.data_subject_guid == PATIENT


@pytest.mark.parametrize('leaky', [False, True], ids=['request-correct', 'request-leaks'])
def test_an_ordinary_pat_gets_the_feed_exactly_as_before(client, app, db, leaky):
    """provider1 / cgm: the pre-#1043 item, byte for byte — also when
    request.pdhc (older, or wrong) sends the guid anyway."""
    r = _feed(client, Feed(leaky=leaky), PAT_HEALTHCARE)
    assert r.status_code == 200
    assert PATIENT not in r.get_data(as_text=True), 'no patient guid in the body'
    expected = {'items': [LEGACY_FEED_ITEM], 'total': 1}
    assert r.get_json() == expected
    with app.test_request_context():
        from flask import jsonify
        assert r.get_data() == jsonify(expected).get_data()
    audit = AuditLog.query.filter_by(event_type='provider.feed.polled').one()
    assert audit.data_subject_guid is None


def test_an_ordinary_pat_is_refused_the_patient_filter_by_name(client, app, db):
    """Refused before anything is asked of request.pdhc — never ignored,
    which would answer with every patient's orders."""
    feed = Feed(leaky=True)
    r = _feed(client, feed, PAT_HEALTHCARE, f'?patient_guid={PATIENT}')
    assert r.status_code == 403, r.get_json()
    assert r.get_json().get('code') == 'PATIENT_FILTER_REQUIRES_PATIENT_GENERATED'
    assert 'items' not in r.get_json()
    assert feed.gets == [], 'request.pdhc was not asked'
    assert AuditLog.query.filter_by(
        event_type='provider.feed.patient_filter_refused').count() == 1
    assert AuditLog.query.filter_by(event_type='provider.feed.polled').count() == 0
    # the same PAT without the filter still gets its feed
    assert _feed(client, feed, PAT_HEALTHCARE).status_code == 200
