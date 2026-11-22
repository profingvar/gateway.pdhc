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

Backward compatible: patient_guid/provider_org_guid/contract_guid in
body are cross-checked against the derived values if present. The
legacy wire-format alias `organisation_guid` is still accepted as a
back-compat read; canonical `provider_org_guid` wins when both are
sent (#294 RFC plan §4 / #301).
"""
import logging
from datetime import datetime, timezone
from flask import g, current_app, request as flask_request
from ..models import AuditLog, CdrDeliveryLog
from ..extensions import db
from ..errors import APIError
from .grant_validation import GrantValidationService
from .sr_context import SRContextService
from .contract_scope import ContractScopeService
from .patient_sync import ensure_cdr_patient
from .observation_validator import ObservationValidator
from .sso_service import current_session_id
from . import data_origin as _origin
from . import review as _review

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
        # #666: the submitter MAY declare which organisation authored this
        # data, when that is not itself. Optional — every existing integration
        # keeps working unchanged and simply leaves it NULL.
        report_author_org_guid = body.get('author_org_guid')
        # #1014: OPT-IN patient-generated flag, report level (each
        # observation may also carry its own). Refused before anything is
        # fetched or stored when it is not a value cdr accepts.
        report_data_origin, _origin_err = _origin.check(body.get('data_origin'))
        if _origin_err:
            raise APIError(_origin_err, code='VALIDATION_ERROR', status_code=422)
        # A patient-client PAT forces the flag on everything it submits; the
        # body can neither omit nor change it (only one value exists).
        forced_origin = _origin.forced_by_credential(getattr(g, 'pat_result', None))
        if forced_origin:
            report_data_origin = forced_origin

        # #1043 (contract s3.2): does this report ask for care's review? Only
        # a patient-generated credential may; anyone else is refused BY NAME,
        # before anything is fetched or stored — never accepted with the
        # marker silently dropped.
        review_required, _review_err = _review.claim(body)
        if _review_err:
            raise APIError(
                '`review_required` must be the JSON boolean true or false',
                code=_review_err, status_code=422)
        if review_required and not forced_origin:
            _audit_rejection(service_request_guid, body.get('patient_guid'),
                             g.provider_org_guid, body.get('contract_guid'),
                             _review.REFUSED_NOT_PATIENT_GENERATED,
                             'review claimed by a PAT without scope patient_generated')
            raise APIError(
                'This report asks for review (it answers the EPS form, carries '
                'the eps-review tag, or sets review_required), and review '
                'exists only for patient-generated data: the access token '
                'must carry the scope `patient_generated`.',
                code=_review.REFUSED_NOT_PATIENT_GENERATED, status_code=403)
        if review_required and not _review.is_questionnaire_response(report_payload):
            raise APIError(
                'review_required applies to a FHIR QuestionnaireResponse only',
                code=_review.REFUSED_NOT_A_QUESTIONNAIRE_RESPONSE, status_code=422)

        # Backward compat: accept patient/org/contract from body for cross-check.
        # #294/#301: canonical wire key is `provider_org_guid`; legacy
        # `organisation_guid` still accepted as a back-compat read.
        body_patient_guid = body.get('patient_guid')
        body_org_guid = (body.get('provider_org_guid')
                         or body.get('organisation_guid'))
        body_contract_guid = body.get('contract_guid')
        expires_at = body.get('expires_at')  # ignored — request.pdhc checks expiry

        # Derive org_guid from PAT (never from body)
        provider_org_guid = g.provider_org_guid

        # ── Step 2: Cross-check body org if provided ────────────────
        if body_org_guid and body_org_guid != provider_org_guid:
            _audit_rejection(service_request_guid, body_patient_guid,
                             provider_org_guid, body_contract_guid,
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
                             provider_org_guid, None,
                             'SR_NOT_FOUND', sr_context.error)
            raise APIError(
                'ServiceRequest not found',
                code='SR_NOT_FOUND', status_code=404,
            )

        patient_guid = sr_context.patient_guid
        if not patient_guid:
            _audit_rejection(service_request_guid, body_patient_guid,
                             provider_org_guid, None,
                             'SR_INCOMPLETE', 'SR has no patient_guid')
            raise APIError(
                'ServiceRequest is missing patient_guid',
                code='SR_INCOMPLETE', status_code=409,
            )

        # Cross-check body patient_guid if the client still sends it
        if body_patient_guid and body_patient_guid != patient_guid:
            _audit_rejection(service_request_guid, body_patient_guid,
                             provider_org_guid, None,
                             'PATIENT_MISMATCH', body_patient_guid)
            raise APIError(
                'Patient GUID does not match ServiceRequest',
                code='PATIENT_MISMATCH', status_code=403,
            )

        # ── Step 4: Validate grant via request.pdhc ─────────────────
        grant_result = GrantValidationService.validate(
            service_request_guid, patient_guid,
            provider_org_guid, grant_token,
        )
        if not grant_result.valid:
            _audit_rejection(service_request_guid, patient_guid,
                             provider_org_guid, None,
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
                             provider_org_guid, contract_guid,
                             'contract_mismatch', body_contract_guid)
            raise APIError(
                'Contract GUID does not match grant',
                code='CONTRACT_MISMATCH', status_code=403,
            )

        # ── Ensure patient demographics in cdr1 (fail-soft, idempotent) ─
        # The delivery is now fully validated (SR + patient + grant + contract
        # all matched — the request-GUID round-trip is the trust anchor). The
        # patient REFERENCE is the SR's patient_guid; the demographic DATA came
        # from ips via the SR context. A patient ips doesn't know stays
        # pseudonymous; a write failure never affects the receipt below.
        ensure_cdr_patient(
            patient_guid, sr_context.patient_name,
            sr_context.patient_birth_date, sr_context.patient_gender,
            session_id=current_session_id(),
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
            if review_required and not sr_context.patient_org_guid:
                # The CDR scopes the review queue on the PATIENT's
                # organisation. A pending answer stored without it can be
                # decided by nobody, so it is not accepted without it.
                # Retryable: nothing has been stored.
                _audit_rejection(service_request_guid, patient_guid,
                                 provider_org_guid, contract_guid,
                                 _review.REFUSED_NO_PATIENT_ORG,
                                 'SR context carries no patient_org_guid')
                raise APIError(
                    "The patient's organisation could not be established for "
                    'this ServiceRequest, and an answer awaiting review cannot '
                    'be stored without it. Nothing was stored; retry.',
                    code=_review.REFUSED_NO_PATIENT_ORG, status_code=503)
            return _store_questionnaire_response(
                service_request_guid, patient_guid, provider_org_guid,
                contract_guid, grant_token, report_payload, status,
                is_late=is_late, data_origin=report_data_origin,
                review_required=review_required,
                patient_org_guid=sr_context.patient_org_guid or None,
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
                            # The concept's SCALE (2026-11-19), from request.pdhc.
                            obs['scale_min'] = txn.get('scale_min')
                            obs['scale_max'] = txn.get('scale_max')
                            obs['requirement_type'] = txn.get('requirement_type', '')
                            # Preserve the procedure concept for audit —
                            # we stored over obs['concept_guid'] above.
                            obs['procedure_concept_guid'] = txn.get('concept_guid')

                validation = ObservationValidator.validate_observations(obs_list)
                # #1014: a per-observation data_origin must be valid too.
                _origin_errs = [f'observations[{_i}]: {_e}'
                                for _i, _o in enumerate(obs_list)
                                if isinstance(_o, dict)
                                for _e in [_origin.check(_o.get('data_origin'))[1]]
                                if _e]
                if _origin_errs:
                    _audit_rejection(service_request_guid, patient_guid,
                                     provider_org_guid, contract_guid,
                                     'VALIDATION_ERROR', _origin_errs)
                    raise APIError(
                        'Observation validation failed',
                        code='VALIDATION_ERROR',
                        status_code=422,
                        details=_origin_errs,
                    )
                if not validation.valid:
                    _audit_rejection(service_request_guid, patient_guid,
                                     provider_org_guid, contract_guid,
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
                             provider_org_guid, contract_guid,
                             'VALIDATION_ERROR', 'report_payload is required')
            raise APIError(
                'report_payload is required',
                code='VALIDATION_ERROR',
                status_code=400,
            )

        # ── Step 6b: Concept SCALE (2026-11-19) ─────────────────────
        # A value outside its concept's scale (stress 1-7, systolic 60-250)
        # is not a measurement and is DROPPED with OUT_OF_SCALE; the rest of
        # the report is accepted, as for out-of-scope concepts. This is the
        # SCALE, never the plan's clinical target range: a systolic of 160 is
        # exactly what should be stored and flagged, and is accepted.
        rejected_out_of_scale = []
        original_index = {id(o): i for i, o in enumerate(observations)}
        if observations:
            kept = []
            for idx, obs in enumerate(observations):
                problem = _outside_scale(obs)
                if problem:
                    rejected_out_of_scale.append({
                        'observation_index': idx,
                        'code': 'OUT_OF_SCALE',
                        'concept_guid': obs.get('concept_guid'),
                        'transaction_guid': obs.get('transaction_guid'),
                        'value': obs.get('value'),
                        'scale_min': obs.get('scale_min'),
                        'scale_max': obs.get('scale_max'),
                        'message': problem,
                    })
                else:
                    kept.append(obs)
            if rejected_out_of_scale:
                _audit_rejection(service_request_guid, patient_guid,
                                 provider_org_guid, contract_guid,
                                 'OUT_OF_SCALE', rejected_out_of_scale)
                if not kept:
                    raise APIError(
                        'All observations are outside their concept scale',
                        code='OUT_OF_SCALE',
                        status_code=422,
                        details=rejected_out_of_scale,
                    )
            observations = kept

        # ── Step 7: Contract scope validation ───────────────────────
        # Partial acceptance: an observation whose (authoritative) concept
        # is outside the contract return_scope is DROPPED and reported —
        # the in-scope observations still store and forward, instead of the
        # whole batch being rejected with 403. Two things stay hard errors:
        # the obligatory-on-completed completeness check (a contract can't
        # be completed without its obligatory returns), and the degenerate
        # case where every observation is out of scope (nothing to accept).
        rejected_out_of_scope = list(rejected_out_of_scale)
        scope_result = ContractScopeService.fetch_scope(contract_guid)
        if scope_result.valid and scope_result.scope_defined and observations:
            permitted = scope_result.all_permitted_guids
            kept = []
            for idx, obs in enumerate(observations):
                concept_guid = obs.get('concept_guid')
                if concept_guid and concept_guid not in permitted:
                    rejected_out_of_scope.append({
                        'observation_index': original_index.get(id(obs), idx),
                        'concept_guid': concept_guid,
                        'transaction_guid': obs.get('transaction_guid'),
                        'message': 'Concept not in contract return scope',
                    })
                else:
                    kept.append(obs)

            if rejected_out_of_scope:
                _audit_rejection(service_request_guid, patient_guid,
                                 provider_org_guid, contract_guid,
                                 'SCOPE_VIOLATION_PARTIAL', rejected_out_of_scope)
            observations = kept

            # Obligatory-on-completed completeness — evaluated against the
            # ACCEPTED set. Only the completeness error (no per-observation
            # index) is a hard failure; per-obs scope errors were already
            # handled above by dropping.
            if status == 'completed' and scope_result.obligatory_guids:
                _ok, _errs = ContractScopeService.validate_observations(
                    scope_result, observations, status,
                    service_request_guid=service_request_guid,
                )
                oblig_errs = [e for e in _errs if 'missing_concept_guids' in e]
                if oblig_errs:
                    _audit_rejection(service_request_guid, patient_guid,
                                     provider_org_guid, contract_guid,
                                     'SCOPE_VIOLATION', oblig_errs)
                    raise APIError(
                        'Missing obligatory concepts for completed status',
                        code='SCOPE_VIOLATION',
                        status_code=403,
                        details=oblig_errs,
                    )

            # Every observation was out of scope → nothing to accept. Fail
            # clearly rather than returning an empty acceptance.
            if not observations and rejected_out_of_scope:
                raise APIError(
                    'All observations are outside the contract return scope',
                    code='SCOPE_VIOLATION',
                    status_code=403,
                    details=rejected_out_of_scope,
                )
        elif not scope_result.valid and scope_result.error_code == 'CONTRACT_INACTIVE':
            _audit_rejection(service_request_guid, patient_guid,
                             provider_org_guid, contract_guid,
                             'CONTRACT_INACTIVE', scope_result.error)
            raise APIError(
                scope_result.error,
                code='CONTRACT_INACTIVE',
                status_code=403,
            )
        # If scope service unavailable or no scope defined: proceed (fail-open for availability)

        # ── Step 8: Idempotency check ──────────────────────────────
        # Dedup now reads CdrDeliveryLog (phase 1 of the SSOT cutover;
        # docs/cdr1_ssot_cutover_plan.md §5). The log row outlives the
        # source InboundObservation row in later phases. receipt_guid
        # prefers the (still-existing) inbound guid for backwards
        # compatibility; falls back to the log guid once phase 5
        # nulls the FK.
        payload_hash = CdrDeliveryLog.hash_payload(report_payload)
        existing = CdrDeliveryLog.query.filter_by(
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
                dedup_key = CdrDeliveryLog.compute_dedup_key(
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
                    # Queries CdrDeliveryLog (phase 1 SSOT cutover).
                    prior = CdrDeliveryLog.query.filter_by(
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
                # #296: CdrDeliveryLog written directly. No
                # InboundObservation row. cdr_delivery_log is the sole
                # queue + dedup index + provenance source for the
                # forwarder.
                record = CdrDeliveryLog(
                    patient_guid=patient_guid,
                    service_request_guid=service_request_guid,
                    transaction_guid=obs.get('transaction_guid'),
                    concept_guid=obs.get('concept_guid'),
                    contract_guid=contract_guid,
                    provider_org_guid=provider_org_guid,
                    # #666: declared per observation, else per report, else
                    # NULL. Deliberately NOT defaulted to the provider: a
                    # guess written into the row becomes indistinguishable
                    # from a value the submitter actually declared.
                    author_org_guid=(obs.get('author_org_guid')
                                     or report_author_org_guid),
                    # #1014: per observation, else per report, else NULL
                    # (= healthcare). Validated at Step 5b.
                    data_origin=(forced_origin
                                 or _origin.check(obs.get('data_origin'))[0]
                                 or report_data_origin),
                    payload_hash=payload_hash,
                    dedup_key=dedup_key,
                    received_at=datetime.now(timezone.utc),
                    fhir_observation_json=obs,
                    operator_session_id=current_session_id(),  # X2 #408
                    status=('pending' if obs.get('concept_guid') else 'skipped'),
                )
                db.session.add(record)
                db.session.flush()
                stored.append(record)
        else:
            # Manual/freeform mode — #296: write CdrDeliveryLog
            # directly, status='skipped' (no concept_guid → not
            # forwardable). Still carries payload_hash so re-POST
            # dedup works.
            record = CdrDeliveryLog(
                patient_guid=patient_guid,
                service_request_guid=service_request_guid,
                contract_guid=contract_guid,
                provider_org_guid=provider_org_guid,
                author_org_guid=report_author_org_guid,   # #666
                data_origin=report_data_origin,           # #1014
                payload_hash=payload_hash,
                received_at=datetime.now(timezone.utc),
                fhir_observation_json=report_payload,
                operator_session_id=current_session_id(),  # X2 #408
                status='skipped',
            )
            db.session.add(record)
            db.session.flush()
            stored.append(record)

        # ── Step 10: Audit ──────────────────────────────────────────
        audit = AuditLog(
            event_type='report.received',
            actor_guid=provider_org_guid,
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
                'observations_rejected': len(rejected_out_of_scope),
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
                      provider_org_guid, contract_guid,
                      accepted=True,
                      observations_stored=len(stored),
                      payload_hash=payload_hash,
                      is_late=is_late)

        # Track delivery for request completion (tillägg 7).
        # `status` carries the provider's explicit lifecycle declaration
        # (e.g. 'completed') so the SR can be auto-closed on request.pdhc.
        _track_delivery(service_request_guid, patient_guid,
                         provider_org_guid, contract_guid,
                         len(stored), expires_at, observations, status)

        # All-stored → action=created; all-ignored → duplicate_ignored
        # (matches the batch-fast-path semantics); mixed → partial. The
        # per-obs `ignored` list is always included when non-empty so
        # providers can re-derive receipt-guids for prior submissions.
        if rejected_out_of_scope:
            # Some observations were dropped as out-of-scope → the batch is
            # partial regardless of the stored/ignored split.
            action = 'partial'
        elif stored and not ignored:
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
        if rejected_out_of_scope:
            resp['observations_rejected'] = rejected_out_of_scope
        return resp


def _outside_scale(obs):
    """A reason string when a numeric value lies outside its concept's scale,
    else None. No scale, or a non-numeric value, is never refused here."""
    lo, hi = obs.get('scale_min'), obs.get('scale_max')
    if lo is None and hi is None:
        return None
    try:
        v = float(obs.get('value'))
    except (TypeError, ValueError):
        return None
    if lo is not None and v < float(lo):
        return f'Value {obs.get("value")} is below the concept scale minimum {lo}'
    if hi is not None and v > float(hi):
        return f'Value {obs.get("value")} is above the concept scale maximum {hi}'
    return None


def _store_questionnaire_response(sr_guid, patient_guid, org_guid,
                                  contract_guid, grant_token,
                                  report_payload, status,
                                  is_late=False, data_origin=None,
                                  review_required=False, patient_org_guid=None):
    """Store a FHIR QuestionnaireResponse, extracting each item as a
    separate InboundObservation so downstream display and analysis can
    work with individual answers instead of one opaque blob.

    Creates N+1 records: one parent receipt record
    holding the full QR resource, plus one child record per answered item
    (response_type set per answer type — CANONICAL values only since #871; see
    app/services/response_types.py, which owns the vocabulary).

    #1043 — NESTED items are walked (`_walk_qr_items`): a group's fields are
    extracted with their own linkId, and each instance of a group gets a
    `group` id shared by its fields. With `review_required` (the patient EPS
    form, contract s3.2) every child is forwardable keyed by its linkId even
    without an item `code`, and carries the marker, the linkId, the group and
    the patient's organisation in its own JSON — the forwarder builds the CDR
    context from the row alone.

    NOTE the parent is `status='skipped'` and its fhir_observation_json is the
    raw report payload: it carries no response_type and never reaches the
    builder. This docstring used to say the parent is
    `response_type='QuestionnaireResponse'`, which was never true of the row —
    that string appears only in the audit payload and the HTTP response.
    """
    # QR endpoint dedup — same as Step 8 in the main path, queries
    # CdrDeliveryLog (phase 1 SSOT cutover).
    payload_hash = CdrDeliveryLog.hash_payload(report_payload)
    existing = CdrDeliveryLog.query.filter_by(
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

    # #296: parent QR record as a CdrDeliveryLog (status='skipped' —
    # the whole QR resource is too coarse to forward directly; the
    # individual items become forwardable children).
    parent = CdrDeliveryLog(
        patient_guid=patient_guid,
        service_request_guid=sr_guid,
        contract_guid=contract_guid,
        provider_org_guid=org_guid,
        data_origin=data_origin,                  # #1014
        payload_hash=payload_hash,
        received_at=datetime.now(timezone.utc),
        fhir_observation_json=report_payload,
        operator_session_id=current_session_id(),  # X2 #408
        status='skipped',
    )
    db.session.add(parent)
    db.session.flush()  # populate parent.guid for child back-references

    # Extract individual items as child observations — nested ones too.
    leaves = list(_walk_qr_items(report_payload.get('item'), parent.guid))
    if review_required:
        # A review answer is keyed by its linkId in String(36) columns, here
        # and in the CDR. Refused by name rather than truncated or 500'd.
        bad = sorted({str(i.get('linkId') or '') for i, _ in leaves
                      if not i.get('linkId')
                      or len(str(i['linkId'])) > _review.MAX_LINK_ID})
        if bad:
            db.session.rollback()
            raise APIError(
                f'Every answered item needs a linkId of at most '
                f'{_review.MAX_LINK_ID} characters when the report is for review',
                code=_review.REFUSED_LINK_ID, status_code=422,
                details=[{'linkId': b} for b in bad])
    stored_items = []
    for item, group in leaves:
        answers = item.get('answer') or []

        link_id = item.get('linkId', '')
        text = item.get('text', '')
        codes = item.get('code', [])
        concept_code = codes[0].get('code', '') if codes else ''
        concept_display = codes[0].get('display', '') if codes else text

        for answer in answers:
            value, response_type = _extract_qr_answer(
                answer, prefer_code=review_required)
            if value is None:
                continue

            # #296: each child is a CdrDeliveryLog. parent_guid is now
            # the parent log's guid (was InboundObservation.guid).
            # status='pending' when concept_code is present (real
            # concept), 'skipped' when only linkId fallback (cdr1's
            # transformer needs a real concept_guid).
            child = CdrDeliveryLog(
                patient_guid=patient_guid,
                service_request_guid=sr_guid,
                transaction_guid=link_id,
                concept_guid=concept_code or link_id,
                contract_guid=contract_guid,
                provider_org_guid=org_guid,
                data_origin=data_origin,          # #1014: a PROM answer is PGD
                received_at=datetime.now(timezone.utc),
                fhir_observation_json=_qr_child_json(
                    link_id, text, concept_code, concept_display, answer,
                    report_payload.get('questionnaire', ''), parent.guid,
                    value, response_type, group=group,
                    review_required=review_required,
                    patient_org_guid=patient_org_guid),
                operator_session_id=current_session_id(),  # X2 #408
                # Without an item `code` there is no concept, and the row is
                # stored but not forwarded — EXCEPT a review answer (#1043):
                # the EPS form's items carry no code by design ("the form
                # codes nothing"), and an answer that never reaches the CDR
                # can never be reviewed. It goes keyed by its linkId.
                status=('pending' if (concept_code or review_required)
                        else 'skipped'),
            )
            db.session.add(child)
            db.session.flush()
            stored_items.append(child)

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
            'review_required': bool(review_required),
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
        # #1043: said back to the submitter, so "stored for review" is never
        # an assumption on its side.
        **({'review_required': True} if review_required else {}),
    }


def _walk_qr_items(items, receipt_guid):
    """Yield ``(item, group)`` for every ANSWERED item of a
    QuestionnaireResponse, at any depth (#1043).

    Until #1043 only top-level items were read, so the fields of a group —
    every field of the patient EPS form's repeating `<section>.entry` — were
    dropped without a word.

    ``group`` identifies the INSTANCE of the nearest enclosing group: the
    same for all fields of one entry, different between entries, and unique
    to this report (``<receipt guid>/<n>``, n counting group instances in
    document order) so two submissions never share one. ``None`` for an item
    outside any group. Items nested under an ANSWER are walked too and keep
    the group they are in.
    """
    counter = [0]

    def walk(nodes, group):
        for item in nodes or []:
            if not isinstance(item, dict):
                continue
            answers = [a for a in (item.get('answer') or []) if isinstance(a, dict)]
            if answers:
                yield item, group
                for a in answers:
                    yield from walk(a.get('item'), group)
            children = item.get('item')
            if children:
                inner = group
                if not answers:          # a group: one instance, one id
                    counter[0] += 1
                    inner = f'{receipt_guid}/{counter[0]}'
                yield from walk(children, inner)

    yield from walk(items, None)


def _qr_child_json(link_id, text, concept_code, concept_display, answer,
                   questionnaire, parent_guid, value, response_type, *,
                   group=None, review_required=False, patient_org_guid=None):
    """The JSON stored on one extracted answer's delivery row.

    The first nine keys are the pre-#1043 shape. The forwarder reads the rest
    from HERE and nowhere else — the row is the only thing that crosses the
    queue between this request and the delivery to the CDR.
    """
    doc = {
        'linkId': link_id,
        'text': text,
        'concept_code': concept_code,
        'concept_display': concept_display,
        'answer': answer,
        'questionnaire': questionnaire,
        'parent_guid': parent_guid,
        'value': value,
        'response_type': response_type,
    }
    if group:
        doc['group'] = group
    if review_required:
        doc['review_required'] = True
        # The label a reviewer reads: the CDR takes it from
        # Observation.code.text, which the builder fills from concept_name.
        doc['concept_name'] = concept_display or text or link_id
        # Captured now, while request.pdhc has just answered, so a delivery
        # retried when it cannot be asked still carries it.
        doc['patient_org_guid'] = patient_org_guid
    return doc


def _extract_qr_answer(answer, prefer_code=False):
    """Extract (value, response_type) from a FHIR QuestionnaireResponse answer.

    Returns (None, None) for unsupported answer types.

    ``prefer_code`` (#1043, review answers): a coded answer is stored as its
    CODE rather than its display. eps reads `has` / `none` / `unknown` and
    `mild` / `severe` from the value; a translated display would not be
    recognised there.
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
        # #871: `dateTime`, not `date`. FHIR's dateTime admits a date-only
        # value (`2026-03-14` is a valid dateTime), so nothing is invented by
        # widening it, and the builder has no `date` branch to land in.
        return answer['valueDate'], 'dateTime'
    if 'valueDateTime' in answer:
        # #871: was `datetime` — a CASE difference from the only spelling the
        # builder and validator know. It fell through both.
        return answer['valueDateTime'], 'dateTime'
    if 'valueCoding' in answer:
        # #871: was `choice`. The builder checks `categorical`, so a coded
        # answer reached its blind-float() branch and a Likert "5" or a coded
        # "100" was written to the clinical store as a valueQuantity — a
        # NUMBER where a code belonged.
        coding = answer['valueCoding']
        if prefer_code and coding.get('code'):
            return str(coding['code']), 'categorical'
        return coding.get('display', coding.get('code', '')), 'categorical'
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
                    observations_count, expires_at_iso, observations,
                    report_status=None):
    """Track delivery progress for request completion (tillägg 7).

    `report_status` is the provider's explicit lifecycle status on the
    report ('completed' closes the SR and propagates to request.pdhc).
    """
    try:
        from .request_completion import RequestCompletionService

        txn_guids = [o.get('transaction_guid') for o in observations
                     if o.get('transaction_guid')] if observations else None

        RequestCompletionService.track_delivery(
            sr_guid, patient_guid, org_guid, contract_guid,
            observations_count, expires_at_iso, txn_guids,
            report_status=report_status,
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

        # A poll-mode provider has no push endpoint by design: it reads its
        # results from the feed. That is not a broken PAT, so do not say so
        # (the warning fired on every cgm_provider report, 2026-11-18).
        if getattr(pat, 'delivery_mode', None) == 'poll':
            logger.debug('PAT for org %s is poll-mode; no receipt push', org_guid)
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
