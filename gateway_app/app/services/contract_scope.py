"""Contract scope validation service.

Fetches contract scope from contract.pdhc internal API and validates
that observation concept_guids fall within the contract's return_scope.

Scope rules:
- Contract revoked/terminated/cancelled → reject all
- scope_defined=false → all concepts permitted (backward compatible)
- Each observation concept must appear in return_scope (obligatory or optional)
- On status=completed: all obligatory_return concepts must be present
"""
import logging
from datetime import datetime, timezone

import requests as http_requests
from flask import current_app

from ..models import GuidResolutionCache, InboundObservation
from ..extensions import db

logger = logging.getLogger(__name__)

# Statuses that mean the contract is no longer valid for submissions
DEAD_STATUSES = frozenset({'revoked', 'terminated', 'cancelled'})


class ContractScopeResult:
    """Result of a contract scope check."""

    def __init__(self, valid, error=None, error_code=None,
                 scope_defined=False, request_scope=None, return_scope=None):
        self.valid = valid
        self.error = error
        self.error_code = error_code
        self.scope_defined = scope_defined
        self.request_scope = request_scope or []
        self.return_scope = return_scope or {}

    @staticmethod
    def _coerce_to_guids(entries):
        """Normalize a scope entry list to a set of bare concept GUIDs.

        contract.pdhc returns entries as dicts:
            {"concept_guid": "<uuid>", "concept_url": "https://…/<uuid>"}
        Older shapes (and tests) sometimes use bare GUID strings. Accept both.
        Unknown shapes are skipped with a warning rather than 500-ing.
        """
        out = set()
        for entry in entries or []:
            if isinstance(entry, str):
                out.add(entry)
            elif isinstance(entry, dict):
                guid = entry.get('concept_guid')
                if guid:
                    out.add(guid)
                else:
                    logger.warning('Scope entry missing concept_guid: %r', entry)
            else:
                logger.warning('Unrecognised scope entry type %s: %r',
                               type(entry).__name__, entry)
        return out

    @property
    def obligatory_guids(self):
        return self._coerce_to_guids(self.return_scope.get('obligatory_return', []))

    @property
    def optional_guids(self):
        return self._coerce_to_guids(self.return_scope.get('optional_return', []))

    @property
    def all_permitted_guids(self):
        return self.obligatory_guids | self.optional_guids


class ContractScopeService:
    """Validates observations against contract scope from contract.pdhc."""

    @staticmethod
    def fetch_scope(contract_guid):
        """Fetch contract scope, using cache when available.

        Returns:
            ContractScopeResult
        """
        if not contract_guid:
            return ContractScopeResult(
                valid=False,
                error='Missing contract_guid',
                error_code='MISSING_CONTRACT',
            )

        # Check cache
        cached = _get_cached(contract_guid)
        if cached is not None:
            return _build_result(cached)

        # Fetch from contract.pdhc
        data = _fetch_upstream(contract_guid)
        if data is None:
            return ContractScopeResult(
                valid=False,
                error='Contract scope service unavailable',
                error_code='SERVICE_UNAVAILABLE',
            )

        # Cache result
        _set_cache(contract_guid, data)
        return _build_result(data)

    @staticmethod
    def fetch_parties(contract_guid):
        """Return {'requesting_org_guid': str|None, 'provider_org_guids': [str]}.

        Reuses the same cache row populated by fetch_scope() — contract.pdhc
        now returns a 'parties' field on /internal/contract/<guid>/scope.
        Returns None on failure (caller decides whether to skip or 502).
        """
        if not contract_guid:
            return None
        cached = _get_cached(contract_guid)
        if cached is None:
            cached = _fetch_upstream(contract_guid)
            if cached is None:
                return None
            _set_cache(contract_guid, cached)
        return cached.get('parties') or {
            'requesting_org_guid': None,
            'provider_org_guids': [],
        }

    @staticmethod
    def validate_observations(scope_result, observations, status='in-progress',
                              service_request_guid=None):
        """Validate observation concepts against contract scope.

        Args:
            scope_result: ContractScopeResult from fetch_scope()
            observations: list of observation dicts (must have concept_guid)
            status: report status — 'completed' triggers obligatory check
            service_request_guid: when status == 'completed', union with
                concepts from prior submissions for this SR so an
                obligatory satisfied in an earlier in-progress batch is
                not re-required in the closing batch (Phase G #9).

        Returns:
            (valid, errors) tuple
        """
        if not scope_result.valid:
            return False, [scope_result.error]

        if not scope_result.scope_defined:
            # No scope defined — backward compatible, all concepts allowed
            return True, []

        errors = []
        permitted = scope_result.all_permitted_guids

        # Check each observation's concept is in return_scope
        for i, obs in enumerate(observations):
            concept_guid = obs.get('concept_guid')
            if not concept_guid:
                continue  # concept_guid may be resolved later from transaction
            if concept_guid not in permitted:
                errors.append({
                    'observation_index': i,
                    'concept_guid': concept_guid,
                    'message': 'Concept not in contract return scope',
                })

        # On completed: all obligatory concepts must be present across the
        # CURRENT batch ∪ prior submissions on the same SR (Phase G #9).
        if status == 'completed' and scope_result.obligatory_guids:
            submitted_concepts = {
                obs.get('concept_guid') for obs in observations
                if obs.get('concept_guid')
            }
            if service_request_guid:
                prior = (
                    InboundObservation.query
                    .with_entities(InboundObservation.concept_guid)
                    .filter(InboundObservation.service_request_guid == service_request_guid)
                    .filter(InboundObservation.concept_guid.isnot(None))
                    .distinct()
                    .all()
                )
                submitted_concepts |= {row.concept_guid for row in prior}
            missing = scope_result.obligatory_guids - submitted_concepts
            if missing:
                errors.append({
                    'message': 'Missing obligatory concepts for completed status',
                    'missing_concept_guids': sorted(missing),
                })

        return len(errors) == 0, errors


