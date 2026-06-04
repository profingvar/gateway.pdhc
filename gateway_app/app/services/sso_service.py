"""SSO integration — token validation, role mapping, access helpers."""
import requests
from flask import current_app, request, session


def current_session_id():
    """Return the SSO session_id ("sid" JWT claim, see ticket #191)
    for the current request, or None if not available.

    Resolution order (ticket #222):
      1. ``X-Operator-Session-Id`` header — canonical carrier for
         internal service-key callers (sim.pdhc / monitor.pdhc / etc.)
         that don't go through the SSO blob.
      2. ``session['access_blob']['session_id']`` — set by
         ``get_access_blob`` on each fresh /me/service response.
      3. None — legacy caller / AUTH_DISABLED dev blob without the
         claim. Audit row gets NULL; downstream PDL kontroller queries
         must treat NULL as "no session correlation available".
    """
    try:
        header_val = request.headers.get('X-Operator-Session-Id')
    except RuntimeError:
        # No active request context (e.g. background CLI work).
        header_val = None
    if header_val:
        # Cap length at the column's storage to avoid silent truncation.
        return header_val[:128]
    blob = session.get('access_blob') if session else None
    if isinstance(blob, dict):
        sid = blob.get('session_id')
        if sid:
            return str(sid)[:128]
    return None


def initiate_sso_login(next_url, state):
    """Build SSO login redirect URL."""
    sso_base = current_app.config['SSO_BASE_URL']
    callback = current_app.config['SSO_CALLBACK_URL']
    return f"{sso_base}/login?next={callback}&state={state}"


def validate_sso_token(token):
    """Validate token against SSO, return access blob or None."""
    sso_base = current_app.config['SSO_BASE_URL']
    try:
        resp = requests.get(
            f"{sso_base}/api/auth/me/service",
            headers={
                'Authorization': f'Bearer {token}',
                'X-SSO-Client-Id': current_app.config['SSO_CLIENT_ID'],
                'X-SSO-Client-Secret': current_app.config['SSO_CLIENT_SECRET'],
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
        return None
    except requests.RequestException:
        return None


def logout_sso(token):
    """Call SSO logout endpoint."""
    sso_base = current_app.config['SSO_BASE_URL']
    try:
        requests.post(
            f"{sso_base}/api/auth/logout",
            headers={'Authorization': f'Bearer {token}'},
            timeout=10,
        )
    except requests.RequestException:
        pass


def _clear_sso_session():
    """Drop SSO state from the Flask session. Called when SSO rejects a
    previously-valid token (idle timeout, revoked session, password reset)."""
    session.pop('sso_token', None)
    session.pop('access_blob', None)
    session.pop('role', None)


def get_access_blob():
    """Return the access blob, re-validated against SSO on every call.

    Ticket #93: no caching. session['access_blob'] is retained only as a
    display-side convenience and refreshed from each fresh /me/service
    response. This is what makes SSO-side idle timeouts (10-min inactivity)
    and forced logouts take effect immediately on gateway.pdhc, matching
    the behaviour of request.pdhc / plan.pdhc / dashboard.pdhc (ticket #50).

    Returns the blob on success, or None if the token is missing, expired,
    or revoked (in which case the local session is wiped).
    """
    if current_app.config.get('AUTH_DISABLED'):
        return {
            'user_guid': 'dev-admin-guid',
            'email': 'dev@localhost',
            'display_name': 'Dev Admin',
            'user_type': 'professional',
            'is_su_admin': True,
            'effective_phases': ['planning', 'request', 'provider', 'analysis'],
            'organization_ids': [],
        }
    token = session.get('sso_token')
    if not token:
        return None
    blob = validate_sso_token(token)
    if blob is None:
        _clear_sso_session()
        return None
    session['access_blob'] = blob
    return blob


def map_role(access_blob):
    """Map SSO access blob → local role string.

    admin     — is_su_admin
    analyst   — professional with 'analysis' in effective_phases
    read_only — everything else (should not reach protected views)
    """
    if not access_blob:
        return 'read_only'
    if access_blob.get('is_su_admin'):
        return 'admin'
    phases = access_blob.get('effective_phases') or []
    if access_blob.get('user_type') == 'professional' and 'analysis' in phases:
        return 'analyst'
    return 'read_only'


def has_analysis_access(access_blob):
    """True if user is admin or has analysis phase assignment."""
    if not access_blob:
        return False
    if access_blob.get('is_su_admin'):
        return True
    phases = access_blob.get('effective_phases') or []
    return access_blob.get('user_type') == 'professional' and 'analysis' in phases


def is_admin(access_blob):
    """True if user is SU admin."""
    return bool(access_blob and access_blob.get('is_su_admin'))
