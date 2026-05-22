"""Authentication decorators for provider-facing endpoints.

PATs are issued by request.pdhc. The gateway validates them and
derives the provider identity from the token — never from request params.
"""
import functools
import logging
from flask import request, g, jsonify
from ..services.pat_validation import PATValidationService
from ..models import AuditLog
from ..extensions import db

logger = logging.getLogger(__name__)


def _err(code, message, status):
    """Build a spec-conforming error response envelope.

    Includes both `error` (per provider integration guide vers2 Phase G)
    and `code` (existing internal contract), plus `service_request_guid`
    when the route is scoped to one (Flask URL view_args).
    """
    body = {'error': code, 'code': code, 'message': message}
    sr = (request.view_args or {}).get('service_request_guid')
    if sr:
        body['service_request_guid'] = sr
    return jsonify(body), status


def require_provider_token(scope=None):
    """Decorator: validate X-Provider-Token and set g.pat_result.

    Args:
        scope: Required scope ('read', 'write', or None for any)
    """
    def decorator(f):
        @functools.wraps(f)
        def wrapped(*args, **kwargs):
            raw_token = request.headers.get('X-Provider-Token')

            if not raw_token:
                _audit_rejected('missing_token')
                return _err('UNAUTHORIZED', 'X-Provider-Token header required', 401)

            result = PATValidationService.validate(raw_token)

            if not result.valid:
                _audit_rejected(result.error)
                return _err('UNAUTHORIZED', f'Invalid provider token: {result.error}', 401)

            # Check scope if required
            if scope and not result.has_scope(scope):
                _audit_rejected(f'scope_mismatch: need {scope}, have {result.scopes}')
                return _err('FORBIDDEN', f'Token lacks required scope: {scope}', 403)

            # Set provider context on g
            g.pat_result = result
            g.raw_token = raw_token
            g.provider_org_guid = result.provider_org_guid
            g.contract_guid = result.contract_guid

            # Audit successful validation
            _audit_validated(result.provider_org_guid)

            return f(*args, **kwargs)
        return wrapped
    return decorator


def _audit_validated(provider_org_guid):
    """Log successful PAT validation."""
    try:
        entry = AuditLog(
            event_type='pat.validated',
            actor_guid=provider_org_guid,
            ip_address=request.remote_addr,
            correlation_id=request.headers.get('X-Correlation-Id'),
            payload_snapshot={
                'endpoint': request.path,
                'method': request.method,
            },
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()


def _audit_rejected(reason):
    """Log failed PAT validation."""
    try:
        entry = AuditLog(
            event_type='pat.rejected',
            ip_address=request.remote_addr,
            correlation_id=request.headers.get('X-Correlation-Id'),
            payload_snapshot={
                'endpoint': request.path,
                'method': request.method,
                'reason': reason,
            },
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()
