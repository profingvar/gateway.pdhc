"""Report ingestion service.

Handles the complete validation chain and storage of inbound
observation reports from providers.

New validation chain (defense in depth):
1. PAT already validated by @require_provider_token decorator
   → g.provider_org_guid and g.contract_guid derived from PAT
2. Grant validates via request.pdhc internal API
   → contract_guid confirmed from grant response
3. SR context fetched from request.pdhc internal API
   → patient_guid cross-checked, transaction map loaded
4. Contract scope fetched from contract.pdhc internal API
   → observation concepts validated against return_scope
5. Obligatory concepts checked on completed status
6. Observations enriched from SR context (concept_guid, unit, ranges)
7. Stored in inbound_observations with audit trail
8. Receipt pushed to provider.pdhc

Minimal required body: grant_token, status, observations[]
  (each obs: transaction_guid, value, recorded_at)

Gateway derives authoritatively from:
  - sr_guid (URL path) → sr_context.patient_guid, sr_context.contract_guid,
                         transaction_map (concept, goal_concept, ranges, units)
  - PAT (X-Provider-Token header) → provider_org_guid, scopes
  - grant_token (body) → contract_guid confirmation

Clients SHOULD NOT send concept_guid, unit, ranges — gateway overwrites
them from the SR context so a compromised provider cannot tag
observations against concepts the contract doesn't permit.

Backward compatible: patient_guid/organisation_guid/contract_guid in
body are cross-checked against the derived values if present.
"""
import logging
from datetime import datetime, timezone
from flask import g, current_app, request as flask_request
from ..models import InboundObservation, AuditLog, CdrDeliveryLog
from ..extensions import db
from ..errors import APIError
from .grant_validation import GrantValidationService
from .sr_context import SRContextService
from .contract_scope import ContractScopeService
from .observation_validator import ObservationValidator

logger = logging.getLogger(__name__)


