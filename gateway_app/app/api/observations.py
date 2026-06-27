"""Observation read API for analyse-phase consumers (e.g. dashboard.pdhc).

GET /api/v1/observations?organization=<org_guid>

Auth: Authorization: Bearer <SSO token> — validated via the same SSO
flow gateway already uses for its web routes.

Phase gate: caller must be SU admin OR have 'analysis' in effective_phases.

Org scoping: org_guid must be in the caller's `organization_ids` blob
field (admin bypass). The org_guid is treated as the *requesting*
organisation — i.e. the org that ordered the underlying service request,
which lives on the contract in contract.pdhc. We resolve every relevant
contract via gateway's existing ContractScopeService cache and only
return observations whose contract's requesting_org matches.

Returns a FHIR R5 searchset Bundle of Observation resources.
"""
from datetime import datetime, timezone
import json
import logging

from flask import request, jsonify, current_app

from . import api_bp
from ..models import InboundObservation, ServiceRequestStatus, AuditLog, GuidResolutionCache
from ..extensions import db
from ..services.sso_service import validate_sso_token, has_analysis_access
from ..services.contract_scope import ContractScopeService
from ..services.fhir_observation_builder import build_fhir_observation
from ..services.ips_client import (
    fetch_blocks_for_patients,
    filter_blocked_observations,
)

logger = logging.getLogger(__name__)


def _bearer_token():
    h = request.headers.get('Authorization', '')
    if not h.startswith('Bearer '):
        return None
    return h[7:].strip() or None


@api_bp.route('/observations', methods=['GET'])
def list_observations():
    org_guid = (request.args.get('organization') or '').strip()
    if not org_guid:
        return jsonify({'error': 'missing organization parameter'}), 400

    token = _bearer_token()
    if not token:
        return jsonify({'error': 'missing bearer token'}), 401

    blob = validate_sso_token(token)
    if not blob:
        return jsonify({'error': 'invalid token'}), 401

    if not has_analysis_access(blob):
        return jsonify({'error': 'analysis phase required'}), 403

    is_admin = bool(blob.get('is_su_admin'))
    user_orgs = list(blob.get('organization_ids') or [])
    if not is_admin and org_guid not in user_orgs:
        return jsonify({'error': 'organization not in your scope'}), 403

    # PDL Ch 4 §1 + Lag (2022:913) §5 (ticket #220). When an SU admin
    # reads observations for an org outside their own affiliations, the
    # read bypasses normal access scoping. That bypass must be
    # explicitly justified and audited as a distinct event
    # (observations.admin_read), not blended into the generic
    # observations.read stream.
    is_admin_bypass = is_admin and org_guid not in user_orgs
    justification = (request.headers.get('X-Admin-Justification') or '').strip()
    if is_admin_bypass and not justification:
        return jsonify({
            'error': 'X-Admin-Justification header required for admin '
                     'cross-org read',
        }), 400

    # Pull all inbound observations, then filter via contract → requesting org.
    # We group by contract_guid to avoid duplicate contract.pdhc lookups.
    sr_rows = ServiceRequestStatus.query.all()
    sr_to_contract = {r.service_request_guid: r.contract_guid for r in sr_rows}

    # Resolve which contracts have requesting_org == org_guid.
    # Admin bypasses the *user-orgs* check above (so they can query any
    # org), but the requesting-org filter still applies — admin views the
    # data as that org would see it.
    matching_contracts = set()
    for contract_guid in {c for c in sr_to_contract.values() if c}:
        parties = ContractScopeService.fetch_parties(contract_guid)
        if not parties:
            continue
        if parties.get('requesting_org_guid') == org_guid:
            matching_contracts.add(contract_guid)

    matching_srs = {
        sr_guid for sr_guid, c_guid in sr_to_contract.items()
        if c_guid in matching_contracts
    }

    if not matching_srs:
        # #221 — kontroller cares "who tried", not just "who got data".
        # An empty bundle still represents a read attempt; record it.
        _audit_observation_read(
            blob, org_guid, 0,
            is_admin_bypass=is_admin_bypass,
            justification=justification or None,
            patient_guids=[],
        )
        return jsonify(_empty_bundle()), 200

    obs_rows = (
        InboundObservation.query
        .filter(InboundObservation.service_request_guid.in_(matching_srs))
        .order_by(InboundObservation.received_at.asc())
        .all()
    )

    # Spärr Phase 3 — drop rows whose provider source is blocked for
    # that patient. PDL Ch 4 § 4; ticket #206. We batch one IPS lookup
    # per unique patient_guid (cache-bounded, 30 s TTL).
    if obs_rows:
        patient_guids = {r.patient_guid for r in obs_rows if r.patient_guid}
        blocks_by_patient = fetch_blocks_for_patients(patient_guids)
        obs_rows = filter_blocked_observations(obs_rows, blocks_by_patient)

    # Pre-load sr_context for all service requests in one query
    sr_guids = {r.service_request_guid for r in obs_rows if r.service_request_guid}
    sr_contexts = {}
    if sr_guids:
        ctx_rows = (
            GuidResolutionCache.query
            .filter(GuidResolutionCache.source_type == 'sr_context',
                    GuidResolutionCache.source_guid.in_(sr_guids))
            .all()
        )
        for c in ctx_rows:
            sr_contexts[c.source_guid] = c.resolved_json or {}

    # Pre-load contract_scope for party info
    contract_guids = {r.contract_guid for r in obs_rows if r.contract_guid}
    contract_scopes = {}
    if contract_guids:
        scope_rows = (
            GuidResolutionCache.query
            .filter(GuidResolutionCache.source_type == 'contract_scope',
                    GuidResolutionCache.source_guid.in_(contract_guids))
            .all()
        )
        for c in scope_rows:
            contract_scopes[c.source_guid] = c.resolved_json or {}

    bundle = {
        'resourceType': 'Bundle',
        'type': 'searchset',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'total': len(obs_rows),
        'entry': [{'resource': _to_fhir_observation(r, sr_contexts, contract_scopes)} for r in obs_rows],
    }

    # Ticket #221 — decided per-route audit granularity. The normal
    # read keeps one row per query (cheaper, polling-friendly) but
    # carries the patient_guids list in payload_snapshot so kontroller
    # can answer "was patient P's data in any read by user X" without
    # joining the bundle content. The admin bypass path explodes to
    # per-patient rows — the bypass is rare and high-stakes, and the
    # per-patient row carries the same justification on every entry
    # so consumers can filter cheaply by patient.
    patient_guids = sorted({
        r.patient_guid for r in obs_rows if r.patient_guid
    })
    _audit_observation_read(
        blob, org_guid, len(obs_rows),
        is_admin_bypass=is_admin_bypass,
        justification=justification or None,
        patient_guids=patient_guids,
    )
    return jsonify(bundle), 200


