"""PAT validation service.

PATs are issued by request.pdhc. The gateway validates them by calling
request.pdhc's validation endpoint and caching the result locally.

The provider's identity (org_guid, contract_guid, scopes) is derived
from the PAT record — NEVER from request parameters.
"""
import logging
import requests as http_requests
from datetime import datetime, timezone
from flask import current_app
from ..models import GuidResolutionCache
from ..extensions import db

logger = logging.getLogger(__name__)


class PATValidationResult:
    """Result of a PAT validation."""

    def __init__(self, valid, provider_org_guid=None, contract_guid=None,
                 scopes=None, delivery_mode=None, error=None,
                 push_endpoint_url=None, push_secret=None):
        self.valid = valid
        self.provider_org_guid = provider_org_guid
        self.contract_guid = contract_guid
        self.scopes = scopes or ''
        self.delivery_mode = delivery_mode
        self.error = error
        # Receipt routing — where gateway should send receipts for reports
        # submitted with this PAT. Derived from the PAT record in request.pdhc.
        self.push_endpoint_url = push_endpoint_url
        self.push_secret = push_secret

    def has_scope(self, scope):
        return scope in self.scopes.split(',')


class PATValidationService:
    """Validates Provider Access Tokens issued by request.pdhc."""

    @staticmethod
    def validate(raw_token):
        """Validate a PAT and return the associated provider identity.

        1. Check local cache first
        2. If cache miss or expired, call request.pdhc
        3. Cache the result
        """
        if not raw_token:
            return PATValidationResult(valid=False, error='Missing token')

        # Check cache
        cached = PATValidationService._check_cache(raw_token)
        if cached is not None:
            return cached

        # Call upstream
        result = PATValidationService._validate_upstream(raw_token)

        # Cache if valid
        if result.valid:
            PATValidationService._cache_result(raw_token, result)

        return result

    @staticmethod
    def _check_cache(raw_token):
        """Check if we have a cached validation for this token."""
        import hashlib
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        cached = GuidResolutionCache.query.filter_by(
            source_guid=token_hash,
            source_type='pat_validation',
        ).first()

        if not cached or cached.is_expired():
            return None

        data = cached.resolved_json
        if not data:
            return None

        return PATValidationResult(
            valid=True,
            provider_org_guid=data.get('provider_org_guid'),
            contract_guid=data.get('contract_guid'),
            scopes=data.get('scopes', ''),
            delivery_mode=data.get('delivery_mode'),
            push_endpoint_url=data.get('push_endpoint_url'),
            push_secret=data.get('push_secret'),
        )

    @staticmethod
    def _validate_upstream(raw_token):
        """Call request.pdhc to validate the PAT."""
        base_url = current_app.config.get('REQUEST_SERVICE_URL')
        if not base_url:
            logger.error('REQUEST_SERVICE_URL not configured')
            return PATValidationResult(valid=False, error='Upstream not configured')

        url = f'{base_url.rstrip("/")}/provider/validate-token'

        try:
            resp = http_requests.post(
                url,
                json={'token': raw_token},
                headers={'Content-Type': 'application/json'},
                timeout=10,
            )

            if resp.status_code == 200:
                data = resp.json()
                return PATValidationResult(
                    valid=True,
                    provider_org_guid=data.get('provider_org_guid'),
                    contract_guid=data.get('contract_guid'),
                    scopes=data.get('scopes', 'read,write'),
                    delivery_mode=data.get('delivery_mode', 'poll'),
                    push_endpoint_url=data.get('push_endpoint_url'),
                    push_secret=data.get('push_auth_key'),
                )
            elif resp.status_code in (401, 403):
                reason = 'expired or revoked'
                try:
                    reason = resp.json().get('message', reason)
                except Exception:
                    pass
                return PATValidationResult(valid=False, error=reason)
            else:
                logger.warning('Upstream PAT validation returned %d', resp.status_code)
                return PATValidationResult(
                    valid=False, error=f'Upstream error: HTTP {resp.status_code}'
                )

        except http_requests.ConnectionError:
            logger.warning('Cannot reach request.pdhc for PAT validation')
            return PATValidationResult(valid=False, error='Upstream unreachable')
        except Exception as e:
            logger.error('PAT validation error: %s', str(e))
            return PATValidationResult(valid=False, error=str(e))

    @staticmethod
    def _cache_result(raw_token, result):
        """Cache a successful PAT validation."""
        import hashlib
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        ttl = current_app.config.get('GUID_CACHE_TTL_SECONDS', 3600)

        existing = GuidResolutionCache.query.filter_by(
            source_guid=token_hash,
            source_type='pat_validation',
        ).first()

        data = {
            'provider_org_guid': result.provider_org_guid,
            'contract_guid': result.contract_guid,
            'scopes': result.scopes,
            'delivery_mode': result.delivery_mode,
            'push_endpoint_url': result.push_endpoint_url,
            'push_secret': result.push_secret,
        }

        if existing:
            existing.resolved_json = data
            existing.fetched_at = datetime.now(timezone.utc)
            existing.ttl_seconds = ttl
        else:
            entry = GuidResolutionCache(
                source_guid=token_hash,
                source_type='pat_validation',
                resolved_json=data,
                fetched_from=current_app.config.get('REQUEST_SERVICE_URL'),
                ttl_seconds=ttl,
            )
            db.session.add(entry)

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
