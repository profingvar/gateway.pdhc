"""Tests — Report submission endpoint (reformed validation chain).

All upstream services (request.pdhc PAT, request.pdhc grant, request.pdhc SR context,
contract.pdhc scope) are mocked. Tests verify the full chain including:
- Minimal payload (no org_guid/contract_guid in body)
- Backward compat (org_guid/contract_guid cross-checked if provided)
- Grant delegation to request.pdhc
- SR context lookup + patient cross-check
- Contract scope enforcement
- Observation enrichment from SR context
- Obligatory concept enforcement on completed status
"""
import pytest
from unittest.mock import patch, MagicMock


# ── Mock helpers ────────────────────────────────────────────────────

def _mock_pat_ok(*args, **kwargs):
    class Resp:
        status_code = 200
        def json(self):
            return {
                'provider_org_guid': 'org-aaa',
                'contract_guid': 'contract-bbb',
                'scopes': 'read,write',
                'delivery_mode': 'poll',
            }
    return Resp()


def _mock_pat_read_only(*args, **kwargs):
    class Resp:
        status_code = 200
        def json(self):
            return {
                'provider_org_guid': 'org-aaa',
                'contract_guid': 'contract-bbb',
                'scopes': 'read',
                'delivery_mode': 'poll',
            }
    return Resp()


def _mock_grant_valid(*args, **kwargs):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        'valid': True,
        'contract_guid': 'contract-bbb',
        'grant_type': 'standard',
        'uses_remaining': None,
    }
    return resp


def _mock_grant_invalid(*args, **kwargs):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        'valid': False,
        'error': 'Invalid grant token',
    }
    return resp


def _mock_sr_context(*args, **kwargs):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        'service_request_guid': args[0].split('/')[-2] if args else 'sr-111',
        'status': 'active',
        'patient_guid': 'patient-222',
        'contract_guid': 'contract-bbb',
        'requester_org_guid': 'org-requester',
        'transactions': [
            {
                'transaction_guid': 'tx-001',
                'concept_guid': 'concept-001',
                'concept_name': 'Spirometri',
                'unit': 'percent',
                'unit_display': '% predicted',
                'range_min': 70.0,
                'range_max': 120.0,
                'requirement_type': 'required',
            },
            {
                'transaction_guid': 'tx-002',
                'concept_guid': 'concept-002',
                'concept_name': 'Blodtryck',
                'unit': 'mmHg',
                'unit_display': 'mmHg',
                'range_min': None,
                'range_max': None,
                'requirement_type': 'optional',
            },
        ],
        'goals': [],
    }
    return resp


def _mock_scope_ok(*args, **kwargs):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        'contract_guid': 'contract-bbb',
        'status': 'active',
        'scope_defined': True,
        'request_scope': [],
        'return_scope': {
            'obligatory_return': ['concept-001'],
            'optional_return': ['concept-002'],
        },
    }
    return resp


def _mock_scope_no_scope(*args, **kwargs):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        'contract_guid': 'contract-bbb',
        'status': 'active',
        'scope_defined': False,
        'request_scope': [],
        'return_scope': {},
    }
    return resp


def _mock_scope_revoked(*args, **kwargs):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        'contract_guid': 'contract-bbb',
        'status': 'revoked',
        'scope_defined': True,
        'request_scope': [],
        'return_scope': {},
    }
    return resp


def _patch_all_upstreams(pat_mock=_mock_pat_ok, grant_mock=_mock_grant_valid,
                         sr_context_mock=_mock_sr_context,
                         scope_mock=_mock_scope_ok):
    """Patch all upstream HTTP calls."""
    def _route_post(url, **kwargs):
        if 'validate-token' in url:
            return pat_mock(url, **kwargs)
        elif 'grant/validate' in url:
            return grant_mock(url, **kwargs)
        raise ValueError(f'Unexpected POST to {url}')

    def _route_get(url, **kwargs):
        if '/context' in url:
            return sr_context_mock(url, **kwargs)
        elif '/scope' in url:
            return scope_mock(url, **kwargs)
        raise ValueError(f'Unexpected GET to {url}')

    return (
        patch('app.services.pat_validation.http_requests.post', side_effect=_route_post),
        patch('app.services.grant_validation.http_requests.post', side_effect=_route_post),
        patch('app.services.sr_context.http_requests.get', side_effect=_route_get),
        patch('app.services.contract_scope.http_requests.get', side_effect=_route_get),
    )