class ReportIngestionService:

    @staticmethod
    def ingest(service_request_guid, body):
        """Process an inbound report submission.

        Args:
            service_request_guid: from URL path
            body: JSON body — minimal: patient_guid, grant_token, status, observations[]

        Returns:
            dict with receipt info
        """
        # ── Extract fields ──────────────────────────────────────────
        grant_token = body.get('grant_token')
        status = body.get('status', 'in-progress')
        report_payload = body.get('report_payload')

        # Backward compat: accept patient/org/contract from body for cross-check
        body_patient_guid = body.get('patient_guid')
        body_org_guid = body.get('organisation_guid')
        body_contract_guid = body.get('contract_guid')
        expires_at = body.get('expires_at')  # ignored — request.pdhc checks expiry

        # Derive org_guid from PAT (never from body)
        organisation_guid = g.provider_org_guid

        # ── Step 2: Cross-check body org if provided ────────────────
        if body_org_guid and body_org_guid != organisation_guid:
            _audit_rejection(service_request_guid, body_patient_guid,
                             organisation_guid, body_contract_guid,
                             'org_mismatch', body_org_guid)
            raise APIError(
                'Organisation GUID does not match authenticated provider',
                code='ORG_MISMATCH', status_code=403,
            )

        # ── Step 3: Fetch SR context FIRST, before grant validation ─
        # Client no longer needs to send patient_guid / contract_guid —
        # both come authoritatively from the SR. Body values, if any,
        # are only used as cross-checks below.
        sr_context = SRContextService.fetch(service_request_guid)
        if not sr_context.found:
            _audit_rejection(service_request_guid, body_patient_guid,
                             organisation_guid, None,
                             'SR_NOT_FOUND', sr_context.error)
            raise APIError(
                'ServiceRequest not found',
                code='SR_NOT_FOUND', status_code=404,
            )

        patient_guid = sr_context.patient_guid
        if not patient_guid:
            _audit_rejection(service_request_guid, body_patient_guid,
                             organisation_guid, None,
                             'SR_INCOMPLETE', 'SR has no patient_guid')
            raise APIError(
                'ServiceRequest is missing patient_guid',
                code='SR_INCOMPLETE', status_code=409,
            )

        # Cross-check body patient_guid if the client still sends it
        if body_patient_guid and body_patient_guid != patient_guid:
            _audit_rejection(service_request_guid, body_patient_guid,
                             organisation_guid, None,
                             'PATIENT_MISMATCH', body_patient_guid)
            raise APIError(
                'Patient GUID does not match ServiceRequest',
                code='PATIENT_MISMATCH', status_code=403,
            )

        # ── Step 4: Validate grant via request.pdhc ─────────────────
        grant_result = GrantValidationService.validate(
            service_request_guid, patient_guid,
            organisation_guid, grant_token,
        )
        if not grant_result.valid:
            _audit_rejection(service_request_guid, patient_guid,
                             organisation_guid, None,
                             grant_result.error_code, grant_result.error)
            status_code = 400 if grant_result.error_code == 'COMPOSITE_KEY_INCOMPLETE' else 403
            raise APIError(
                grant_result.error,
                code=grant_result.error_code,
                status_code=status_code,
            )

        # Derive contract_guid from grant response
        contract_guid = grant_result.contract_guid

        # Cross-check body contract_guid if provided
        if body_contract_guid and body_contract_guid != contract_guid:
            _audit_rejection(service_request_guid, patient_guid,
                             organisation_guid, contract_guid,
                             'contract_mismatch', body_contract_guid)
            raise APIError(
                'Contract GUID does not match grant',
                code='CONTRACT_MISMATCH', status_code=403,
            )

        # Build transaction lookup map
        txn_map = sr_context.transaction_map()

        # Ticket #90: late-arrival flag. Compare the request arrival time
        # against the SR's period_end. period_end may be None (open-ended
        # request) — in that case nothing is ever late.
        period_end = sr_context.period_end
        is_late = bool(period_end and datetime.now(timezone.utc) > period_end)

        # ── Step 5a: FHIR QuestionnaireResponse — store directly ────
        if isinstance(report_payload, dict) and report_payload.get('resourceType') == 'QuestionnaireResponse':
            return _store_questionnaire_response(
                service_request_guid, patient_guid, organisation_guid,
                contract_guid, grant_token, report_payload, status,
                is_late=is_late,
            )

        # ── Step 5b: Extract and enrich observations ──────────────
        observations = []
        if report_payload and isinstance(report_payload, dict):
            obs_list = report_payload.get('observations')
            if obs_list is not None:
                # Enrich from SR context BEFORE validation. The client
                # should send only {transaction_guid, value, recorded_at};
                # gateway is the authority on concept, unit, ranges — any
                # client-supplied values are overwritten so we cannot be
                # tricked into tagging an observation against a concept
                # the contract doesn't permit.
                #
                # The concept gateway stamps on the observation is the
                # transaction's **goal_concept_guid** (the thing being
                # measured — e.g. B-glucos), NOT the transaction's own
                # procedure concept (e.g. CGM). Contract return_scope is
                # expressed in measurement concepts, so the goal concept
                # is what scope validation compares against. If the
                # snapshot is missing a goal linkage (old SRs before the
                # plan.pdhc change, or multi-goal plans without explicit
                # assignment), fall back to the transaction's own
                # concept_guid so we still store something meaningful.
                # Single-transaction fallback. Providers that receive a
                # FHIR CarePlan today don't see transaction_guids (they
                # live in the SR snapshot, not the bundle), so they send
                # placeholder strings like 'tx-glucose'. When the SR has
                # exactly one transaction we can safely infer which one
                # — same pattern as the single-goal inference in
                # request.pdhc context_service. For multi-transaction
                # plans the provider MUST send the correct guid; the
                # long-term fix is to surface transaction_guids in the
                # careplan bundle (TODO).
                single_txn = (
                    next(iter(txn_map.values())) if txn_map and len(txn_map) == 1 else None
                )
                # Build concept_guid → transaction lookup for providers
                # that send concept_guid instead of transaction_guid
                # (older careplans missing transaction_guid field).
                concept_map = {
                    t['concept_guid']: t
                    for t in txn_map.values()
                    if t.get('concept_guid')
                }
                if txn_map:
                    for obs in obs_list:
                        txn_guid = obs.get('transaction_guid')
                        txn = txn_map.get(txn_guid) if txn_guid else None
                        # Fallback: provider sent concept_guid as transaction_guid
                        if txn is None and txn_guid:
                            txn = concept_map.get(txn_guid)
                            if txn:
                                obs['transaction_guid'] = txn['transaction_guid']
                        if txn is None and single_txn is not None:
                            txn = single_txn
                            obs['transaction_guid'] = txn['transaction_guid']
                        if txn is not None:
                            measurement_concept = (
                                txn.get('goal_concept_guid')
                                or txn.get('concept_guid', '')
                            )
                            measurement_name = (
                                txn.get('goal_concept_name')
                                if txn.get('goal_concept_guid')
                                else txn.get('concept_name', '')
                            ) or ''
                            obs['concept_guid'] = measurement_concept
                            obs['concept_name'] = measurement_name
                            obs['response_type'] = (
                                txn.get('response_type') or obs.get('response_type')
                            )
                            obs['unit'] = txn.get('unit') or obs.get('unit', '')
                            obs['unit_display'] = txn.get('unit_display') or obs.get('unit_display', '')
                            obs['range_min'] = txn.get('range_min')
                            obs['range_max'] = txn.get('range_max')
                            obs['requirement_type'] = txn.get('requirement_type', '')
                            # Preserve the procedure concept for audit —
                            # we stored over obs['concept_guid'] above.
                            obs['procedure_concept_guid'] = txn.get('concept_guid')

                validation = ObservationValidator.validate_observations(obs_list)
                if not validation.valid:
                    _audit_rejection(service_request_guid, patient_guid,
                                     organisation_guid, contract_guid,
                                     'VALIDATION_ERROR', validation.errors)
                    raise APIError(
                        'Observation validation failed',
                        code='VALIDATION_ERROR',
                        status_code=422,
                        details=validation.errors,
                    )
                observations = obs_list

        if not report_payload:
            _audit_rejection(service_request_guid, patient_guid,
                             organisation_guid, contract_guid,
                             'VALIDATION_ERROR', 'report_payload is required')
            raise APIError(
                'report_payload is required',
                code='VALIDATION_ERROR',
                status_code=400,
            )

        # ── Step 7: Contract scope validation ───────────────────────
        scope_result = ContractScopeService.fetch_scope(contract_guid)
        if scope_result.valid and scope_result.scope_defined and observations:
            scope_ok, scope_errors = ContractScopeService.validate_observations(
                scope_result, observations, status,
                service_request_guid=service_request_guid,
            )
            if not scope_ok:
                _audit_rejection(service_request_guid, patient_guid,
                                 organisation_guid, contract_guid,
                                 'SCOPE_VIOLATION', scope_errors)
                raise APIError(
                    'Observations violate contract scope',
                    code='SCOPE_VIOLATION',
                    status_code=403,
                    details=scope_errors,
                )
        elif not scope_result.valid and scope_result.error_code == 'CONTRACT_INACTIVE':
            _audit_rejection(service_request_guid, patient_guid,
                             organisation_guid, contract_guid,
                             'CONTRACT_INACTIVE', scope_result.error)
            raise APIError(
                scope_result.error,
                code='CONTRACT_INACTIVE',
                status_code=403,
            )
        # If scope service unavailable or no scope defined: proceed (fail-open for availability)

        # ── Step 8: Idempotency check ──────────────────────────────
        payload_hash = InboundObservation.hash_payload(report_payload)
        existing = InboundObservation.query.filter_by(
            service_request_guid=service_request_guid,
            payload_hash=payload_hash,
        ).first()
        if existing:
            return {
                'status': 'accepted',
                'receipt_guid': existing.guid,
                'service_request_guid': service_request_guid,
                'action': 'duplicate_ignored',
            }

        # ── Step 9: Store observations (per-obs idempotency, #148) ──
        # The batch fast-path above caught byte-identical re-POSTs. Here
        # we dedup at the per-observation level by
        # sha256(patient|tx|recorded_at). A batch with one new obs and
        # several previously-seen ones now stores only the new one
        # instead of duplicating the unchanged ones.
        stored = []
        ignored = []
        seen_in_batch = set()
        if observations:
            for idx, obs in enumerate(observations):
                dedup_key = InboundObservation.compute_dedup_key(
                    patient_guid,
                    obs.get('transaction_guid'),
                    obs.get('recorded_at'),
                )
                if dedup_key:
                    # Intra-batch dedup (first wins).
                    if dedup_key in seen_in_batch:
                        ignored.append({
                            'observation_index': idx,
                            'transaction_guid': obs.get('transaction_guid'),
                            'reason': 'duplicate_in_batch',
                        })
                        continue
                    seen_in_batch.add(dedup_key)
                    # Cross-batch dedup — was this (patient,tx,recorded_at)
                    # already stored on a prior submission of this SR?
                    prior = InboundObservation.query.filter_by(
                        service_request_guid=service_request_guid,
                        dedup_key=dedup_key,
                    ).first()
                    if prior:
                        ignored.append({
                            'observation_index': idx,
                            'transaction_guid': obs.get('transaction_guid'),
                            'reason': 'duplicate_prior_submission',
                            'receipt_guid': prior.guid,
                        })
                        continue
                record = InboundObservation(
                    service_request_guid=service_request_guid,
                    transaction_guid=obs.get('transaction_guid'),
                    concept_guid=obs.get('concept_guid'),
                    patient_guid=patient_guid,
                    provider_org_guid=organisation_guid,
                    contract_guid=contract_guid,
                    grant_token=grant_token,
                    fhir_observation_json=obs,
                    value=str(obs.get('value', '')),
                    response_type=obs.get('response_type'),
                    payload_hash=payload_hash,
                    dedup_key=dedup_key,
                    validation_status='valid',
                    resolution_status='resolved' if obs.get('concept_guid') else 'pending',
                    is_late=is_late,
                )
                db.session.add(record)
                db.session.flush()  # populate record.guid for FK below
                stored.append(record)
                db.session.add(CdrDeliveryLog(
                    inbound_observation_guid=record.guid,
                    patient_guid=record.patient_guid,
                    status=('pending' if record.resolution_status == 'resolved'
                            else 'skipped'),
                ))
        else:
            # Manual/freeform mode — store whole payload as single record
            record = InboundObservation(
                service_request_guid=service_request_guid,
                patient_guid=patient_guid,
                provider_org_guid=organisation_guid,
                contract_guid=contract_guid,
                grant_token=grant_token,
                fhir_observation_json=report_payload,
                payload_hash=payload_hash,
                validation_status='valid',
                resolution_status='pending',
                is_late=is_late,
            )
            db.session.add(record)
            db.session.flush()  # populate record.guid for FK below
            stored.append(record)
            # No concept_guid → not forwardable. Log as skipped.
            db.session.add(CdrDeliveryLog(
                inbound_observation_guid=record.guid,
                patient_guid=record.patient_guid,
                status='skipped',
            ))

        # ── Step 10: Audit ──────────────────────────────────────────
        audit = AuditLog(
            event_type='report.received',
            actor_guid=organisation_guid,
            data_subject_guid=patient_guid,
            resource_guid=service_request_guid,
            ip_address=flask_request.remote_addr,
            correlation_id=flask_request.headers.get('X-Correlation-Id'),
            payload_snapshot={
                'service_request_guid': service_request_guid,
                'patient_guid': patient_guid,
                'contract_guid': contract_guid,
                'observation_count': len(observations) if observations else 1,
                'observations_stored': len(stored),
                'observations_ignored': len(ignored),
                'status': status,
                'payload_hash': payload_hash,
                'is_late': is_late,
                'period_end': period_end.isoformat() if period_end else None,
            },
        )
        db.session.add(audit)
        db.session.commit()

        # Send acceptance receipt to provider (fire-and-forget)
        _send_receipt(service_request_guid, patient_guid,
                      organisation_guid, contract_guid,
                      accepted=True,
                      observations_stored=len(stored),
                      payload_hash=payload_hash,
                      is_late=is_late)

        # Track delivery for request completion (tillägg 7)
        _track_delivery(service_request_guid, patient_guid,
                         organisation_guid, contract_guid,
                         len(stored), expires_at, observations)

        # All-stored → action=created; all-ignored → duplicate_ignored
        # (matches the batch-fast-path semantics); mixed → partial. The
        # per-obs `ignored` list is always included when non-empty so
        # providers can re-derive receipt-guids for prior submissions.
        if stored and not ignored:
            action = 'created'
        elif ignored and not stored:
            action = 'duplicate_ignored'
        else:
            action = 'partial'

        resp = {
            'status': 'accepted',
            'receipt_guid': stored[0].guid if stored
                            else (ignored[0].get('receipt_guid') if ignored else None),
            'service_request_guid': service_request_guid,
            'observations_stored': len(stored),
            'is_late': is_late,
            'action': action,
        }
        if ignored:
            resp['observations_ignored'] = ignored
        return resp


