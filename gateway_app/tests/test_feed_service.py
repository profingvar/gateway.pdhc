"""Phase 5 tests — Provider feed and bundle download (proxy to request.pdhc)."""
import pytest
from unittest.mock import patch, MagicMock
from app.models import AuditLog


class FakeResponse:
    def __init__(self, status_code, json_data, text=''):
        self.status_code = status_code
        self._json = json_data
        self.text = text or str(json_data)

    def json(self):
        return self._json


MOCK_FEED = {
    'requests': [
        {
            'request_guid': 'sr-001',
            'provider_guid': 'org-111',
            'status': 'active',
            'created_at': '2026-03-26T07:00:00Z',
        },
        {
            'request_guid': 'sr-002',
            'provider_guid': 'org-111',
            'status': 'submitted',
            'created_at': '2026-03-26T08:00:00Z',
        },
    ],
    'cursor': '2',
    'has_more': False,
}

MOCK_BUNDLE = {
    'request_guid': 'sr-001',
    'fhir_bundle': {
        'resourceType': 'Bundle',
        'type': 'collection',
        'entry': [],
    },
    'grant_token': 'hmac-grant-abc',
    'patient_guid': 'patient-222',
    'contract_guid': 'contract-333',
    'provider_org_guid': 'org-111',
    'expires_at': '2026-03-29T07:00:00Z',
}


def _mock_pat_ok(*args, **kwargs):
    return FakeResponse(200, {
        'provider_org_guid': 'org-111',
        'contract_guid': 'contract-222',
        'scopes': 'read,write',
        'delivery_mode': 'poll',
    })


def _mock_feed_ok(*args, **kwargs):
    return FakeResponse(200, MOCK_FEED)


def _mock_bundle_ok(*args, **kwargs):
    return FakeResponse(200, MOCK_BUNDLE)


def _mock_upstream_404(*args, **kwargs):
    return FakeResponse(404, {'code': 'not_found'}, text='Not found')


def _mock_upstream_unreachable(*args, **kwargs):
    import requests
    raise requests.ConnectionError('Connection refused')


class TestProviderFeed:

    def _auth_headers(self):
        return {'X-Provider-Token': 'valid-token'}

    def test_feed_returns_data(self, client, db):
        """Feed endpoint proxies data from request.pdhc."""
        with patch('app.services.pat_validation.http_requests.post', _mock_pat_ok), \
             patch('app.services.feed_service.http_requests.get', _mock_feed_ok):
            resp = client.get('/api/v1/provider/feed', headers=self._auth_headers())
            assert resp.status_code == 200
            data = resp.get_json()
            assert 'requests' in data
            assert len(data['requests']) == 2
            assert data['requests'][0]['request_guid'] == 'sr-001'

    def test_feed_forwards_query_params(self, client, db):
        """Query params (since, limit, cursor) forwarded to upstream."""
        def _check_params(*args, **kwargs):
            params = kwargs.get('params', {})
            assert params.get('since') == '2026-03-26T00:00:00Z'
            assert params.get('limit') == '10'
            return FakeResponse(200, MOCK_FEED)

        with patch('app.services.pat_validation.http_requests.post', _mock_pat_ok), \
             patch('app.services.feed_service.http_requests.get', _check_params):
            resp = client.get(
                '/api/v1/provider/feed?since=2026-03-26T00:00:00Z&limit=10',
                headers=self._auth_headers(),
            )
            assert resp.status_code == 200

    def test_feed_requires_read_scope(self, client, db):
        """Feed requires read scope — write-only token should be rejected."""
        def _mock_pat_write_only(*args, **kwargs):
            return FakeResponse(200, {
                'provider_org_guid': 'org-111',
                'contract_guid': 'contract-222',
                'scopes': 'write',
                'delivery_mode': 'poll',
            })

        with patch('app.services.pat_validation.http_requests.post', _mock_pat_write_only):
            resp = client.get('/api/v1/provider/feed', headers=self._auth_headers())
            assert resp.status_code == 403

    def test_feed_upstream_unreachable(self, client, db):
        """Feed returns 502 when request.pdhc is down."""
        with patch('app.services.pat_validation.http_requests.post', _mock_pat_ok), \
             patch('app.services.feed_service.http_requests.get', _mock_upstream_unreachable):
            resp = client.get('/api/v1/provider/feed', headers=self._auth_headers())
            assert resp.status_code == 502
            data = resp.get_json()
            assert data['code'] == 'UPSTREAM_UNREACHABLE'

    def test_feed_audited(self, app, client, db):
        """Feed access is logged in audit trail."""
        with patch('app.services.pat_validation.http_requests.post', _mock_pat_ok), \
             patch('app.services.feed_service.http_requests.get', _mock_feed_ok):
            client.get('/api/v1/provider/feed', headers=self._auth_headers())

        with app.app_context():
            audits = AuditLog.query.filter_by(event_type='feed.accessed').all()
            assert len(audits) >= 1


class TestBundleDownload:

    def _auth_headers(self):
        return {'X-Provider-Token': 'valid-token'}

    def test_download_returns_bundle(self, client, db):
        """Download endpoint proxies bundle from request.pdhc."""
        with patch('app.services.pat_validation.http_requests.post', _mock_pat_ok), \
             patch('app.services.feed_service.http_requests.get', _mock_bundle_ok):
            resp = client.get('/api/v1/provider/download/sr-001',
                              headers=self._auth_headers())
            assert resp.status_code == 200
            data = resp.get_json()
            assert data['request_guid'] == 'sr-001'
            assert 'fhir_bundle' in data
            assert 'grant_token' in data

    def test_download_not_found(self, client, db):
        """Download returns upstream 404."""
        with patch('app.services.pat_validation.http_requests.post', _mock_pat_ok), \
             patch('app.services.feed_service.http_requests.get', _mock_upstream_404):
            resp = client.get('/api/v1/provider/download/nonexistent',
                              headers=self._auth_headers())
            assert resp.status_code == 404

    def test_download_upstream_unreachable(self, client, db):
        """Download returns 502 when request.pdhc is down."""
        with patch('app.services.pat_validation.http_requests.post', _mock_pat_ok), \
             patch('app.services.feed_service.http_requests.get', _mock_upstream_unreachable):
            resp = client.get('/api/v1/provider/download/sr-001',
                              headers=self._auth_headers())
            assert resp.status_code == 502

    def test_download_audited(self, app, client, db):
        """Bundle download is logged in audit trail."""
        with patch('app.services.pat_validation.http_requests.post', _mock_pat_ok), \
             patch('app.services.feed_service.http_requests.get', _mock_bundle_ok):
            client.get('/api/v1/provider/download/sr-001',
                       headers=self._auth_headers())

        with app.app_context():
            audits = AuditLog.query.filter_by(event_type='bundle.downloaded').all()
            assert len(audits) >= 1

    def test_download_requires_auth(self, client, db):
        """Download requires PAT."""
        resp = client.get('/api/v1/provider/download/sr-001')
        assert resp.status_code == 401
