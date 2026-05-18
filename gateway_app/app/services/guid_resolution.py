"""GUID chain resolution service.

Resolves the chain:  transaction_guid → careplan → plandefinition
by calling request.pdhc to fetch the ServiceRequest context and
parsing the plan_definition_snapshot.

The gateway uses the provider's PAT (already validated) to call
request.pdhc's provider-facing endpoints.  For SSO-protected
endpoints (ServiceRequest detail), it uses a service API key
passed via X-Api-Key header.

All resolved data is cached in guid_resolution_cache with TTL.
"""
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

from flask import current_app
import requests as http_requests

from ..models import GuidResolutionCache
from ..extensions import db

logger = logging.getLogger(__name__)


@dataclass
class ResolvedChain:
    """Result of a GUID chain resolution."""
    resolved: bool = False
    service_request_guid: str = ''
    transaction_guid: str = ''
    # From ServiceRequest
    patient_guid: str = ''
    plan_definition_guid: str = ''
    plan_definition_snapshot: Optional[dict] = None
    # Matched transaction context
    concept_guid: str = ''
    concept_name: str = ''
    response_type: str = ''
    activity_description: str = ''
    # CarePlan-level context
    careplan_guid: str = ''
    careplan_title: str = ''
    careplan_status: str = ''
    # PlanDefinition-level context
    plandef_title: str = ''
    # Error info
    error: str = ''

    def to_context_dict(self):
        """Build the resolved context for vector storage."""
        return {
            'service_request_guid': self.service_request_guid,
            'transaction_guid': self.transaction_guid,
            'patient_guid': self.patient_guid,
            'plan_definition_guid': self.plan_definition_guid,
            'concept_guid': self.concept_guid,
            'concept_name': self.concept_name,
            'response_type': self.response_type,
            'activity_description': self.activity_description,
            'careplan_guid': self.careplan_guid,
            'careplan_title': self.careplan_title,
            'careplan_status': self.careplan_status,
            'plandef_title': self.plandef_title,
        }


class GuidResolutionService:

    @staticmethod
    def resolve(service_request_guid, transaction_guid=None):
        """Resolve the GUID chain for a service request.

        1. Check cache for the service request
        2. If miss, fetch from request.pdhc
        3. Parse plan_definition_snapshot to find transaction context
        4. Cache the resolved data

        Args:
            service_request_guid: The SR GUID from the observation
            transaction_guid: Optional — if provided, extract the
                specific transaction context from the snapshot

        Returns:
            ResolvedChain with the resolved context
        """
        result = ResolvedChain(
            service_request_guid=service_request_guid,
            transaction_guid=transaction_guid or '',
        )

        # Step 1: Check cache
        sr_data = _get_cached('service_request', service_request_guid)
        if sr_data is None:
            # Step 2: Fetch from request.pdhc
            sr_data = _fetch_service_request(service_request_guid)
            if sr_data is None:
                result.error = 'ServiceRequest not found or upstream unreachable'
                return result
            # Cache the result
            _set_cache('service_request', service_request_guid, sr_data,
                       f'{current_app.config["REQUEST_SERVICE_URL"]}/ServiceRequest/{service_request_guid}')

        # Step 3: Extract fields from ServiceRequest
        result.patient_guid = sr_data.get('patient_guid', '')
        result.plan_definition_guid = sr_data.get('plan_definition_guid', '')
        result.plan_definition_snapshot = sr_data.get('plan_definition_snapshot')

        # Extract PlanDefinition title
        snapshot = result.plan_definition_snapshot or {}
        result.plandef_title = snapshot.get('title', snapshot.get('name', ''))

        # Extract careplan info from the FHIR resource if available
        fhir_resource = sr_data.get('fhir_resource') or {}
        contained = fhir_resource.get('contained', [])
        for resource in contained:
            if resource.get('resourceType') == 'CarePlan':
                result.careplan_guid = resource.get('id', '')
                result.careplan_title = resource.get('title', '')
                result.careplan_status = resource.get('status', '')
                break

        # Step 4: If transaction_guid provided, find the matching activity
        if transaction_guid and snapshot:
            txn_context = _find_transaction_in_snapshot(
                snapshot, transaction_guid, result.careplan_guid)
            if txn_context:
                result.concept_guid = txn_context.get('concept_guid', '')
                result.concept_name = txn_context.get('concept_name', '')
                result.response_type = txn_context.get('response_type', '')
                result.activity_description = txn_context.get('activity_description', '')

        result.resolved = True
        return result

    @staticmethod
    def resolve_for_observation(observation):
        """Convenience: resolve chain for an InboundObservation record.

        Updates the observation's resolution_status.

        Returns:
            ResolvedChain
        """
        chain = GuidResolutionService.resolve(
            observation.service_request_guid,
            observation.transaction_guid,
        )
        if chain.resolved:
            observation.resolution_status = 'resolved'
        else:
            observation.resolution_status = 'failed'
        return chain