def _store_questionnaire_response(sr_guid, patient_guid, org_guid,
                                  contract_guid, grant_token,
                                  report_payload, status,
                                  is_late=False):
    """Store a FHIR QuestionnaireResponse, extracting each item as a
    separate InboundObservation so downstream display and analysis can
    work with individual answers instead of one opaque blob.

    Creates N+1 records: one parent record (response_type='QuestionnaireResponse')
    holding the full QR resource, plus one child record per answered item
    (response_type set per answer type: 'numeric', 'choice', 'text', etc.).
    """
    payload_hash = InboundObservation.hash_payload(report_payload)
    existing = InboundObservation.query.filter_by(
        service_request_guid=sr_guid,
        payload_hash=payload_hash,
    ).first()
    if existing:
        return {
            'status': 'accepted',
            'receipt_guid': existing.guid,
            'service_request_guid': sr_guid,
            'action': 'duplicate_ignored',
        }

    # Parent record: full QR resource for reference
    parent = InboundObservation(
        service_request_guid=sr_guid,
        patient_guid=patient_guid,
        provider_org_guid=org_guid,
        contract_guid=contract_guid,
        grant_token=grant_token,
        fhir_observation_json=report_payload,
        response_type='QuestionnaireResponse',
        payload_hash=payload_hash,
        validation_status='valid',
        resolution_status='resolved',
        is_late=is_late,
    )
    db.session.add(parent)

    # Extract individual items as child observations
    items = report_payload.get('item', [])
    stored_items = []
    for item in items:
        answers = item.get('answer', [])
        if not answers:
            continue

        link_id = item.get('linkId', '')
        text = item.get('text', '')
        codes = item.get('code', [])
        concept_code = codes[0].get('code', '') if codes else ''
        concept_display = codes[0].get('display', '') if codes else text

        for answer in answers:
            value, response_type = _extract_qr_answer(answer)
            if value is None:
                continue

            child = InboundObservation(
                service_request_guid=sr_guid,
                transaction_guid=link_id,
                concept_guid=concept_code or link_id,
                patient_guid=patient_guid,
                provider_org_guid=org_guid,
                contract_guid=contract_guid,
                grant_token=grant_token,
                fhir_observation_json={
                    'linkId': link_id,
                    'text': text,
                    'concept_code': concept_code,
                    'concept_display': concept_display,
                    'answer': answer,
                    'questionnaire': report_payload.get('questionnaire', ''),
                    'parent_guid': parent.guid,
                },
                value=str(value),
                response_type=response_type,
                validation_status='valid',
                resolution_status='resolved',
                is_late=is_late,
            )
            db.session.add(child)
            db.session.flush()  # populate child.guid for FK below
            stored_items.append(child)
            # QR child has a concept_guid (or linkId fallback). cdr1's
            # FHIR transformer expects a real concept_guid, so mark
            # 'skipped' when concept_code is empty and the linkId was
            # used as a fallback — see services/fhir_observation_builder.
            db.session.add(CdrDeliveryLog(
                inbound_observation_guid=child.guid,
                patient_guid=child.patient_guid,
                status=('pending' if concept_code else 'skipped'),
            ))

    audit = AuditLog(
        event_type='report.received',
        actor_guid=org_guid,
        data_subject_guid=patient_guid,
        resource_guid=sr_guid,
        ip_address=flask_request.remote_addr,
        correlation_id=flask_request.headers.get('X-Correlation-Id'),
        payload_snapshot={
            'service_request_guid': sr_guid,
            'patient_guid': patient_guid,
            'contract_guid': contract_guid,
            'response_type': 'QuestionnaireResponse',
            'questionnaire': report_payload.get('questionnaire', ''),
            'items_extracted': len(stored_items),
            'status': status,
            'payload_hash': payload_hash,
            'is_late': is_late,
        },
    )
    db.session.add(audit)
    db.session.commit()

    _send_receipt(sr_guid, patient_guid, org_guid, contract_guid,
                  accepted=True, observations_stored=1 + len(stored_items),
                  payload_hash=payload_hash, is_late=is_late)

    return {
        'status': 'accepted',
        'receipt_guid': parent.guid,
        'service_request_guid': sr_guid,
        'response_type': 'QuestionnaireResponse',
        'items_extracted': len(stored_items),
        'is_late': is_late,
        'action': 'created',
    }


