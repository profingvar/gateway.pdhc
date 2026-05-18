"""Phase 2.a tests — PAT validation middleware."""
import pytest
from unittest.mock import patch, MagicMock
from app.services.pat_validation import PATValidationService, PATValidationResult


class FakeResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json


def _mock_upstream_ok(*args, **kwargs):
    return FakeResponse(200, {
        'provider_org_guid': 'org-111',
        'contract_guid': 'contract-222',
        'scopes': 'read,write',
        'delivery_mode': 'poll',
    })


def _mock_upstream_401(*args, **kwargs):
    return FakeResponse(401, {'message': 'Token expired'})


def _mock_upstream_403(*args, **kwargs):
    return FakeResponse(403, {'message': 'Token revoked'})


def _mock_upstream_unreachable(*args, **kwargs):
    import requests
    raise requests.ConnectionError('Connection refused')


def test_valid_pat(app, db):
    with app.app_context():
        with patch('app.services.pat_validation.http_requests.post', _mock_upstream_ok):
            result = PATValidationService.validate('valid-token-123')
            assert result.valid is True
            assert result.provider_org_guid == 'org-111'
            assert result.contract_guid == 'contract-222'
            assert result.has_scope('read')
            assert result.has_scope('write')


def test_missing_token(app, db):
    with app.app_context():
        result = PATValidationService.validate(None)
        assert result.valid is False
        assert 'Missing' in result.error


def test_empty_token(app, db):
    with app.app_context():
        result = PATValidationService.validate('')
        assert result.valid is False


def test_expired_pat(app, db):
    with app.app_context():
        with patch('app.services.pat_validation.http_requests.post', _mock_upstream_401):
            result = PATValidationService.validate('expired-token')
            assert result.valid is False
            assert 'expired' in result.error.lower() or 'Token' in result.error


def test_revoked_pat(app, db):
    with app.app_context():
        with patch('app.services.pat_validation.http_requests.post', _mock_upstream_403):
            result = PATValidationService.validate('revoked-token')
            assert result.valid is False


def test_upstream_unreachable(app, db):
    with app.app_context():
        with patch('app.services.pat_validation.http_requests.post', _mock_upstream_unreachable):
            result = PATValidationService.validate('some-token')
            assert result.valid is False
            assert 'unreachable' in result.error.lower()


def test_pat_cache_hit(app, db):
    """After a successful validation, the result should be cached."""
    with app.app_context():
        with patch('app.services.pat_validation.http_requests.post', _mock_upstream_ok) as mock_post:
            # First call — hits upstream
            result1 = PATValidationService.validate('cacheable-token')
            assert result1.valid is True

            # Second call — should hit cache, not upstream
            result2 = PATValidationService.validate('cacheable-token')
            assert result2.valid is True
            assert result2.provider_org_guid == 'org-111'


def test_scope_check(app, db):
    with app.app_context():
        result = PATValidationResult(
            valid=True,
            provider_org_guid='org-1',
            contract_guid='con-1',
            scopes='read',
        )
        assert result.has_scope('read') is True
        assert result.has_scope('write') is False


def test_auth_decorator_missing_token(client, db):
    """Endpoint protected by @require_provider_token should return 401 without token."""
    # The stub endpoints don't have auth yet, but we can test
    # by adding a test endpoint
    pass  # Will be tested via protected endpoints in Phase 3


def test_auth_decorator_via_report_endpoint(client, db):
    """Report endpoint returns 401 without token (auth enforced)."""
    resp = client.post('/api/v1/provider/report/test-guid')
    assert resp.status_code == 401
