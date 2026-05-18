"""Tests for SSO authentication and role-based access control."""
import pytest
from unittest.mock import patch
from app import create_app
from app.extensions import db as _db


class SSOEnabledConfig:
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


ADMIN_BLOB = {
    'user_guid': 'admin-001',
    'email': 'admin@pdhc.se',
    'display_name': 'Admin User',
    'user_type': 'professional',
    'is_su_admin': True,
    'effective_phases': ['planning', 'request', 'provider', 'analysis'],
    'organization_ids': ['org-A'],
}

ANALYST_BLOB = {
    'user_guid': 'analyst-001',
    'email': 'analyst@pdhc.se',
    'display_name': 'Analyst User',
    'user_type': 'professional',
    'is_su_admin': False,
    'effective_phases': ['analysis'],
    'organization_ids': ['org-B'],
}

NO_ACCESS_BLOB = {
    'user_guid': 'patient-001',
    'email': 'patient@example.com',
    'display_name': 'Patient',
    'user_type': 'patient',
    'is_su_admin': False,
    'effective_phases': [],
    'organization_ids': [],
}


@pytest.fixture
def sso_app():
    app = create_app(config_class=SSOEnabledConfig)
    with app.app_context():
        _db.create_all()
        yield app
        _db.drop_all()


@pytest.fixture
def sso_client(sso_app):
    return sso_app.test_client()


def _login_as(client, blob, token='test-token-123'):
    """Simulate SSO login by setting session directly."""
    with client.session_transaction() as sess:
        sess['sso_token'] = token
        sess['access_blob'] = blob
        sess['role'] = 'admin' if blob.get('is_su_admin') else 'analyst'


class TestLoginRedirect:

    def test_unauthenticated_redirects_to_login(self, sso_client):
        resp = sso_client.get('/')
        assert resp.status_code == 302
        assert '/auth/login' in resp.location

    def test_login_redirects_to_sso(self, sso_client):
        resp = sso_client.get('/auth/login')
        assert resp.status_code == 302
        assert 'sso.test/login' in resp.location

    def test_callback_without_token_redirects(self, sso_client):
        resp = sso_client.get('/auth/callback')
        assert resp.status_code == 302
        assert '/auth/login' in resp.location

    def test_callback_bad_state_redirects(self, sso_client):
        with sso_client.session_transaction() as sess:
            sess['sso_state'] = 'expected'
        resp = sso_client.get('/auth/callback?token=tok&state=wrong')
        assert resp.status_code == 302
        assert '/auth/login' in resp.location

    @patch('app.web.auth.validate_sso_token')
    def test_callback_invalid_token_redirects(self, mock_validate, sso_client):
        mock_validate.return_value = None
        with sso_client.session_transaction() as sess:
            sess['sso_state'] = 'good-state'
        resp = sso_client.get('/auth/callback?token=bad&state=good-state')
        assert resp.status_code == 302
        assert '/auth/login' in resp.location

    @patch('app.web.auth.validate_sso_token')
    def test_callback_no_access_rejected(self, mock_validate, sso_client):
        mock_validate.return_value = NO_ACCESS_BLOB
        with sso_client.session_transaction() as sess:
            sess['sso_state'] = 'st'
        resp = sso_client.get('/auth/callback?token=tok&state=st')
        assert resp.status_code == 302
        assert '/auth/login' in resp.location

    @patch('app.web.auth.validate_sso_token')
    def test_callback_success_admin(self, mock_validate, sso_client):
        mock_validate.return_value = ADMIN_BLOB
        with sso_client.session_transaction() as sess:
            sess['sso_state'] = 'st'
        resp = sso_client.get('/auth/callback?token=tok&state=st')
        assert resp.status_code == 302
        # Should redirect to dashboard
        assert '/' in resp.location

    @patch('app.web.auth.validate_sso_token')
    def test_callback_success_analyst(self, mock_validate, sso_client):
        mock_validate.return_value = ANALYST_BLOB
        with sso_client.session_transaction() as sess:
            sess['sso_state'] = 'st'
        resp = sso_client.get('/auth/callback?token=tok&state=st')
        assert resp.status_code == 302


