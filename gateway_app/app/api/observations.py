"""Observation read API for analyse-phase consumers (e.g. dashboard.pdhc).

GET /api/v1/observations?organization=<org_guid>

Phase 3 of the cdr1 SSOT cutover (ticket #282;
docs/cdr1_ssot_cutover_plan.md §7). This endpoint used to read from
gateway's own ``inbound_observations`` table; it now PROXIES to cdr1
(Option A). Gateway keeps:

  - SSO bearer validation + analyse-phase gate
  - Org-membership check (admin bypass with X-Admin-Justification)
  - Contract-scope filter (find SRs whose contract.requesting_org
    matches the requested org)
  - IPS-spärr filter on returned rows
  - Audit (observations.read / observations.admin_read)

What gateway no longer does:

  - Reading from InboundObservation
  - Assembling the FHIR R5 Observation resource — cdr1 returns
    already-assembled resources from its FhirResource table (gateway
    forwarded them there in the same shape via the cdr_forwarder).

The bundle gateway returns is the bundle cdr1 returns, post-filtered
through the spärr check. Returned shape is identical to the
pre-cutover bundle: FHIR R5 searchset Bundle of Observation resources.
"""
from datetime import datetime, timezone
import logging

from flask import request, jsonify

from . import api_bp
from ..models import ServiceRequestStatus, AuditLog
from ..extensions import db
from ..services.sso_service import validate_sso_token, has_analysis_access
from ..services.contract_scope import ContractScopeService
from ..services.cdr_client import CdrClient, CdrRejected, CdrUnavailable
from ..services.ips_client import (
    Block,
    blocked_clinic_ids,
    fetch_blocks_for_patients,
)

logger = logging.getLogger(__name__)


def _bearer_token():
    h = request.headers.get('Authorization', '')
    if not h.startswith('Bearer '):
        return None
    return h[7:].strip() or None


def _patient_guid_from_resource(resource):
    """Extract patient_guid from a FHIR Observation's subject.reference.

    Returns None if the reference is absent or unparseable.
    """
    if not resource:
        return None
    subj = (resource.get('subject') or {}).get('reference') or ''
    if not subj:
        return None
    return subj.rsplit('/', 1)[-1] or None


def _provider_org_from_resource(resource):
    for perf in (resource.get('performer') or []):
        ident = (perf.get('identifier') or {}).get('value')
        if ident:
            return ident
    return None


def _concept_guid_from_resource(resource):
    for coding in ((resource.get('code') or {}).get('coding') or []):
        if coding.get('system') == 'urn:pdhc:concept':
            return coding.get('code') or None
    return None


def _observed_iso_from_resource(resource):
    return (resource.get('effectiveDateTime')
            or resource.get('issued')
            or None)


def _resource_passes_any_lift(resource, blocks, provider):
    """Mirror ips_client._row_passes_any_lift but for FHIR dicts."""
    concept = str(_concept_guid_from_resource(resource) or '')
    observed_iso = _observed_iso_from_resource(resource)
    for b in blocks:
        if b.source_scope_id != provider or b.source_scope_type != 'clinic':
            continue
        if b.lift_kind != 'indispensable_care' or not b.lift_concept_guids:
            continue
        allowed = {str(g) for g in (b.lift_concept_guids or [])}
        if concept not in allowed:
            continue
        if b.lift_from_date and observed_iso and observed_iso < b.lift_from_date:
            continue
        if b.lift_until_date and observed_iso and observed_iso > b.lift_until_date:
            continue
        return True
    return False