def _to_fhir_observation(row, sr_contexts=None, contract_scopes=None):
    return build_fhir_observation(row, sr_contexts, contract_scopes)


def _empty_bundle():
    return {
        'resourceType': 'Bundle',
        'type': 'searchset',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'total': 0,
        'entry': [],
    }


def _audit_observation_read(blob, org_guid, count, *,
                            is_admin_bypass=False, justification=None,
                            patient_guids=None):
    """Persist audit row(s) for the observations read.

    Audit granularity (ticket #221):
      - ``observations.read`` (normal scope) — ONE row per query.
        Carries the full ``patient_guids`` list in the snapshot so
        kontroller can decide "was patient P in any read by user X"
        without joining the bundle content.
      - ``observations.admin_read`` (off-org bypass, #220) — ONE row
        per patient touched. Each row carries the same justification
        verbatim and the same correlation id so the bypass act is
        reconstructable as a single operator action, but per-patient
        filters work cheaply on the audit_log table directly.

    The rationale for the split: normal reads run at high volume
    (analyse phase polling); per-patient explode would inflate the
    audit table 30-200x without changing what kontroller can answer
    (the patient_guids array on the per-query row carries the same
    information). Admin bypass is rare and high-stakes; per-patient
    rows are warranted there even at higher cost.

    See gateway_technical_guide.md "Read-side audit granularity" for
    the full decision matrix.
    """
    patient_guids = list(patient_guids or [])
    correlation = request.headers.get('X-Correlation-Id')
    try:
        if is_admin_bypass:
            # Per-patient explode. If no patient guids were resolved
            # (e.g. count=0), fall back to one row with an empty list
            # so the bypass act is still recorded.
            seeds = patient_guids or [None]
            for pg in seeds:
                snapshot = {
                    'org_guid': org_guid,
                    'count': count,
                    'justification': justification,
                    'granularity': 'per-patient',
                    'patient_guid': pg,
                    'n_patients': len(patient_guids),
                }
                db.session.add(AuditLog(
                    event_type='observations.admin_read',
                    actor_guid=blob.get('user_guid'),
                    resource_guid=org_guid,
                    ip_address=request.remote_addr,
                    correlation_id=correlation,
                    payload_snapshot=snapshot,
                ))
        else:
            snapshot = {
                'org_guid': org_guid,
                'count': count,
                'granularity': 'per-query',
                'patient_guids': patient_guids,
                'n_patients': len(patient_guids),
            }
            db.session.add(AuditLog(
                event_type='observations.read',
                actor_guid=blob.get('user_guid'),
                resource_guid=org_guid,
                ip_address=request.remote_addr,
                correlation_id=correlation,
                payload_snapshot=snapshot,
            ))
        db.session.commit()
    except Exception:
        db.session.rollback()