def _get_cached(source_type, source_guid):
    """Look up a cached resolution result."""
    cached = GuidResolutionCache.query.filter_by(
        source_guid=source_guid,
        source_type=source_type,
    ).first()
    if cached and not cached.is_expired():
        return cached.resolved_json
    return None


def _set_cache(source_type, source_guid, resolved_json, fetched_from=None):
    """Store a resolution result in cache."""
    ttl = current_app.config.get('GUID_CACHE_TTL_SECONDS', 3600)
    # Upsert
    cached = GuidResolutionCache.query.filter_by(
        source_guid=source_guid,
        source_type=source_type,
    ).first()
    if cached:
        cached.resolved_json = resolved_json
        cached.fetched_from = fetched_from
        cached.fetched_at = datetime.now(timezone.utc)
        cached.ttl_seconds = ttl
    else:
        cached = GuidResolutionCache(
            source_guid=source_guid,
            source_type=source_type,
            resolved_json=resolved_json,
            fetched_from=fetched_from,
            ttl_seconds=ttl,
        )
        db.session.add(cached)
    db.session.commit()


def _fetch_service_request(sr_guid):
    """Fetch a ServiceRequest from request.pdhc.

    Uses BOOTSTRAP_SU_API_KEY for service-to-service auth.
    """
    base_url = current_app.config['REQUEST_SERVICE_URL']
    api_key = current_app.config.get('BOOTSTRAP_SU_API_KEY')
    url = f'{base_url}/ServiceRequest/{sr_guid}'

    try:
        headers = {}
        if api_key:
            headers['X-Api-Key'] = api_key
        resp = http_requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        logger.warning('ServiceRequest fetch failed: %s %s', resp.status_code, url)
        return None
    except http_requests.ConnectionError:
        logger.warning('request.pdhc unreachable at %s', url)
        return None
    except Exception as e:
        logger.error('ServiceRequest fetch error: %s', e)
        return None


def _find_transaction_in_snapshot(snapshot, transaction_guid, careplan_guid=''):
    """Find a transaction within the PlanDefinition snapshot.

    The snapshot contains activities with codings. The transaction_guid
    may match either:
    - An activity.id directly (from the provider feed format)
    - A deterministic GUID derived from careplan_guid + activity_guid + 'txn' + sort_order
      (from parse_service in request.pdhc)

    We try both matching strategies.
    """
    actions = snapshot.get('action', [])
    for idx, action in enumerate(actions):
        action_id = action.get('id', '')

        # Direct match on action ID
        if action_id == transaction_guid:
            return _extract_action_context(action)

        # Try deterministic GUID match (request.pdhc parse_service formula)
        if careplan_guid:
            import hashlib
            import uuid as uuid_mod
            combined = '|'.join(str(p) for p in [careplan_guid, action_id, 'txn', idx] if p)
            det_guid = str(uuid_mod.UUID(hashlib.md5(combined.encode()).hexdigest()))
            if det_guid == transaction_guid:
                return _extract_action_context(action)

    # Fallback: search nested codings for concept_guid match
    for action in actions:
        codings = _get_codings(action)
        for coding in codings:
            if coding.get('code') == transaction_guid:
                return _extract_action_context(action)

    return None


def _extract_action_context(action):
    """Extract clinical context from a PlanDefinition action."""
    codings = _get_codings(action)
    first_coding = codings[0] if codings else {}

    return {
        'concept_guid': first_coding.get('code', ''),
        'concept_name': first_coding.get('display', ''),
        'response_type': action.get('type', {}).get('coding', [{}])[0].get('code', '')
                         if isinstance(action.get('type'), dict) else '',
        'activity_description': action.get('title', action.get('description', '')),
    }


def _get_codings(action):
    """Extract codings from a PlanDefinition action."""
    code = action.get('code', [])
    if isinstance(code, list):
        codings = []
        for c in code:
            codings.extend(c.get('coding', []) if isinstance(c, dict) else [])
        return codings
    elif isinstance(code, dict):
        return code.get('coding', [])
    return []
