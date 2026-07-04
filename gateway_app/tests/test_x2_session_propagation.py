"""X2 (#408) — universal X-Operator-Session-Id propagation on onward calls.

Covers the reusable helper, the CdrClient header, and the async forwarder
replaying the operator session captured at ingest onto the cdr1 delivery.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from app.services.sso_service import outbound_session_headers
from app.services.cdr_client import CdrClient
from app.services import cdr_forwarder
from app.models import CdrDeliveryLog
from app.extensions import db as _db


# --- reusable helper --------------------------------------------------------

def test_outbound_headers_from_request_header(app):
    with app.test_request_context(
            headers={'X-Operator-Session-Id': 'sid-from-header'}):
        assert outbound_session_headers() == {
            'X-Operator-Session-Id': 'sid-from-header'}


def test_outbound_headers_explicit_wins_and_no_context_needed(app):
    with app.app_context():
        assert outbound_session_headers(session_id='sid-explicit') == {
            'X-Operator-Session-Id': 'sid-explicit'}


def test_outbound_headers_absent_is_empty(app):
    # No header, no access_blob in the session -> forward nothing.
    with app.test_request_context():
        assert outbound_session_headers() == {}


def test_outbound_headers_truncates_to_128(app):
    with app.app_context():
        h = outbound_session_headers(session_id='x' * 200)
        assert len(h['X-Operator-Session-Id']) == 128


# --- CdrClient header -------------------------------------------------------

def test_cdr_client_headers_include_operator_session(app):
    with app.app_context():
        with_sid = CdrClient._headers('req-1', session_id='sid-c')
        assert with_sid['X-Operator-Session-Id'] == 'sid-c'
        # machine-to-machine (no operator session) omits the header entirely
        without = CdrClient._headers('req-1')
        assert 'X-Operator-Session-Id' not in without
        assert CdrClient._headers('req-1', session_id=None) == without


# --- forwarder replays the captured session id ------------------------------

def _valid_pending_row(operator_session_id):
    return CdrDeliveryLog(
        patient_guid='pat-1',
        service_request_guid='sr-1',
        concept_guid='concept-1',
        contract_guid='contract-1',
        provider_org_guid='org-1',
        fhir_observation_json={'resourceType': 'Observation'},
        operator_session_id=operator_session_id,
        status='pending',
    )


def test_forwarder_replays_operator_session_id(app, db):
    row = _valid_pending_row('sid-fwd')
    _db.session.add(row)
    _db.session.flush()

    captured = {}

    def _fake_deliver(payload, request_id, session_id=None):
        captured['session_id'] = session_id
        return {'cdr_resource_id': 'cdr-xyz'}

    with app.app_context():
        with patch.object(cdr_forwarder, '_build_payload', return_value={}), \
             patch.object(CdrClient, 'deliver_one', side_effect=_fake_deliver):
            ok = cdr_forwarder._deliver_one(row)

    assert ok is True
    assert captured['session_id'] == 'sid-fwd'


def test_forwarder_passes_none_for_machine_push(app, db):
    row = _valid_pending_row(None)
    _db.session.add(row)
    _db.session.flush()

    captured = {}

    def _fake_deliver(payload, request_id, session_id=None):
        captured['session_id'] = session_id
        return {'cdr_resource_id': 'cdr-xyz'}

    with app.app_context():
        with patch.object(cdr_forwarder, '_build_payload', return_value={}), \
             patch.object(CdrClient, 'deliver_one', side_effect=_fake_deliver):
            cdr_forwarder._deliver_one(row)

    assert captured['session_id'] is None