class TestRoleAccess:

    def test_dashboard_accessible_to_analyst(self, sso_client):
        _login_as(sso_client, ANALYST_BLOB)
        resp = sso_client.get('/')
        assert resp.status_code == 200

    def test_dashboard_accessible_to_admin(self, sso_client):
        _login_as(sso_client, ADMIN_BLOB)
        resp = sso_client.get('/')
        assert resp.status_code == 200

    def test_observations_accessible_to_analyst(self, sso_client):
        _login_as(sso_client, ANALYST_BLOB)
        resp = sso_client.get('/observations')
        assert resp.status_code == 200

    def test_observations_accessible_to_admin(self, sso_client):
        _login_as(sso_client, ADMIN_BLOB)
        resp = sso_client.get('/observations')
        assert resp.status_code == 200

    def test_requests_accessible_to_analyst(self, sso_client):
        _login_as(sso_client, ANALYST_BLOB)
        resp = sso_client.get('/requests')
        assert resp.status_code == 200

    def test_pats_blocked_for_analyst(self, sso_client):
        _login_as(sso_client, ANALYST_BLOB)
        resp = sso_client.get('/pats')
        assert resp.status_code == 302  # redirect with flash

    def test_audit_blocked_for_analyst(self, sso_client):
        _login_as(sso_client, ANALYST_BLOB)
        resp = sso_client.get('/audit')
        assert resp.status_code == 302

    def test_grants_blocked_for_analyst(self, sso_client):
        _login_as(sso_client, ANALYST_BLOB)
        resp = sso_client.get('/grants')
        assert resp.status_code == 302

    def test_pats_accessible_to_admin(self, sso_client):
        _login_as(sso_client, ADMIN_BLOB)
        resp = sso_client.get('/pats')
        assert resp.status_code == 200

    def test_audit_accessible_to_admin(self, sso_client):
        _login_as(sso_client, ADMIN_BLOB)
        resp = sso_client.get('/audit')
        assert resp.status_code == 200

    def test_grants_accessible_to_admin(self, sso_client):
        _login_as(sso_client, ADMIN_BLOB)
        resp = sso_client.get('/grants')
        assert resp.status_code == 200

    def test_docs_accessible_to_analyst(self, sso_client):
        _login_as(sso_client, ANALYST_BLOB)
        resp = sso_client.get('/docs')
        assert resp.status_code == 200


class TestNavVisibility:

    def test_admin_sees_all_nav_links(self, sso_client):
        _login_as(sso_client, ADMIN_BLOB)
        resp = sso_client.get('/')
        html = resp.data.decode('utf-8')
        assert 'Observations' in html
        assert 'PATs' in html
        assert 'Audit' in html
        assert 'Grants' in html
        assert 'Logout' in html
        assert 'Admin User' in html

    def test_analyst_sees_limited_nav(self, sso_client):
        _login_as(sso_client, ANALYST_BLOB)
        resp = sso_client.get('/')
        html = resp.data.decode('utf-8')
        # Extract nav section only
        nav_html = html.split('<nav>')[1].split('</nav>')[0] if '<nav>' in html else html
        assert 'Observations' in nav_html
        assert 'Requests' in nav_html
        assert 'PATs' not in nav_html
        assert 'Audit' not in nav_html
        assert 'Grants' not in nav_html
        assert 'Analyst User' in nav_html

    def test_unauthenticated_sees_login(self, sso_client):
        resp = sso_client.get('/auth/login')
        # Redirects to SSO, but let's check we don't crash
        assert resp.status_code == 302


class TestLogout:

    @patch('app.web.auth.logout_sso')
    def test_logout_clears_session(self, mock_logout, sso_client):
        _login_as(sso_client, ADMIN_BLOB)
        resp = sso_client.get('/auth/logout')
        assert resp.status_code == 302
        assert '/auth/login' in resp.location
        mock_logout.assert_called_once()

        # Verify session cleared — dashboard should redirect
        resp2 = sso_client.get('/')
        assert resp2.status_code == 302


class TestSSOService:

    def test_map_role_admin(self):
        from app.services.sso_service import map_role
        assert map_role(ADMIN_BLOB) == 'admin'

    def test_map_role_analyst(self):
        from app.services.sso_service import map_role
        assert map_role(ANALYST_BLOB) == 'analyst'

    def test_map_role_no_access(self):
        from app.services.sso_service import map_role
        assert map_role(NO_ACCESS_BLOB) == 'read_only'

    def test_map_role_none(self):
        from app.services.sso_service import map_role
        assert map_role(None) == 'read_only'

    def test_has_analysis_access_admin(self):
        from app.services.sso_service import has_analysis_access
        assert has_analysis_access(ADMIN_BLOB) is True

    def test_has_analysis_access_analyst(self):
        from app.services.sso_service import has_analysis_access
        assert has_analysis_access(ANALYST_BLOB) is True

    def test_has_analysis_access_patient(self):
        from app.services.sso_service import has_analysis_access
        assert has_analysis_access(NO_ACCESS_BLOB) is False

    def test_is_admin(self):
        from app.services.sso_service import is_admin
        assert is_admin(ADMIN_BLOB) is True
        assert is_admin(ANALYST_BLOB) is False
        assert is_admin(None) is False


class TestAuthDisabledMode:
    """When AUTH_DISABLED=True, all routes should work without session."""

    def test_dashboard_no_login_needed(self, client):
        resp = client.get('/')
        assert resp.status_code == 200

    def test_observations_no_login_needed(self, client):
        resp = client.get('/observations')
        assert resp.status_code == 200

    def test_pats_no_login_needed(self, client):
        resp = client.get('/pats')
        assert resp.status_code == 200

    def test_audit_no_login_needed(self, client):
        resp = client.get('/audit')
        assert resp.status_code == 200

    def test_grants_no_login_needed(self, client):
        resp = client.get('/grants')
        assert resp.status_code == 200