def _make_minimal_body():
    """Minimal payload — no org_guid, no contract_guid."""
    return {
        'patient_guid': 'patient-222',
        'grant_token': 'valid-grant-token',
        'status': 'completed',
        'report_payload': {
            'observations': [
                {
                    'transaction_guid': 'tx-001',
                    'concept_guid': 'concept-001',
                    'value': 72,
                    'response_type': 'numeric',
                },
                {
                    'transaction_guid': 'tx-002',
                    'concept_guid': 'concept-002',
                    'value': 'normal',
                    'response_type': 'categorical',
                },
            ],
        },
    }


def _make_backward_compat_body():
    """Full payload with org_guid + contract_guid (backward compatible)."""
    body = _make_minimal_body()
    body['organisation_guid'] = 'org-aaa'
    body['contract_guid'] = 'contract-bbb'
    return body


# ── Tests ───────────────────────────────────────────────────────────

class TestMinimalPayload:

    def test_valid_minimal_submission(self, client, app, db):
        body = _make_minimal_body()
        patches = _patch_all_upstreams()
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-111',
                json=body,
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 202
        data = resp.get_json()
        assert data['status'] == 'accepted'
        assert data['observations_stored'] == 2
        assert data['action'] == 'created'

    def test_backward_compat_body(self, client, app, db):
        body = _make_backward_compat_body()
        patches = _patch_all_upstreams()
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-111',
                json=body,
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 202

    def test_idempotent_submission(self, client, app, db):
        body = _make_minimal_body()
        body['report_payload']['observations'][0]['value'] = 999  # unique
        patches = _patch_all_upstreams()
        with patches[0], patches[1], patches[2], patches[3]:
            resp1 = client.post(
                '/api/v1/provider/report/sr-idem-new',
                json=body,
                headers={'X-Provider-Token': 'valid-token'},
            )
            resp2 = client.post(
                '/api/v1/provider/report/sr-idem-new',
                json=body,
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp1.status_code == 202
        assert resp2.status_code == 202
        assert resp2.get_json()['action'] == 'duplicate_ignored'


class TestOrgCrossCheck:

    def test_wrong_org_in_body(self, client, app, db):
        body = _make_minimal_body()
        body['organisation_guid'] = 'wrong-org'
        patches = _patch_all_upstreams()
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-111',
                json=body,
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 403
        assert resp.get_json()['code'] == 'ORG_MISMATCH'


class TestContractCrossCheck:

    def test_wrong_contract_in_body(self, client, app, db):
        body = _make_minimal_body()
        body['contract_guid'] = 'wrong-contract'
        patches = _patch_all_upstreams()
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-111',
                json=body,
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 403
        assert resp.get_json()['code'] == 'CONTRACT_MISMATCH'


class TestGrantValidation:

    def test_forged_grant_token(self, client, db):
        body = _make_minimal_body()
        patches = _patch_all_upstreams(grant_mock=_mock_grant_invalid)
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-111',
                json=body,
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 403
        assert resp.get_json()['code'] == 'GRANT_TOKEN_INVALID'

    def test_missing_grant_token(self, client, db):
        body = _make_minimal_body()
        del body['grant_token']
        patches = _patch_all_upstreams()
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-111',
                json=body,
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 400
        assert resp.get_json()['code'] == 'COMPOSITE_KEY_INCOMPLETE'


class TestPatientCrossCheck:

    def test_wrong_patient(self, client, app, db):
        body = _make_minimal_body()
        body['patient_guid'] = 'wrong-patient'
        patches = _patch_all_upstreams()
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-111',
                json=body,
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 403
        assert resp.get_json()['code'] == 'PATIENT_MISMATCH'


class TestContractScope:

    def test_concept_not_in_scope(self, client, app, db):
        body = _make_minimal_body()
        body['report_payload']['observations'][1]['concept_guid'] = 'concept-999'
        patches = _patch_all_upstreams()
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-scope-test',
                json=body,
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 403
        assert resp.get_json()['code'] == 'SCOPE_VIOLATION'

    def test_missing_obligatory_on_completed(self, client, app, db):
        body = _make_minimal_body()
        # Only submit concept-002 (optional), missing concept-001 (obligatory)
        body['report_payload']['observations'] = [
            {
                'transaction_guid': 'tx-002',
                'concept_guid': 'concept-002',
                'value': 'normal',
                'response_type': 'categorical',
            },
        ]
        patches = _patch_all_upstreams()
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-oblig-test',
                json=body,
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 403
        assert resp.get_json()['code'] == 'SCOPE_VIOLATION'

    def test_missing_obligatory_ok_on_in_progress(self, client, app, db):
        body = _make_minimal_body()
        body['status'] = 'in-progress'
        body['report_payload']['observations'] = [
            {
                'transaction_guid': 'tx-002',
                'concept_guid': 'concept-002',
                'value': 'normal',
                'response_type': 'categorical',
            },
        ]
        patches = _patch_all_upstreams()
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-progress-test',
                json=body,
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 202

    def test_revoked_contract_rejected(self, client, app, db):
        body = _make_minimal_body()
        patches = _patch_all_upstreams(scope_mock=_mock_scope_revoked)
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-revoked-test',
                json=body,
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 403
        assert resp.get_json()['code'] == 'CONTRACT_INACTIVE'

    def test_no_scope_allows_all(self, client, app, db):
        body = _make_minimal_body()
        body['report_payload']['observations'][1]['concept_guid'] = 'anything'
        patches = _patch_all_upstreams(scope_mock=_mock_scope_no_scope)
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-noscope-test',
                json=body,
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 202


class TestObservationEnrichment:

    def test_concept_guid_enriched_from_sr_context(self, client, app, db):
        """Observation without concept_guid gets it from transaction map."""
        body = _make_minimal_body()
        # Remove concept_guid — should be enriched from SR context
        del body['report_payload']['observations'][0]['concept_guid']
        del body['report_payload']['observations'][1]['concept_guid']

        # Use scope_no_scope so the enriched concepts aren't rejected
        patches = _patch_all_upstreams(scope_mock=_mock_scope_no_scope)
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-enrich-test',
                json=body,
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 202


class TestAuthAndEdgeCases:

    def test_missing_token(self, client, db):
        resp = client.post(
            '/api/v1/provider/report/sr-111',
            json={'anything': True},
        )
        assert resp.status_code == 401

    def test_invalid_token(self, client, db):
        def mock_401(*a, **k):
            class R:
                status_code = 401
                def json(self): return {'message': 'bad token'}
            return R()
        with patch('app.services.pat_validation.http_requests.post', mock_401):
            resp = client.post(
                '/api/v1/provider/report/sr-111',
                json={'anything': True},
                headers={'X-Provider-Token': 'bad-token'},
            )
        assert resp.status_code == 401

    def test_scope_mismatch_read_only(self, client, db):
        with patch('app.services.pat_validation.http_requests.post', _mock_pat_read_only):
            resp = client.post(
                '/api/v1/provider/report/sr-111',
                json={'anything': True},
                headers={'X-Provider-Token': 'read-only-token'},
            )
        assert resp.status_code == 403

    def test_no_json_body(self, client, db):
        patches = _patch_all_upstreams()
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-111',
                headers={'X-Provider-Token': 'valid-token'},
                content_type='application/json',
            )
        assert resp.status_code == 400

    def test_missing_report_payload(self, client, app, db):
        body = {
            'patient_guid': 'patient-222',
            'grant_token': 'valid-grant-token',
        }
        patches = _patch_all_upstreams()
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-nopayload',
                json=body,
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 400

    def test_freeform_payload(self, client, app, db):
        body = {
            'patient_guid': 'patient-222',
            'grant_token': 'valid-grant-token',
            'report_payload': {'custom_data': 'freeform', 'score': 42},
        }
        patches = _patch_all_upstreams(scope_mock=_mock_scope_no_scope)
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-free-new',
                json=body,
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 202
        assert resp.get_json()['observations_stored'] == 1


