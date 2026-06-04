"""Tests for AuditLog.session_id (ticket #222) — propagates the SSO
session_id (sid JWT claim, ticket #191) into gateway's existing
audit_log so multi-request reads chain back to one operator session.

Sources of session_id, in priority order (see
sso_service.current_session_id):
  1. X-Operator-Session-Id request header
  2. session['access_blob']['session_id']
  3. None (legacy callers / no request context)
"""
from __future__ import annotations

import pytest

from app import create_app
from app.extensions import db as _db
from app.models.audit_log import AuditLog


class _Config:
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    HMAC_SECRET = 'test-hmac-secret'
    BOOTSTRAP_SU_API_KEY = 'test-su-key'
    REQUEST_SERVICE_URL = 'http://mock/api/v1'
    PGVECTOR_DIMENSIONS = 384
    AUTH_DISABLED = False
    SSO_BASE_URL = 'https://sso.test'
    SSO_CLIENT_ID = 'gw-test'
    SSO_CLIENT_SECRET = 'gw-secret'
    SSO_CALLBACK_URL = 'https://gateway.test/auth/callback'
    SECRET_KEY = 'test-secret'


@pytest.fixture
def app222():
    app = create_app(config_class=_Config)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.rollback()
        for table in reversed(_db.metadata.sorted_tables):
            _db.session.execute(table.delete())
        _db.session.commit()


@pytest.fixture
def client222(app222):
    return app222.test_client()


# ---------------------------------------------------------------------------
# current_session_id() resolution
# ---------------------------------------------------------------------------

def test_current_session_id_prefers_header_over_blob(app222):
    """A service-key caller forwarding X-Operator-Session-Id beats
    whatever happens to be in the Flask session."""
    from app.services.sso_service import current_session_id

    with app222.test_request_context(
        '/some/path',
        headers={'X-Operator-Session-Id': 'sid-from-header'},
    ):
        from flask import session as flask_session
        flask_session['access_blob'] = {'session_id': 'sid-from-blob'}
        assert current_session_id() == 'sid-from-header'


def test_current_session_id_falls_back_to_blob(app222):
    from app.services.sso_service import current_session_id

    with app222.test_request_context('/some/path'):
        from flask import session as flask_session
        flask_session['access_blob'] = {'session_id': 'sid-from-blob'}
        assert current_session_id() == 'sid-from-blob'


def test_current_session_id_none_when_no_blob_no_header(app222):
    from app.services.sso_service import current_session_id

    with app222.test_request_context('/some/path'):
        assert current_session_id() is None


def test_current_session_id_truncates_oversized_header(app222):
    """The column is String(128); the resolver caps to avoid silent
    truncation surprises later in the request."""
    from app.services.sso_service import current_session_id

    oversized = 'x' * 200
    with app222.test_request_context(
        '/some/path',
        headers={'X-Operator-Session-Id': oversized},
    ):
        sid = current_session_id()
        assert sid is not None
        assert len(sid) == 128


def test_current_session_id_no_context_returns_none(app222):
    """CLI / scripts / background work — no Flask request, no error."""
    from app.services.sso_service import current_session_id

    with app222.app_context():  # app context, no request context
        assert current_session_id() is None


# ---------------------------------------------------------------------------
# AuditLog auto-fills session_id from request context
# ---------------------------------------------------------------------------

def test_audit_log_default_picks_up_header_sid(app222):
    """AuditLog() without an explicit session_id pulls it from the
    request context via the SQLAlchemy default factory."""
    with app222.test_request_context(
        '/some/path',
        headers={'X-Operator-Session-Id': 'sid-end-to-end'},
    ):
        row = AuditLog(event_type='observations.read',
                       actor_guid='user-1',
                       data_subject_guid='patient-9')
        _db.session.add(row)
        _db.session.commit()

        fetched = AuditLog.query.filter_by(event_type='observations.read').first()
        assert fetched is not None
        assert fetched.session_id == 'sid-end-to-end'


def test_audit_log_default_null_when_no_session_context(app222):
    """No Flask request at all → session_id stays NULL, write still
    succeeds (the column is nullable)."""
    with app222.app_context():
        row = AuditLog(event_type='cli.maintenance', actor_guid='cron')
        _db.session.add(row)
        _db.session.commit()

        fetched = AuditLog.query.filter_by(event_type='cli.maintenance').first()
        assert fetched is not None
        assert fetched.session_id is None


def test_audit_log_explicit_session_id_overrides_default(app222):
    """Callers can override the default — useful when an audit writer
    has a context-detached pipeline (background worker, retry queue)."""
    with app222.app_context():
        row = AuditLog(event_type='report.received',
                       actor_guid='provider-A',
                       session_id='explicitly-set-sid')
        _db.session.add(row)
        _db.session.commit()

        fetched = AuditLog.query.filter_by(event_type='report.received').first()
        assert fetched.session_id == 'explicitly-set-sid'


def test_to_dict_exposes_session_id(app222):
    with app222.app_context():
        row = AuditLog(event_type='evt', session_id='sid-1')
        _db.session.add(row)
        _db.session.commit()
        d = row.to_dict()
        assert 'session_id' in d
        assert d['session_id'] == 'sid-1'
