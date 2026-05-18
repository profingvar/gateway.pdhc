"""Tests for ticket #90 — gateway flags reports arriving after the SR's
period_end as late, but still accepts them. Archived SRs go through the
normal ingestion path (the gateway doesn't gate on status).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models import InboundObservation


# ── Mock helpers (reused shape from test_report_submission) ──────────

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


def _make_sr_context_mock(status='active', period_end=None):
    def _mock(*args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            'service_request_guid': 'sr-late',
            'status': status,
            'patient_guid': 'patient-222',
            'contract_guid': 'contract-bbb',
            'requester_org_guid': 'org-requester',
            'period_start': None,
            'period_end': period_end,
            'transactions': [
                {
                    'transaction_guid': 'tx-001',
                    'concept_guid': 'concept-001',
                    'concept_name': 'Spirometri',
                    'unit': 'percent',
                    'unit_display': '% predicted',
                    'range_min': None,
                    'range_max': None,
                    'requirement_type': 'optional',
                    'response_type': 'numeric',
                },
            ],
            'goals': [],
        }
        return resp
    return _mock


def _patch_upstreams(sr_context_mock):
    def _route_post(url, **kwargs):
        if 'validate-token' in url:
            return _mock_pat_ok(url, **kwargs)
        elif 'grant/validate' in url:
            return _mock_grant_valid(url, **kwargs)
        raise ValueError(f'Unexpected POST to {url}')

    def _route_get(url, **kwargs):
        if '/context' in url:
            return sr_context_mock(url, **kwargs)
        elif '/scope' in url:
            return _mock_scope_no_scope(url, **kwargs)
        raise ValueError(f'Unexpected GET to {url}')

    return (
        patch('app.services.pat_validation.http_requests.post', side_effect=_route_post),
        patch('app.services.grant_validation.http_requests.post', side_effect=_route_post),
        patch('app.services.sr_context.http_requests.get', side_effect=_route_get),
        patch('app.services.contract_scope.http_requests.get', side_effect=_route_get),
    )


def _make_body(sr_guid='sr-late'):
    return {
        'patient_guid': 'patient-222',
        'grant_token': 'valid-grant-token',
        'status': 'completed',
        'report_payload': {
            'observations': [
                {
                    'transaction_guid': 'tx-001',
                    'value': 72,
                    'response_type': 'numeric',
                },
            ],
        },
    }


# ── Tests ────────────────────────────────────────────────────────────

class TestLateFlag:

    def test_observation_after_period_end_is_flagged_late(self, client, app, db):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        sr_mock = _make_sr_context_mock(status='archived', period_end=past)
        patches = _patch_upstreams(sr_mock)
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-late-1',
                json=_make_body(),
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 202
        data = resp.get_json()
        assert data['is_late'] is True

        with app.app_context():
            rec = InboundObservation.query.filter_by(
                service_request_guid='sr-late-1').first()
            assert rec is not None
            assert rec.is_late is True

    def test_observation_before_period_end_is_not_late(self, client, app, db):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        sr_mock = _make_sr_context_mock(status='active', period_end=future)
        patches = _patch_upstreams(sr_mock)
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-late-2',
                json=_make_body(),
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 202
        assert resp.get_json()['is_late'] is False

        with app.app_context():
            rec = InboundObservation.query.filter_by(
                service_request_guid='sr-late-2').first()
            assert rec is not None
            assert rec.is_late is False

    def test_open_ended_sr_never_marks_late(self, client, app, db):
        sr_mock = _make_sr_context_mock(status='active', period_end=None)
        patches = _patch_upstreams(sr_mock)
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-late-3',
                json=_make_body(),
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 202
        assert resp.get_json()['is_late'] is False

    def test_archived_sr_still_accepts_report(self, client, app, db):
        """Archived SRs are not gated — reports still flow through,
        just flagged late."""
        past = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        sr_mock = _make_sr_context_mock(status='archived', period_end=past)
        patches = _patch_upstreams(sr_mock)
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.post(
                '/api/v1/provider/report/sr-late-4',
                json=_make_body(),
                headers={'X-Provider-Token': 'valid-token'},
            )
        assert resp.status_code == 202
        data = resp.get_json()
        assert data['status'] == 'accepted'
        assert data['is_late'] is True
