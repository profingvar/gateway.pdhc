"""Phase 8 — Full endpoint test script (Rules 9, 20).

Tests all API endpoints according to the capability statement.
Covers both authenticated and unauthenticated access patterns.
"""
import pytest
from unittest.mock import patch, MagicMock


class FakeResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data
    def json(self):
        return self._json


def _mock_pat_ok(*args, **kwargs):
    return FakeResponse(200, {
        'provider_org_guid': 'org-test',
        'contract_guid': 'con-test',
        'scopes': 'read,write',
        'delivery_mode': 'poll',
    })


def _mock_pat_read_only(*args, **kwargs):
    return FakeResponse(200, {
        'provider_org_guid': 'org-test',
        'contract_guid': 'con-test',
        'scopes': 'read',
        'delivery_mode': 'poll',
    })


AUTH = {'X-Provider-Token': 'valid'}


class TestHealthEndpoints:
    """Unauthenticated endpoints."""

    def test_health(self, client, db):
        resp = client.get('/api/v1/health')
        assert resp.status_code == 200
        assert resp.get_json()['status'] == 'ok'

    def test_dashboard(self, client, db):
        resp = client.get('/')
        assert resp.status_code == 200

    def test_observations_page(self, client, db):
        resp = client.get('/observations')
        assert resp.status_code == 200


class TestProviderReportEndpoint:
    """POST /api/v1/provider/report/<sr_guid>"""

    def test_requires_auth(self, client, db):
        resp = client.post('/api/v1/provider/report/sr-001')
        assert resp.status_code == 401

    def test_requires_write_scope(self, client, db):
        with patch('app.services.pat_validation.http_requests.post', _mock_pat_read_only):
            resp = client.post('/api/v1/provider/report/sr-001',
                               json={}, headers=AUTH)
        assert resp.status_code == 403

    def test_requires_json_body(self, client, db):
        with patch('app.services.pat_validation.http_requests.post', _mock_pat_ok):
            resp = client.post('/api/v1/provider/report/sr-001',
                               data='not json',
                               content_type='application/json',
                               headers=AUTH)
        assert resp.status_code == 400

    def test_valid_submission(self, app, client, db):
        body = {
            'patient_guid': 'p-1',
            'grant_token': 'valid-grant',
            'report_payload': {
                'observations': [{
                    'transaction_guid': 'tx-1',
                    'concept_guid': 'c-1',
                    'value': 72,
                    'response_type': 'numeric',
                }],
            },
        }

        def _route_post(url, **kw):
            if 'validate-token' in url:
                return _mock_pat_ok(url, **kw)
            # grant validation
            return FakeResponse(200, {
                'valid': True, 'contract_guid': 'con-test',
                'grant_type': 'standard', 'uses_remaining': None,
            })

        def _route_get(url, **kw):
            if '/context' in url:
                return FakeResponse(200, {
                    'service_request_guid': 'sr-full',
                    'status': 'active',
                    'patient_guid': 'p-1',
                    'contract_guid': 'con-test',
                    'transactions': [{
                        'transaction_guid': 'tx-1',
                        'concept_guid': 'c-1',
                        'concept_name': 'Test',
                        'unit': '', 'unit_display': '',
                        'range_min': None, 'range_max': None,
                        'requirement_type': 'required',
                    }],
                    'goals': [],
                })
            # scope
            return FakeResponse(200, {
                'contract_guid': 'con-test', 'status': 'active',
                'scope_defined': False, 'request_scope': [], 'return_scope': {},
            })

        with patch('app.services.pat_validation.http_requests.post', side_effect=_route_post), \
             patch('app.services.grant_validation.http_requests.post', side_effect=_route_post), \
             patch('app.services.sr_context.http_requests.get', side_effect=_route_get), \
             patch('app.services.contract_scope.http_requests.get', side_effect=_route_get):
            resp = client.post('/api/v1/provider/report/sr-full',
                               json=body, headers=AUTH)
        assert resp.status_code == 202
        data = resp.get_json()
        assert data['status'] == 'accepted'
        assert 'receipt_guid' in data


class TestProviderFeedEndpoint:
    """GET /api/v1/provider/feed"""

    def test_requires_auth(self, client, db):
        resp = client.get('/api/v1/provider/feed')
        assert resp.status_code == 401

    def test_requires_read_scope(self, client, db):
        def _mock_write_only(*args, **kwargs):
            return FakeResponse(200, {
                'provider_org_guid': 'org-test',
                'contract_guid': 'con-test',
                'scopes': 'write',
                'delivery_mode': 'poll',
            })
        with patch('app.services.pat_validation.http_requests.post', _mock_write_only):
            resp = client.get('/api/v1/provider/feed', headers=AUTH)
        assert resp.status_code == 403

    def test_proxies_upstream(self, client, db):
        """With valid PAT but no upstream, returns 502."""
        with patch('app.services.pat_validation.http_requests.post', _mock_pat_ok):
            resp = client.get('/api/v1/provider/feed', headers=AUTH)
        assert resp.status_code == 502


class TestProviderDownloadEndpoint:
    """GET /api/v1/provider/download/<sr_guid>"""

    def test_requires_auth(self, client, db):
        resp = client.get('/api/v1/provider/download/sr-001')
        assert resp.status_code == 401

    def test_proxies_upstream(self, client, db):
        with patch('app.services.pat_validation.http_requests.post', _mock_pat_ok):
            resp = client.get('/api/v1/provider/download/sr-001', headers=AUTH)
        assert resp.status_code == 502


class TestReceiptEndpoint:
    """POST /api/v1/provider/receipt/<token>/ack"""

    def test_requires_auth(self, client, db):
        resp = client.post('/api/v1/provider/receipt/tok-1/ack')
        assert resp.status_code == 401

    def test_ack_with_auth(self, client, db):
        with patch('app.services.pat_validation.http_requests.post', _mock_pat_ok):
            resp = client.post('/api/v1/provider/receipt/tok-1/ack', headers=AUTH)
        assert resp.status_code == 200
        assert resp.get_json()['status'] == 'acknowledged'


class TestMethodNotAllowed:
    """Verify correct 405 for wrong HTTP methods."""

    def test_get_on_report(self, client, db):
        resp = client.get('/api/v1/provider/report/sr-001')
        assert resp.status_code == 405

    def test_post_on_feed(self, client, db):
        resp = client.post('/api/v1/provider/feed')
        assert resp.status_code == 405

    def test_delete_on_health(self, client, db):
        resp = client.delete('/api/v1/health')
        assert resp.status_code == 405
