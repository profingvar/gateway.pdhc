"""ServiceRequest context service.

Fetches pre-extracted SR context from request.pdhc internal API.
Replaces the old approach of fetching full FHIR SR + parsing locally.

The context includes transactions[] and goals[] already extracted
by request.pdhc's ContextService — no FHIR parsing needed here.
"""
import logging
from datetime import datetime, timezone

import requests as http_requests
from flask import current_app

from ..models import GuidResolutionCache
from ..extensions import db

logger = logging.getLogger(__name__)


class SRContextResult:
    """Pre-extracted ServiceRequest context."""

    def __init__(self, found, data=None, error=None):
        self.found = found
        self.error = error
        self._data = data or {}

    @property
    def patient_guid(self):
        return self._data.get('patient_guid', '')

    @property
    def contract_guid(self):
        return self._data.get('contract_guid', '')

    @property
    def requester_org_guid(self):
        return self._data.get('requester_org_guid', '')

    @property
    def status(self):
        return self._data.get('status', '')

    @property
    def period_end(self):
        """Parsed period_end as aware datetime, or None if missing/unparseable.

        request.pdhc serialises this as ISO-8601 (naive UTC from the DB).
        The gateway uses it to flag late submissions (ticket #90).
        """
        raw = self._data.get('period_end')
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    @property
    def transactions(self):
        return self._data.get('transactions', [])

    @property
    def goals(self):
        return self._data.get('goals', [])

    def transaction_map(self):
        """Build {transaction_guid: transaction_dict} for fast lookup."""
        return {
            t['transaction_guid']: t
            for t in self.transactions
            if t.get('transaction_guid')
        }


class SRContextService:
    """Fetches SR context from request.pdhc internal API."""

    @staticmethod
    def fetch(service_request_guid):
        """Fetch SR context, using cache when available.

        Returns:
            SRContextResult
        """
        if not service_request_guid:
            return SRContextResult(found=False, error='Missing service_request_guid')

        # Check cache
        cached = _get_cached(service_request_guid)
        if cached is not None:
            return SRContextResult(found=True, data=cached)

        # Fetch from request.pdhc
        data = _fetch_upstream(service_request_guid)
        if data is None:
            return SRContextResult(
                found=False,
                error='ServiceRequest context unavailable',
            )

        # Cache result
        _set_cache(service_request_guid, data)
        return SRContextResult(found=True, data=data)


def _get_cached(sr_guid):
    """Check cache for SR context."""
    cached = GuidResolutionCache.query.filter_by(
        source_guid=sr_guid,
        source_type='sr_context',
    ).first()
    if cached and not cached.is_expired():
        return cached.resolved_json
    return None


def _set_cache(sr_guid, data):
    """Cache SR context (long TTL — SR is immutable after finalization)."""
    ttl = current_app.config.get('GUID_CACHE_TTL_SECONDS', 3600)
    existing = GuidResolutionCache.query.filter_by(
        source_guid=sr_guid,
        source_type='sr_context',
    ).first()
    if existing:
        existing.resolved_json = data
        existing.fetched_at = datetime.now(timezone.utc)
        existing.ttl_seconds = ttl
    else:
        entry = GuidResolutionCache(
            source_guid=sr_guid,
            source_type='sr_context',
            resolved_json=data,
            fetched_from=current_app.config.get('REQUEST_SERVICE_URL'),
            ttl_seconds=ttl,
        )
        db.session.add(entry)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


def _fetch_upstream(sr_guid):
    """Fetch SR context from request.pdhc internal API."""
    base_url = current_app.config.get('REQUEST_SERVICE_URL', '')
    service_key = current_app.config.get('REQUEST_INTERNAL_SERVICE_KEY', '')

    if not base_url or not service_key:
        logger.error('REQUEST_SERVICE_URL or REQUEST_INTERNAL_SERVICE_KEY not configured')
        return None

    try:
        resp = http_requests.get(
            f'{base_url}/internal/service-request/{sr_guid}/context',
            headers={'X-Service-Key': service_key},
            timeout=10,
        )
    except http_requests.RequestException as e:
        logger.error('SR context fetch failed: %s', e)
        return None

    if resp.status_code == 401:
        logger.error('SR context auth rejected — check REQUEST_INTERNAL_SERVICE_KEY')
        return None

    if resp.status_code == 404:
        logger.warning('ServiceRequest %s not found', sr_guid)
        return None

    if resp.status_code != 200:
        logger.warning('SR context returned %d for %s', resp.status_code, sr_guid)
        return None

    return resp.json()
