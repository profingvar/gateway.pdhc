"""Tests — Grant validation via request.pdhc delegation."""
import pytest
from unittest.mock import patch, MagicMock
from app.services.grant_validation import GrantValidationService


def _mock_grant_response(valid=True, contract_guid='contract-ddd',
                         grant_type='standard', uses_remaining=None,
                         status_code=200, error=None):
    """Build a mock HTTP response from request.pdhc grant validation."""
    resp = MagicMock()
    resp.status_code = status_code
    if status_code == 200:
        resp.json.return_value = {
            'valid': valid,
            'contract_guid': contract_guid if valid else None,
            'grant_type': grant_type if valid else None,
            'uses_remaining': uses_remaining,
            'error': error,
        }
    elif status_code == 400:
        resp.json.return_value = {'valid': False, 'error': error or 'Missing fields'}
    elif status_code == 401:
        resp.json.return_value = {'error': 'unauthorized'}
    return resp


class TestGrantDelegation:

    def test_valid_grant(self, app):
        with app.app_context():
            with patch('app.services.grant_validation.http_requests.post',
                       return_value=_mock_grant_response(valid=True)):
                result = GrantValidationService.validate(
                    'sr-aaa', 'patient-bbb', 'org-ccc', 'token-xxx',
                )
        assert result.valid is True
        assert result.contract_guid == 'contract-ddd'
        assert result.grant_type == 'standard'

    def test_invalid_grant_token(self, app):
        with app.app_context():
            with patch('app.services.grant_validation.http_requests.post',
                       return_value=_mock_grant_response(
                           valid=False, error='Invalid grant token')):
                result = GrantValidationService.validate(
                    'sr-aaa', 'patient-bbb', 'org-ccc', 'bad-token',
                )
        assert result.valid is False
        assert result.error_code == 'GRANT_TOKEN_INVALID'

    def test_missing_fields_local(self, app):
        """Missing fields caught locally before calling upstream."""
        with app.app_context():
            result = GrantValidationService.validate(None, 'p', 'o', 'token')
        assert result.valid is False
        assert result.error_code == 'COMPOSITE_KEY_INCOMPLETE'
        assert 'service_request_guid' in result.error

    def test_missing_grant_token(self, app):
        with app.app_context():
            result = GrantValidationService.validate('sr', 'p', 'o', None)
        assert result.valid is False
        assert 'grant_token' in result.error

    def test_upstream_400(self, app):
        with app.app_context():
            with patch('app.services.grant_validation.http_requests.post',
                       return_value=_mock_grant_response(
                           status_code=400, error='Missing sr_guid')):
                result = GrantValidationService.validate(
                    'sr', 'p', 'o', 'token',
                )
        assert result.valid is False
        assert result.error_code == 'COMPOSITE_KEY_INCOMPLETE'

    def test_upstream_401(self, app):
        """Auth rejected — service key misconfigured."""
        with app.app_context():
            with patch('app.services.grant_validation.http_requests.post',
                       return_value=_mock_grant_response(status_code=401)):
                result = GrantValidationService.validate(
                    'sr', 'p', 'o', 'token',
                )
        assert result.valid is False
        assert result.error_code == 'SERVER_ERROR'

    def test_upstream_unreachable(self, app):
        import requests as http_requests
        with app.app_context():
            with patch('app.services.grant_validation.http_requests.post',
                       side_effect=http_requests.ConnectionError('refused')):
                result = GrantValidationService.validate(
                    'sr', 'p', 'o', 'token',
                )
        assert result.valid is False
        assert result.error_code == 'SERVICE_UNAVAILABLE'

    def test_config_missing(self, app):
        """No REQUEST_SERVICE_URL configured."""
        with app.app_context():
            app.config['REQUEST_SERVICE_URL'] = ''
            try:
                result = GrantValidationService.validate(
                    'sr', 'p', 'o', 'token',
                )
                assert result.valid is False
                assert result.error_code == 'SERVER_ERROR'
            finally:
                app.config['REQUEST_SERVICE_URL'] = 'http://mock-request-service/api/v1'

    def test_uses_remaining_passed_through(self, app):
        with app.app_context():
            with patch('app.services.grant_validation.http_requests.post',
                       return_value=_mock_grant_response(
                           valid=True, uses_remaining=5)):
                result = GrantValidationService.validate(
                    'sr', 'p', 'o', 'token',
                )
        assert result.valid is True
        assert result.uses_remaining == 5