def _extract_qr_answer(answer):
    """Extract (value, response_type) from a FHIR QuestionnaireResponse answer.

    Returns (None, None) for unsupported answer types.
    """
    if 'valueDecimal' in answer:
        return answer['valueDecimal'], 'numeric'
    if 'valueInteger' in answer:
        return answer['valueInteger'], 'numeric'
    if 'valueString' in answer:
        return answer['valueString'], 'text'
    if 'valueBoolean' in answer:
        return answer['valueBoolean'], 'boolean'
    if 'valueDate' in answer:
        return answer['valueDate'], 'date'
    if 'valueDateTime' in answer:
        return answer['valueDateTime'], 'datetime'
    if 'valueCoding' in answer:
        coding = answer['valueCoding']
        return coding.get('display', coding.get('code', '')), 'choice'
    if 'valueQuantity' in answer:
        q = answer['valueQuantity']
        return q.get('value'), 'numeric'
    return None, None


def _audit_rejection(sr_guid, patient_guid, org_guid, contract_guid,
                     code, detail):
    """Log a rejected report submission and send rejection receipt."""
    try:
        audit = AuditLog(
            event_type='report.rejected',
            actor_guid=g.provider_org_guid if hasattr(g, 'provider_org_guid') else None,
            data_subject_guid=patient_guid,
            resource_guid=sr_guid,
            ip_address=flask_request.remote_addr,
            correlation_id=flask_request.headers.get('X-Correlation-Id'),
            payload_snapshot={'rejection_code': code, 'detail': str(detail)},
        )
        db.session.add(audit)
        db.session.commit()
    except Exception:
        db.session.rollback()

    # Send rejection receipt to provider (fire-and-forget)
    _send_receipt(sr_guid, patient_guid, org_guid, contract_guid,
                  accepted=False, rejection_code=code,
                  rejection_detail=str(detail))


