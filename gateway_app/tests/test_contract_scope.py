"""Tests — Contract scope validation via contract.pdhc."""
import pytest
from unittest.mock import patch, MagicMock
from app.services.contract_scope import ContractScopeService, ContractScopeResult


def _mock_scope_response(status_code=200, status='active', scope_defined=True,
                         request_scope=None, return_scope=None):
    resp = MagicMock()
    resp.status_code = status_code
    if status_code == 200:
        resp.json.return_value = {
            'contract_guid': 'contract-aaa',
            'status': status,
            'scope_defined': scope_defined,
            'request_scope': request_scope or [],
            'return_scope': return_scope or {},
        }
    return resp


class TestFetchScope:

    def test_active_contract_with_scope(self, app):
        return_scope = {
            'obligatory_return': ['concept-1', 'concept-2'],
            'optional_return': ['concept-3'],
        }
        with app.app_context():
            with patch('app.services.contract_scope.http_requests.get',
                       return_value=_mock_scope_response(return_scope=return_scope)):
                result = ContractScopeService.fetch_scope('contract-aaa')
        assert result.valid is True
        assert result.scope_defined is True
        assert result.obligatory_guids == {'concept-1', 'concept-2'}
        assert result.optional_guids == {'concept-3'}
        assert result.all_permitted_guids == {'concept-1', 'concept-2', 'concept-3'}

    def test_no_scope_defined(self, app):
        with app.app_context():
            with patch('app.services.contract_scope.http_requests.get',
                       return_value=_mock_scope_response(scope_defined=False)):
                result = ContractScopeService.fetch_scope('contract-noscope')
        assert result.valid is True
        assert result.scope_defined is False

    def test_revoked_contract(self, app):
        with app.app_context():
            with patch('app.services.contract_scope.http_requests.get',
                       return_value=_mock_scope_response(status='revoked')):
                result = ContractScopeService.fetch_scope('contract-revoked')
        assert result.valid is False
        assert result.error_code == 'CONTRACT_INACTIVE'

    def test_terminated_contract(self, app):
        with app.app_context():
            with patch('app.services.contract_scope.http_requests.get',
                       return_value=_mock_scope_response(status='terminated')):
                result = ContractScopeService.fetch_scope('contract-terminated')
        assert result.valid is False
        assert result.error_code == 'CONTRACT_INACTIVE'

    def test_not_found(self, app):
        resp = MagicMock()
        resp.status_code = 404
        with app.app_context():
            with patch('app.services.contract_scope.http_requests.get',
                       return_value=resp):
                result = ContractScopeService.fetch_scope('contract-notfound')
        assert result.valid is False
        assert result.error_code == 'SERVICE_UNAVAILABLE'

    def test_auth_rejected(self, app):
        resp = MagicMock()
        resp.status_code = 401
        with app.app_context():
            with patch('app.services.contract_scope.http_requests.get',
                       return_value=resp):
                result = ContractScopeService.fetch_scope('contract-authfail')
        assert result.valid is False

    def test_missing_contract_guid(self, app):
        with app.app_context():
            result = ContractScopeService.fetch_scope(None)
        assert result.valid is False
        assert result.error_code == 'MISSING_CONTRACT'


class TestValidateObservations:

    def test_concepts_in_scope(self):
        scope = ContractScopeResult(
            valid=True, scope_defined=True,
            return_scope={
                'obligatory_return': ['c-1', 'c-2'],
                'optional_return': ['c-3'],
            },
        )
        obs = [
            {'concept_guid': 'c-1', 'value': 80},
            {'concept_guid': 'c-2', 'value': 90},
        ]
        valid, errors = ContractScopeService.validate_observations(scope, obs)
        assert valid is True

    def test_concept_not_in_scope(self):
        scope = ContractScopeResult(
            valid=True, scope_defined=True,
            return_scope={'obligatory_return': ['c-1'], 'optional_return': []},
        )
        obs = [
            {'concept_guid': 'c-1', 'value': 80},
            {'concept_guid': 'c-999', 'value': 90},
        ]
        valid, errors = ContractScopeService.validate_observations(scope, obs)
        assert valid is False
        assert errors[0]['concept_guid'] == 'c-999'

    def test_missing_obligatory_on_completed(self):
        scope = ContractScopeResult(
            valid=True, scope_defined=True,
            return_scope={
                'obligatory_return': ['c-1', 'c-2', 'c-3'],
                'optional_return': [],
            },
        )
        obs = [
            {'concept_guid': 'c-1', 'value': 80},
            # c-2 and c-3 missing
        ]
        valid, errors = ContractScopeService.validate_observations(
            scope, obs, status='completed',
        )
        assert valid is False
        assert any('missing_concept_guids' in e for e in errors)

    def test_obligatory_not_enforced_on_in_progress(self):
        scope = ContractScopeResult(
            valid=True, scope_defined=True,
            return_scope={
                'obligatory_return': ['c-1', 'c-2'],
                'optional_return': [],
            },
        )
        obs = [{'concept_guid': 'c-1', 'value': 80}]
        valid, errors = ContractScopeService.validate_observations(
            scope, obs, status='in-progress',
        )
        assert valid is True

    def test_no_scope_defined_allows_all(self):
        scope = ContractScopeResult(valid=True, scope_defined=False)
        obs = [{'concept_guid': 'any-concept', 'value': 42}]
        valid, errors = ContractScopeService.validate_observations(scope, obs)
        assert valid is True
