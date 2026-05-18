"""Phase 7 tests — Error handling, audit trail completeness, security hardening."""
import pytest
from unittest.mock import patch
from app.errors import APIError
from app.models import AuditLog
from config import Config, TestConfig


# ── 7.a Standardized error responses ───────────────────────────────

class TestErrorResponses:
    """All API error responses must follow { code, message, [details] } format."""

    def test_404_api_json(self, client, db):
        resp = client.get('/api/v1/nonexistent')
        assert resp.status_code == 404
        data = resp.get_json()
        assert 'code' in data
        assert 'message' in data
        assert data['code'] == 'NOT_FOUND'

    def test_404_web_html(self, client, db):
        resp = client.get('/nonexistent')
        assert resp.status_code == 404
        assert resp.content_type.startswith('text/html')

    def test_401_json_format(self, client, db):
        resp = client.post('/api/v1/provider/report/test-guid')
        assert resp.status_code == 401
        data = resp.get_json()
        assert data['code'] == 'UNAUTHORIZED'
        assert 'message' in data

    def test_405_json_format(self, client, db):
        resp = client.delete('/api/v1/provider/feed')
        assert resp.status_code == 405
        data = resp.get_json()
        assert data['code'] == 'METHOD_NOT_ALLOWED'

    def test_api_error_with_details(self, app, db):
        """APIError supports details array for validation errors."""
        err = APIError('Validation failed', code='VALIDATION_ERROR',
                       status_code=422, details=[{'field': 'value', 'error': 'required'}])
        d = err.to_dict()
        assert d['code'] == 'VALIDATION_ERROR'
        assert len(d['details']) == 1

    def test_api_error_without_details(self, app, db):
        """APIError omits details when empty."""
        err = APIError('Not found', code='NOT_FOUND', status_code=404)
        d = err.to_dict()
        assert 'details' not in d


# ── 7.b Audit trail completeness ──────────────────────────────────

class TestAuditTrail:
    """Verify audit events are logged for key operations."""

    def _mock_pat_ok(*args, **kwargs):
        class R:
            status_code = 200
            def json(self): return {
                'provider_org_guid': 'org-111',
                'contract_guid': 'con-222',
                'scopes': 'read,write',
                'delivery_mode': 'poll',
            }
        return R()

    def test_pat_validated_event(self, app, client, db):
        """Successful PAT validation creates pat.validated audit entry."""
        with app.app_context():
            with patch('app.services.pat_validation.http_requests.post', self._mock_pat_ok):
                client.get('/api/v1/provider/feed',
                           headers={'X-Provider-Token': 'test'})
            audits = AuditLog.query.filter_by(event_type='pat.validated').all()
            assert len(audits) >= 1
            assert audits[0].actor_guid == 'org-111'

    def test_pat_rejected_event(self, app, client, db):
        """Missing PAT creates pat.rejected audit entry."""
        with app.app_context():
            client.post('/api/v1/provider/report/test')
            audits = AuditLog.query.filter_by(event_type='pat.rejected').all()
            assert len(audits) >= 1

    def test_audit_includes_ip(self, app, client, db):
        """Audit entries include IP address."""
        with app.app_context():
            client.post('/api/v1/provider/report/test')
            audit = AuditLog.query.filter_by(event_type='pat.rejected').first()
            assert audit.ip_address is not None

    def test_audit_includes_correlation_id(self, app, client, db):
        """Audit entries capture X-Correlation-Id header."""
        with app.app_context():
            client.post('/api/v1/provider/report/test',
                        headers={'X-Correlation-Id': 'corr-test-123'})
            audit = AuditLog.query.filter_by(event_type='pat.rejected').first()
            assert audit.correlation_id == 'corr-test-123'

    def test_audit_includes_endpoint(self, app, client, db):
        """Audit entries capture which endpoint was accessed."""
        with app.app_context():
            client.post('/api/v1/provider/report/sr-audit-test')
            audit = AuditLog.query.filter_by(event_type='pat.rejected').first()
            assert audit.payload_snapshot is not None
            assert 'endpoint' in audit.payload_snapshot


# ── 7.c Security hardening ────────────────────────────────────────

class TestSecurityHardening:

    def test_no_hmac_secret_on_gateway(self):
        """Gateway no longer holds HMAC_SECRET — delegated to request.pdhc."""
        assert not hasattr(Config, 'HMAC_SECRET')
        assert not hasattr(TestConfig, 'HMAC_SECRET')

    def test_grant_validation_delegates(self, app, db):
        """Grant validation delegates to request.pdhc, no local HMAC."""
        import inspect
        from app.services import grant_validation
        source = inspect.getsource(grant_validation)
        # Must call request.pdhc, not compute HMAC locally
        assert 'internal/grant/validate' in source
        assert 'hmac.new' not in source

    def test_no_provider_identity_from_params(self, app, db):
        """Provider identity comes from PAT, never from request params."""
        import inspect
        from app.api.auth import require_provider_token
        source = inspect.getsource(require_provider_token)
        # The decorator sets g.provider_org_guid from result, not from request
        assert 'result.provider_org_guid' in source

    def test_health_endpoint_no_auth(self, client, db):
        """Health endpoint is accessible without authentication."""
        resp = client.get('/api/v1/health')
        assert resp.status_code == 200

    def test_all_provider_endpoints_require_auth(self, client, db):
        """All provider endpoints return 401 without token."""
        endpoints = [
            ('POST', '/api/v1/provider/report/test'),
            ('GET', '/api/v1/provider/feed'),
            ('GET', '/api/v1/provider/download/test'),
            ('POST', '/api/v1/provider/receipt/test/ack'),
        ]
        for method, path in endpoints:
            if method == 'POST':
                resp = client.post(path)
            else:
                resp = client.get(path)
            assert resp.status_code == 401, f'{method} {path} should require auth'