def _build_result(data):
    """Build ContractScopeResult from cached/fetched data."""
    status = data.get('status', '')
    if status in DEAD_STATUSES:
        return ContractScopeResult(
            valid=False,
            error=f'Contract is {status}',
            error_code='CONTRACT_INACTIVE',
        )

    return ContractScopeResult(
        valid=True,
        scope_defined=data.get('scope_defined', False),
        request_scope=data.get('request_scope', []),
        return_scope=data.get('return_scope', {}),
    )


def _get_cached(contract_guid):
    """Check cache for contract scope."""
    cached = GuidResolutionCache.query.filter_by(
        source_guid=contract_guid,
        source_type='contract_scope',
    ).first()
    if cached and not cached.is_expired():
        return cached.resolved_json
    return None


def _set_cache(contract_guid, data):
    """Cache contract scope result."""
    ttl = current_app.config.get('GRANT_CACHE_TTL_SECONDS', 60)
    existing = GuidResolutionCache.query.filter_by(
        source_guid=contract_guid,
        source_type='contract_scope',
    ).first()
    if existing:
        existing.resolved_json = data
        existing.fetched_at = datetime.now(timezone.utc)
        existing.ttl_seconds = ttl
    else:
        entry = GuidResolutionCache(
            source_guid=contract_guid,
            source_type='contract_scope',
            resolved_json=data,
            fetched_from=current_app.config.get('CONTRACT_SERVICE_URL'),
            ttl_seconds=ttl,
        )
        db.session.add(entry)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


def _fetch_upstream(contract_guid):
    """Fetch scope from contract.pdhc internal API."""
    base_url = current_app.config.get('CONTRACT_SERVICE_URL', '')
    service_key = current_app.config.get('CONTRACT_INTERNAL_SERVICE_KEY', '')

    if not base_url or not service_key:
        logger.error('CONTRACT_SERVICE_URL or CONTRACT_INTERNAL_SERVICE_KEY not configured')
        return None

    try:
        resp = http_requests.get(
            f'{base_url}/internal/contract/{contract_guid}/scope',
            headers={'X-Service-Key': service_key},
            timeout=10,
        )
    except http_requests.RequestException as e:
        logger.error('Contract scope fetch failed: %s', e)
        return None

    if resp.status_code == 401:
        logger.error('Contract scope auth rejected — check CONTRACT_INTERNAL_SERVICE_KEY')
        return None

    if resp.status_code == 404:
        logger.warning('Contract %s not found on contract.pdhc', contract_guid)
        return None

    if resp.status_code != 200:
        logger.warning('Contract scope returned %d for %s', resp.status_code, contract_guid)
        return None

    return resp.json()
