"""Tests — SR context fetch via request.pdhc internal API."""
import pytest
from unittest.mock import patch, MagicMock
from app.services.sr_context import SRContextService


def _mock_context_response(status_code=200, data=None):
    resp = MagicMock()
    resp.status_code = status_code
    if status_code == 200:
        resp.json.return_value = data or {
            'service_request_guid': 'sr-aaa',
            'status': 'active',
            'patient_guid': 'patient-bbb',
            'contract_guid': 'contract-ccc',
            'requester_org_guid': 'org-ddd',
            'transactions': [
                {
                    'transaction_guid': 'tx-001',
                    'concept_guid': 'concept-001',
                    'concept_name': 'Spirometri',
                    'unit': 'percent',
                    'unit_display': '% predicted',
                    'expected_value': '80',
                    'range_min': 70.0,
                    'range_max': 120.0,
                    'requirement_type': 'required',
                },
            ],
            'goals': [
                {
                    'description': 'Uppna astmakontroll',
                    'concept_guid': 'concept-001',
                    'priority': 'high',
                    'target_value': 70.0,
                    'target_comparator': '>=',
                },
            ],
        }
    return resp


class TestSRContext:

    def test_fetch_returns_context(self, app):
        with app.app_context():
            with patch('app.services.sr_context.http_requests.get',
                       return_value=_mock_context_response()):
                result = SRContextService.fetch('sr-aaa')
        assert result.found is True
        assert result.patient_guid == 'patient-bbb'
        assert result.contract_guid == 'contract-ccc'
        assert len(result.transactions) == 1
        assert result.transactions[0]['concept_name'] == 'Spirometri'

    def test_transaction_map(self, app):
        with app.app_context():
            with patch('app.services.sr_context.http_requests.get',
                       return_value=_mock_context_response()):
                result = SRContextService.fetch('sr-aaa')
        txn_map = result.transaction_map()
        assert 'tx-001' in txn_map
        assert txn_map['tx-001']['concept_guid'] == 'concept-001'

    def test_not_found(self, app):
        resp = MagicMock()
        resp.status_code = 404
        with app.app_context():
            with patch('app.services.sr_context.http_requests.get',
                       return_value=resp):
                result = SRContextService.fetch('nonexistent')
        assert result.found is False

    def test_upstream_unreachable(self, app):
        import requests as http_requests
        with app.app_context():
            with patch('app.services.sr_context.http_requests.get',
                       side_effect=http_requests.ConnectionError('refused')):
                result = SRContextService.fetch('sr-unreachable')
        assert result.found is False

    def test_missing_guid(self, app):
        with app.app_context():
            result = SRContextService.fetch(None)
        assert result.found is False

    def test_period_end_parsed(self, app):
        from datetime import datetime, timezone
        data = {
            'service_request_guid': 'sr-pe',
            'status': 'archived',
            'patient_guid': 'p',
            'contract_guid': 'c',
            'period_end': '2026-04-01T12:00:00',
            'transactions': [],
            'goals': [],
        }
        with app.app_context():
            with patch('app.services.sr_context.http_requests.get',
                       return_value=_mock_context_response(data=data)):
                result = SRContextService.fetch('sr-pe')
        pe = result.period_end
        assert pe is not None
        assert pe.tzinfo is not None
        assert pe == datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_period_end_missing_returns_none(self, app):
        data = {
            'service_request_guid': 'sr-no-pe',
            'status': 'active',
            'patient_guid': 'p',
            'contract_guid': 'c',
            'transactions': [],
            'goals': [],
        }
        with app.app_context():
            with patch('app.services.sr_context.http_requests.get',
                       return_value=_mock_context_response(data=data)):
                result = SRContextService.fetch('sr-no-pe')
        assert result.period_end is None

    def test_empty_transactions(self, app):
        data = {
            'service_request_guid': 'sr-empty',
            'status': 'draft',
            'patient_guid': 'patient-x',
            'contract_guid': 'contract-x',
            'transactions': [],
            'goals': [],
        }
        with app.app_context():
            with patch('app.services.sr_context.http_requests.get',
                       return_value=_mock_context_response(data=data)):
                result = SRContextService.fetch('sr-empty')
        assert result.found is True
        assert result.transactions == []
        assert result.transaction_map() == {}