def _track_delivery(sr_guid, patient_guid, org_guid, contract_guid,
                    observations_count, expires_at_iso, observations):
    """Track delivery progress for request completion (tillägg 7)."""
    try:
        from .request_completion import RequestCompletionService

        txn_guids = [o.get('transaction_guid') for o in observations
                     if o.get('transaction_guid')] if observations else None

        RequestCompletionService.track_delivery(
            sr_guid, patient_guid, org_guid, contract_guid,
            observations_count, expires_at_iso, txn_guids,
        )
    except Exception as e:
        logger.warning('Failed to track delivery: %s', e)


def _send_receipt(sr_guid, patient_guid, org_guid, contract_guid,
                  accepted=True, observations_stored=0, payload_hash=None,
                  rejection_code=None, rejection_detail=None,
                  is_late=False):
    """Send receipt to the provider that owns the request's PAT.

    Routing is per-PAT: gateway looks up `push_endpoint_url` /
    `push_secret` on `g.pat_result` (filled by @require_provider_token),
    which in turn got them from request.pdhc's /provider/validate-token.
    That means the gateway has no PROVIDER_SERVICE_URL config at all —
    different providers land on different endpoints cleanly.
    """
    try:
        from .push_service import PushService
        import uuid

        pat = getattr(g, 'pat_result', None)
        if not pat:
            logger.warning('_send_receipt called without g.pat_result — '
                           'request did not go through @require_provider_token?')
            return

        push_url = getattr(pat, 'push_endpoint_url', None)
        push_secret = getattr(pat, 'push_secret', None)
        if not push_url or not push_secret:
            logger.warning(
                'PAT for org %s has no push_endpoint_url/push_secret — '
                'receipts cannot be delivered. Fix the PAT record in request.pdhc.',
                org_guid,
            )
            return

        receipt_data = {
            'receipt_guid': str(uuid.uuid4()),
            'service_request_guid': sr_guid,
            'patient_guid': patient_guid,
            'provider_org_guid': org_guid,
            'contract_guid': contract_guid,
            'accepted': accepted,
            'accepted_at': datetime.now(timezone.utc).isoformat(),
        }
        if accepted:
            receipt_data['observations_stored'] = observations_stored
            receipt_data['payload_hash'] = payload_hash
            receipt_data['is_late'] = is_late
        else:
            receipt_data['rejection_code'] = rejection_code
            receipt_data['rejection_detail'] = rejection_detail

        PushService.send_receipt_to_provider(push_url, push_secret, receipt_data)
    except Exception as e:
        logger.warning('Failed to send receipt: %s', e)