def _drop_blocked_entries(entries, blocks_by_patient):
    """Filter Bundle entries by IPS spärr (#206 / PDL Ch 4 §4).

    Mirrors ``ips_client.filter_blocked_observations`` semantics
    (clinic-scope block + indispensable_care lift) but operates on
    FHIR Observation dicts rather than InboundObservation rows.
    """
    if not blocks_by_patient:
        return entries
    kept = []
    for entry in entries:
        resource = entry.get('resource') or {}
        pg = _patient_guid_from_resource(resource)
        blocks = blocks_by_patient.get(pg) or []
        if not blocks:
            kept.append(entry)
            continue
        blocked = blocked_clinic_ids(blocks)
        provider = _provider_org_from_resource(resource)
        if provider not in blocked:
            kept.append(entry)
            continue
        if _resource_passes_any_lift(resource, blocks, provider):
            kept.append(entry)
    return kept


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

    # #220 — admin cross-org read demands explicit justification.
    is_admin_bypass = is_admin and org_guid not in user_orgs
    justification = (request.headers.get('X-Admin-Justification') or '').strip()
    if is_admin_bypass and not justification:
        return jsonify({
            'error': 'X-Admin-Justification header required for admin '
                     'cross-org read',
        }), 400

    # Resolve SR → contract → requesting_org locally. Gateway is the
    # authoritative source for this mapping (contract.pdhc is its
    # upstream, not cdr1's). Only the resulting SR list is sent to cdr1.
    sr_rows = ServiceRequestStatus.query.all()
    sr_to_contract = {r.service_request_guid: r.contract_guid for r in sr_rows}

    matching_contracts = set()
    for contract_guid in {c for c in sr_to_contract.values() if c}:
        parties = ContractScopeService.fetch_parties(contract_guid)
        if not parties:
            continue
        if parties.get('requesting_org_guid') == org_guid:
            matching_contracts.add(contract_guid)

    matching_srs = sorted({
        sr_guid for sr_guid, c_guid in sr_to_contract.items()
        if c_guid in matching_contracts
    })

    correlation = request.headers.get('X-Correlation-Id') or ''

    if not matching_srs:
        # #221 — record the read attempt even with empty result.
        _audit_observation_read(
            blob, org_guid, 0,
            is_admin_bypass=is_admin_bypass,
            justification=justification or None,
            patient_guids=[],
        )
        return jsonify(_empty_bundle()), 200

    # Proxy to cdr1. cdr1 trusts gateway's auth decision; we forward
    # X-Source-Service: gateway.pdhc + X-Service-Key + the
    # pre-computed SR filter.
    try:
        bundle = CdrClient.search_observations(
            matching_srs,
            patient=None,
            request_id=correlation or 'observations.read',
        )
    except CdrRejected as e:
        logger.error("cdr1 rejected analyse-pull (%d): %s",
                     e.status_code, e.body[:200])
        return jsonify({'error': 'cdr1 rejected the request'}), 502
    except CdrUnavailable as e:
        logger.error("cdr1 unavailable for analyse-pull: %s", e)
        return jsonify({'error': 'cdr1 unavailable'}), 502

    entries = (bundle or {}).get('entry') or []

    # Spärr Phase 3 — drop rows whose provider source is blocked for
    # that patient. PDL Ch 4 § 4; ticket #206. Single IPS round-trip
    # bounded by unique patient_guids.
    patient_guids = sorted({
        _patient_guid_from_resource(e.get('resource'))
        for e in entries
        if _patient_guid_from_resource(e.get('resource'))
    })
    if entries and patient_guids:
        blocks_by_patient = fetch_blocks_for_patients(set(patient_guids))
        entries = _drop_blocked_entries(entries, blocks_by_patient)
        patient_guids = sorted({
            _patient_guid_from_resource(e.get('resource'))
            for e in entries
            if _patient_guid_from_resource(e.get('resource'))
        })

    filtered_bundle = {
        'resourceType': 'Bundle',
        'type': 'searchset',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'total': len(entries),
        'entry': entries,
    }

    _audit_observation_read(
        blob, org_guid, len(entries),
        is_admin_bypass=is_admin_bypass,
        justification=justification or None,
        patient_guids=patient_guids,
    )
    return jsonify(filtered_bundle), 200


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
