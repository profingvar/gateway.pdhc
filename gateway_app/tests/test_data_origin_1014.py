"""#1014 — patient-generated data is flagged end to end through gateway.

Opt-in: 'patient_generated', or absent (= produced/endorsed by healthcare).
Pinned: stored on the delivery log (per observation, else report level),
forwarded to cdr in clinical_context, tagged on the FHIR Observation, and an
unknown value is refused before anything is stored.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.models import CdrDeliveryLog
from app.services import cdr_forwarder, data_origin
from app.services.fhir_observation_builder import build_fhir_observation
from app.services.sr_context import SRContextResult
from tests.test_report_submission import _make_minimal_body, _patch_all_upstreams


def _post(client, body, sr='sr-origin'):
    p = _patch_all_upstreams()
    with p[0], p[1], p[2], p[3]:
        return client.post(f'/api/v1/provider/report/{sr}', json=body,
                           headers={'X-Provider-Token': 'valid-token'})


def _origins(app, sr='sr-origin'):
    with app.app_context():
        return {r.transaction_guid: r.data_origin
                for r in CdrDeliveryLog.query.filter_by(service_request_guid=sr)}


def test_report_level_flag_marks_every_observation(client, app, db):
    body = _make_minimal_body()
    body['data_origin'] = 'patient_generated'
    r = _post(client, body)
    assert r.status_code == 202, r.get_json()
    assert _origins(app) == {'tx-001': 'patient_generated',
                             'tx-002': 'patient_generated'}


def test_per_observation_flag_marks_only_that_one(client, app, db):
    body = _make_minimal_body()
    body['report_payload']['observations'][1]['data_origin'] = 'patient_generated'
    r = _post(client, body)
    assert r.status_code == 202, r.get_json()
    assert _origins(app) == {'tx-001': None, 'tx-002': 'patient_generated'}


def test_absent_means_healthcare_and_stays_null(client, app, db):
    r = _post(client, _make_minimal_body())
    assert r.status_code == 202, r.get_json()
    assert set(_origins(app).values()) == {None}


def test_unknown_report_level_value_is_refused(client, app, db):
    body = _make_minimal_body()
    body['data_origin'] = 'healthcare'
    r = _post(client, body)
    assert r.status_code == 422, r.get_json()
    assert _origins(app) == {}


def test_unknown_per_observation_value_is_refused(client, app, db):
    body = _make_minimal_body()
    body['report_payload']['observations'][0]['data_origin'] = 'patient-generated'
    r = _post(client, body)
    assert r.status_code == 422, r.get_json()
    assert 'data_origin' in str(r.get_json())
    assert _origins(app) == {}


def _row(**over):
    base = dict(guid='log-1', patient_guid='pat-1', service_request_guid='sr-1',
                transaction_guid='tx-1', concept_guid='con-1', contract_guid='c-1',
                provider_org_guid='org-1', author_org_guid=None, data_origin=None,
                received_at=None, fhir_observation_json={}, cdr_resource_id=None)
    base.update(over)
    return SimpleNamespace(**base)


def _cc(row):
    with patch.object(cdr_forwarder.SRContextService, 'fetch',
                      return_value=SRContextResult(found=True, data={})), \
         patch.object(cdr_forwarder, 'build_fhir_observation', return_value={}), \
         patch.object(cdr_forwarder, 'build_canonical_observation', return_value={}):
        return cdr_forwarder._build_payload(row)['clinical_context']


def test_forwarded_to_cdr_in_clinical_context():
    assert _cc(_row(data_origin='patient_generated'))['data_origin'] == 'patient_generated'
    assert _cc(_row())['data_origin'] is None


def test_a_row_queued_before_the_column_still_forwards():
    row = _row()
    del row.data_origin
    assert _cc(row)['data_origin'] is None


def test_fhir_observation_is_tagged_only_when_patient_generated():
    obs = build_fhir_observation(_row(data_origin='patient_generated'), {}, {})
    tags = (obs.get('meta') or {}).get('tag') or []
    assert {'system': data_origin.TAG_SYSTEM, 'code': data_origin.TAG_CODE} \
        .items() <= tags[0].items()
    assert data_origin.EXT_URL in {e['url'] for e in obs.get('extension', [])}
    # performer still carries the submitter
    assert obs['performer'][0]['identifier']['value'] == 'org-1'

    plain = build_fhir_observation(_row(), {}, {})
    assert not (plain.get('meta') or {}).get('tag')
    assert data_origin.EXT_URL not in {e['url'] for e in plain.get('extension', [])}


# --- enforced from the credential -------------------------------------------

def _post_as_patient_app(client, body, sr='sr-origin'):
    """Same upstream mocks, but the PAT carries the patient_generated scope."""
    from app.services.pat_validation import PATValidationService, PATValidationResult
    real = PATValidationService.validate

    def validate(raw):
        r = real(raw)
        if r.valid:
            r.scopes = (r.scopes or 'read,write') + ',patient_generated'
        return r
    p = _patch_all_upstreams()
    with p[0], p[1], p[2], p[3], \
         patch.object(PATValidationService, 'validate', side_effect=validate):
        return client.post(f'/api/v1/provider/report/{sr}', json=body,
                           headers={'X-Provider-Token': 'valid-token'})


def test_patient_app_pat_flags_everything_even_when_body_omits_it(client, app, db):
    r = _post_as_patient_app(client, _make_minimal_body())
    assert r.status_code == 202, r.get_json()
    assert _origins(app) == {'tx-001': 'patient_generated',
                             'tx-002': 'patient_generated'}


def test_patient_app_pat_still_refuses_an_unknown_value(client, app, db):
    body = _make_minimal_body()
    body['data_origin'] = 'healthcare'
    r = _post_as_patient_app(client, body)
    assert r.status_code == 422, r.get_json()
    assert _origins(app) == {}


def test_forced_by_credential_contract():
    from app.services.pat_validation import PATValidationResult
    assert data_origin.forced_by_credential(
        PATValidationResult(valid=True, scopes='read,write,patient_generated')) == 'patient_generated'
    assert data_origin.forced_by_credential(
        PATValidationResult(valid=True, scopes='read,write')) is None
    assert data_origin.forced_by_credential(None) is None
