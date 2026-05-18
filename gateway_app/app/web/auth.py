"""SSO auth routes — login, callback, logout."""
import uuid
from functools import wraps
from flask import (
    Blueprint, redirect, request, session, url_for, flash, current_app,
)
from ..services.sso_service import (
    initiate_sso_login, validate_sso_token, logout_sso,
    get_access_blob, map_role, has_analysis_access, is_admin,
)

auth_bp = Blueprint('auth', __name__)


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def require_login(f):
    """Redirect to SSO login if not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if current_app.config.get('AUTH_DISABLED'):
            return f(*args, **kwargs)
        blob = get_access_blob()
        if not blob:
            session['sso_next'] = request.url
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated


def require_analyst(f):
    """Require login + analysis phase (or admin)."""
    @wraps(f)
    @require_login
    def decorated(*args, **kwargs):
        blob = get_access_blob()
        if not has_analysis_access(blob):
            flash('Access denied — analysis phase required.', 'danger')
            return redirect(url_for('web.dashboard'))
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    """Require login + SU admin."""
    @wraps(f)
    @require_login
    def decorated(*args, **kwargs):
        blob = get_access_blob()
        if not is_admin(blob):
            flash('Access denied — admin only.', 'danger')
            return redirect(url_for('web.dashboard'))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@auth_bp.route('/login')
def login():
    if current_app.config.get('AUTH_DISABLED'):
        return redirect(url_for('web.dashboard'))

    next_url = request.args.get('next', url_for('web.dashboard'))
    state = str(uuid.uuid4())
    session['sso_state'] = state
    session['sso_next'] = next_url
    return redirect(initiate_sso_login(next_url, state))


@auth_bp.route('/callback')
def callback():
    token = request.args.get('token')
    state = request.args.get('state')

    if not token:
        flash('No token received from SSO.', 'danger')
        return redirect(url_for('auth.login'))

    expected_state = session.pop('sso_state', None)
    if state != expected_state:
        flash('Invalid state — please try again.', 'danger')
        return redirect(url_for('auth.login'))

    access_blob = validate_sso_token(token)
    if not access_blob:
        flash('SSO token validation failed.', 'danger')
        return redirect(url_for('auth.login'))

    role = map_role(access_blob)
    if role == 'read_only':
        flash('Access denied — you need an analysis phase assignment or admin privileges.', 'danger')
        return redirect(url_for('auth.login'))

    session['sso_token'] = token
    session['access_blob'] = access_blob
    session['role'] = role
    session.permanent = True

    next_url = session.pop('sso_next', url_for('web.dashboard'))
    return redirect(next_url)


@auth_bp.route('/logout', methods=['GET', 'POST'])
def logout():
    token = session.get('sso_token')
    if token:
        logout_sso(token)
    session.clear()
    return redirect(url_for('auth.login'))