class TestObservationValidation:

    def test_invalid_response_type(self, client, app, db):
        body = _make_minimal_body()
        body['report_payload']['observations'] = [{
            'transaction_guid': 'tx',
            'concept_guid': 'concept-001',
            'value': 'x',
            'response_type': 'invalid_type',
        }]
        patches = _patch_all_upstreams()
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-vt-new',
                json=body,
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 422

    def test_empty_observations_array(self, client, app, db):
        body = _make_minimal_body()
        body['report_payload']['observations'] = []
        patches = _patch_all_upstreams()
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-empty-new',
                json=body,
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 422


class TestReceiptAck:

    def test_ack_receipt(self, client, db):
        with patch('app.services.pat_validation.http_requests.post', _mock_pat_ok):
            resp = client.post(
                '/api/v1/provider/receipt/sr-ack-test-new/ack',
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 200
        assert resp.get_json()['status'] == 'acknowledged'

    def test_ack_missing_token(self, client, db):
        resp = client.post('/api/v1/provider/receipt/sr-ack/ack')
        assert resp.status_code == 401


class TestFeedAuth:

    def test_feed_requires_auth(self, client, db):
        resp = client.get('/api/v1/provider/feed')
        assert resp.status_code == 401

    def test_download_requires_auth(self, client, db):
        resp = client.get('/api/v1/provider/download/sr-111')
        assert resp.status_code == 401
