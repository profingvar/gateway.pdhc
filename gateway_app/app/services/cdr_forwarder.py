"""Background worker — forwards CdrDeliveryLog rows to cdr.pdhc.

Mirror of cdr.pdhc/cdr_app/app/services/cambio_worker.py (which sends
the same shape outward to real Cambio). Single delivery type (FHIR),
no openEHR branch, otherwise structurally identical.

Concurrency note: two gunicorn workers will each start a scheduler.
SELECT ... FOR UPDATE SKIP LOCKED ensures only one of them claims any
given row per cycle. Cambio's worker does not bother because in cdr1
only one container exists; gateway runs two workers so we are explicit.

Retry policy:
- attempt 1 immediately
- attempt N waits BASE_BACKOFF * 2 ** (N-1) seconds since last_attempt_at
- after MAX_ATTEMPTS, row is marked 'failed' and stops retrying.
- terminal 4xx from cdr1 (semantic rejection) also marks 'failed'
  without burning the retry budget.
"""
import logging
from datetime import datetime, timezone
from flask import current_app
from sqlalchemy import text
from ..extensions import db
from ..models import CdrDeliveryLog, AuditLog
from .cdr_client import CdrClient, CdrRejected, CdrUnavailable
from .fhir_observation_builder import build_fhir_observation, build_canonical_observation
from .response_types import UnknownResponseType
from .sr_context import SRContextService
from . import data_origin as _origin

logger = logging.getLogger(__name__)

class ReviewContextIncomplete(Exception):
    """A review answer (#1043) cannot be forwarded as it stands.

    ``terminal`` False: the patient's organisation is not known right now
    (request.pdhc could not be asked and the row carries none). The CDR
    scopes the review queue on it, so a pending answer forwarded without it
    could be decided by nobody — the delivery is retried instead.

    ``terminal`` True: the row is marked for review but is not
    patient-generated. That cannot be delivered truthfully either way.
    """

    def __init__(self, message, terminal=False):
        super().__init__(message)
        self.terminal = terminal


MAX_ATTEMPTS = 5
BASE_BACKOFF = 10  # seconds
BATCH_LIMIT = 50


def run_forwarding_cycle(app):
    """Process one cycle of pending deliveries. Called from APScheduler."""
    with app.app_context():
        if not app.config.get('CDR_FORWARDING_ENABLED'):
            return 0

        # Claim rows: status=pending, ordered by created_at, FOR UPDATE
        # SKIP LOCKED on Postgres so two gunicorn workers can run
        # schedulers concurrently without double-processing.
        # SQLite (test suite) doesn't support SKIP LOCKED; fall back to
        # plain SELECT which is safe because tests run single-process.
        dialect = db.engine.dialect.name
        if dialect == 'postgresql':
            claim_sql = text(
                """
                SELECT guid FROM cdr_delivery_log
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT :lim
                FOR UPDATE SKIP LOCKED
                """
            )
        else:
            claim_sql = text(
                """
                SELECT guid FROM cdr_delivery_log
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT :lim
                """
            )
        try:
            claimed_guids = [
                row[0] for row in db.session.execute(
                    claim_sql, {'lim': BATCH_LIMIT}).fetchall()
            ]
        except Exception as e:
            db.session.rollback()
            logger.error("Forwarding cycle: claim failed: %s", e)
            return 0

        if not claimed_guids:
            db.session.commit()  # release the transaction
            return 0

        rows = (
            CdrDeliveryLog.query
            .filter(CdrDeliveryLog.guid.in_(claimed_guids))
            .all()
        )

        delivered = 0
        for log in rows:
            try:
                if _deliver_one(log):
                    delivered += 1
            except UnknownResponseType as e:
                # #871 + #833: a response_type that was DECLARED and names
                # nothing is a PERMANENT condition. Routing it to the generic
                # handler below would retry it MAX_ATTEMPTS times with
                # exponential backoff and then fail anyway — telling an
                # operator "transient" about a configuration error, which is
                # the exact mistake xlate's client made (#833).
                #
                # Terminal is also the safe direction: the alternative to
                # refusing is writing a wrong-typed clinical value, and before
                # #871 that is what happened — an unrecognised type reached the
                # builder's blind-float() branch.
                logger.error("Forwarding %s refused: %s", log.guid, e)
                _mark_failed_terminal(log, str(e))
                db.session.add(AuditLog(
                    event_type='cdr.delivery.failure',
                    actor_guid='gateway.pdhc',
                    data_subject_guid=log.patient_guid,
                    resource_guid=log.guid,
                    payload_snapshot={
                        'terminal': True,
                        'reason': 'unknown_response_type',
                        'response_type': e.raw,
                        'attempt_count': log.attempt_count,
                    },
                ))
            except ReviewContextIncomplete as e:
                # #1043: named, not a traceback. Retryable keeps the normal
                # retry budget; the row is never delivered without the field.
                logger.error("Forwarding %s held: %s", log.guid, e)
                if e.terminal:
                    _mark_failed_terminal(log, str(e))
                else:
                    _mark_retryable(log, str(e))
            except Exception as e:
                logger.exception("Forwarding %s failed: %s", log.guid, e)
                _mark_retryable(log, str(e))

        db.session.commit()
        if delivered or rows:
            logger.info(
                "Forwarding cycle: %d delivered / %d claimed", delivered, len(rows))
        return delivered


def _deliver_one(log):
    """Attempt one delivery. Returns True on success."""
    now = datetime.now(timezone.utc)

    # Per-row backoff check
    if log.attempt_count > 0 and log.last_attempt_at:
        backoff = BASE_BACKOFF * (2 ** (log.attempt_count - 1))
        elapsed = (now - log.last_attempt_at).total_seconds()
        if elapsed < backoff:
            return False  # too soon to retry

    log.attempt_count += 1
    log.last_attempt_at = now

    # #285/#296/#297: payload is built from the log row itself; the
    # InboundObservation lookup is gone. fhir_observation_json is
    # populated on the log row by report_ingestion (since #296).
    if not log.fhir_observation_json:
        _mark_failed_terminal(log, "Log row has no fhir_observation_json")
        return False

    # #294 RFC F1 (2026-06-28): production-path guard. The canonical
    # clinical-context schema requires service_request_guid,
    # concept_guid, contract_guid, provider_org_guid on every
    # `pending` row. report_ingestion sets status='skipped' (not
    # 'pending') when concept_code didn't resolve or the row is a QR
    # child — so reaching this point with any of these NULL means an
    # incomplete production payload slipped through. Mark terminal
    # rather than ship dirty data downstream.
    #
    # #1049 (contract s7.3): a patient-card row has, BY DESIGN, no
    # ServiceRequest, no contract and no provider organisation (no PAT). Its
    # required set is what stands in for them, all written at ingest: the
    # linkId as concept, the section, the version, and the care unit eps
    # confirmed. Recognised by the row's own marker, never by the NULLs —
    # a broken order-path row must still fail here as before.
    if is_patient_card_row(log):
        raw_card = log.fhir_observation_json
        checks = (
            ('concept_guid',     log.concept_guid),
            ('card_section',     raw_card.get('card_section')),
            ('card_version',     raw_card.get('card_version')),
            ('patient_org_guid', raw_card.get('patient_org_guid')),
        )
    else:
        checks = (
            ('service_request_guid', log.service_request_guid),
            ('concept_guid',         log.concept_guid),
            ('contract_guid',        log.contract_guid),
            ('provider_org_guid',    log.provider_org_guid),
        )
    required_missing = [name for name, value in checks if not value]
    if required_missing:
        _mark_failed_terminal(
            log,
            f"Production-path canonical fields NULL: {', '.join(required_missing)}",
        )
        return False

    payload = _build_payload(log)

    db.session.add(AuditLog(
        event_type='cdr.delivery.attempt',
        actor_guid='gateway.pdhc',
        data_subject_guid=log.patient_guid,
        resource_guid=log.guid,
        payload_snapshot={'attempt_count': log.attempt_count},
    ))

    try:
        # X2 (#408): replay the operator session captured on the row at ingest.
        body = CdrClient.deliver_one(
            payload, request_id=log.guid,
            session_id=log.operator_session_id)
    except CdrRejected as e:
        # 4xx from cdr1 — terminal, do not retry
        _mark_failed_terminal(log, f"cdr1 {e.status_code}: {e.body[:200]}")
        db.session.add(AuditLog(
            event_type='cdr.delivery.failure',
            actor_guid='gateway.pdhc',
            data_subject_guid=log.patient_guid,
            resource_guid=log.guid,
            payload_snapshot={
                'terminal': True,
                'status_code': e.status_code,
                'attempt_count': log.attempt_count,
            },
        ))
        return False
    except CdrUnavailable as e:
        # 5xx / network — retryable
        _mark_retryable(log, str(e))
        return False

    # Success
    log.status = 'delivered'
    log.delivered_at = now
    log.last_error = None
    # Echo back any resource id cdr1 gave us, if present
    if isinstance(body, dict):
        log.cdr_resource_id = (body.get('ingest_raw_guid') or
                               body.get('guid') or
                               body.get('resource_id'))

    db.session.add(AuditLog(
        event_type='cdr.delivery.success',
        actor_guid='gateway.pdhc',
        data_subject_guid=log.patient_guid,
        resource_guid=log.guid,
        payload_snapshot={
            'attempt_count': log.attempt_count,
            'cdr_resource_id': log.cdr_resource_id,
        },
    ))

    # #297: nothing to delete. The InboundObservation row never existed
    # (since #296 the writer skipped it). The CDR_FORWARDING_DELETE_AFTER_DELIVERY
    # config is now dead code; left in place + ignored. Will be removed
    # together with the InboundObservation model in #299.
    return True


def is_patient_card_row(log_row) -> bool:
    """#1049: a row written by POST /api/v1/patient-card. The marker is the
    one ``app/services/patient_card.py`` writes; a row is a card row only
    when it also has no ServiceRequest, so an order-path row can never be
    re-classified by what its JSON happens to contain."""
    raw = getattr(log_row, 'fhir_observation_json', None)
    return (isinstance(raw, dict) and raw.get('performer') == 'patient'
            and bool(raw.get('card_version'))
            and not getattr(log_row, 'service_request_guid', None))


def _build_payload(log_row):
    """Map a CdrDeliveryLog row to cdr1's ingest payload shape.

    Since #296/#297 the log row holds everything build_fhir_observation
    needs (fhir_observation_json, patient_guid, service_request_guid,
    transaction_guid, contract_guid, provider_org_guid, received_at,
    concept_guid). No InboundObservation lookup required.
    """
    # #490: restore provenance. The SR context (plan_definition_guid, goals,
    # requester, requesting_org) is fetched from request.pdhc and handed to the
    # builder; previously this was None so ALL of it was dropped from CDR1.
    # A context-fetch failure must NOT block delivery — forward without it.
    sr_contexts, contract_scopes = {}, {}
    # #776: the three fields below were ALREADY being fetched here and then
    # dropped before the clinical_context block was built, so cdr1 recorded
    # plan_definition_guid and requesting_org_guid on 0 of 7,416 rows while the
    # values sat in this very response. cdr's ClinicalContext has mapped all
    # three since #302 — it was waiting for a sender.
    sr_plan_definition_guid = None
    sr_requesting_org_guid = None
    sr_requester_user_guid = None
    sr_care_plan_guid = None
    sr_patient_org_guid = None
    sr_guid = log_row.service_request_guid
    if sr_guid:
        try:
            ctx = SRContextService.fetch(sr_guid)
            if ctx.found:
                sr_contexts = {sr_guid: ctx.as_builder_context()}
                sr_plan_definition_guid = ctx.plan_definition_guid or None
                sr_requesting_org_guid = ctx.requesting_org_guid or None
                sr_requester_user_guid = ctx.requester_user_guid or None
                sr_care_plan_guid = ctx.care_plan_guid or None
                sr_patient_org_guid = ctx.patient_org_guid or None
                if log_row.contract_guid and ctx.requesting_org_guid:
                    contract_scopes = {log_row.contract_guid: {
                        'parties': {'requesting_org_guid': ctx.requesting_org_guid}}}
        except Exception as e:  # noqa: BLE001 — provenance is best-effort
            logger.warning("SR context fetch failed for %s (%s); forwarding "
                           "without provenance", sr_guid, e)

    # #1043 (contract s3.2): the review marker, the linkId and the
    # repeating-group instance travel on the row's own JSON, written at
    # ingest. Read here explicitly, because this function is the ONLY thing
    # that turns a queued row into the CDR's context: a key it does not copy
    # does not arrive (the #776 lesson, three fields fetched and dropped).
    # getattr: this drains a queue and is also handed test doubles (see the
    # author_org_guid note below).
    raw = getattr(log_row, 'fhir_observation_json', None)
    raw = raw if isinstance(raw, dict) else {}
    review_required = raw.get('review_required') is True
    if review_required:
        if getattr(log_row, 'data_origin', None) != _origin.PATIENT_GENERATED:
            raise ReviewContextIncomplete(
                'review_required_without_patient_generated: the row is marked '
                'for review but is not patient-generated', terminal=True)
        # request.pdhc's live answer first; else what ingest captured.
        sr_patient_org_guid = sr_patient_org_guid or raw.get('patient_org_guid') or None
        if not sr_patient_org_guid:
            raise ReviewContextIncomplete(
                'review_patient_org_unavailable: an answer awaiting review is '
                "not forwarded without the patient's organisation "
                '(request.pdhc could not be asked); will retry')

    fhir_obs = build_fhir_observation(log_row, sr_contexts=sr_contexts,
                                      contract_scopes=contract_scopes)
    review_context = {}
    if review_required:
        # JSON true, and only ever present when true: the CDR refuses
        # anything else by name, and every other row's context is unchanged.
        review_context['review_required'] = True
    if raw.get('linkId'):
        # The linkId with its section prefix. It is ALSO transaction_guid
        # below (where the CDR reads it); named here so no reader has to
        # know that a QuestionnaireResponse answer's transaction is its
        # linkId.
        review_context['link_id'] = raw['linkId']
    if raw.get('group'):
        review_context['group'] = str(raw['group'])
    if is_patient_card_row(log_row):
        # #1049 (contract s7.3/s7.4): which card section and which version of
        # it this answer belongs to — the CDR supersedes older pending
        # versions on it — and who submitted it. `source_service` here is
        # what the CDR records on clinical_context; the HTTP caller is still
        # gateway.pdhc with its own key.
        review_context['card_section'] = raw['card_section']
        review_context['card_version'] = raw['card_version']
        review_context['source_service'] = raw.get('source_service') or 'patientapp.pdhc'
    return {
        'patient_guid': log_row.patient_guid,
        'source_type': 'fhir',
        'source_system_id': log_row.guid,
        # #500: canonical typed observation — the source of both projections.
        # Additive + non-breaking; existing readers keep using fhir_resource.
        'observation': build_canonical_observation(log_row),
        'fhir_resource': fhir_obs,
        'clinical_context': {
            'service_request_guid': log_row.service_request_guid,
            'transaction_guid': log_row.transaction_guid,
            'contract_guid': log_row.contract_guid,
            'provider_org_guid': log_row.provider_org_guid,
            # #666: the authoring organisation, accepted by cdr #665. NULL
            # when the submitter did not declare one — cdr stores NULL rather
            # than defaulting, so a guess never becomes indistinguishable
            # from a declared fact.
            # getattr, not attribute access: the forwarder drains a QUEUE, so
            # it can legitimately meet a row enqueued before the column
            # existed — during a rolling deploy, or a backlog. Matching the
            # builder, which is defensive for the same reason.
            'author_org_guid': getattr(log_row, 'author_org_guid', None),
            # #1014: 'patient_generated' or None (= healthcare). getattr for
            # rows queued before the column existed, as above.
            'data_origin': getattr(log_row, 'data_origin', None),
            # #776. Fetched above from request.pdhc, not stored on the delivery
            # log — `cdr_delivery_log` has no plan_definition_guid column — so a
            # delivery retried after its ServiceRequest is archived legitimately
            # carries None here. cdr stores NULL rather than defaulting, the
            # same rule #665 applied to author_org_guid: an absent fact must not
            # become indistinguishable from a declared one.
            'plan_definition_guid': sr_plan_definition_guid,
            'requesting_org_guid': sr_requesting_org_guid,
            'requester_user_guid': sr_requester_user_guid,
            # 2026-11-20: the CarePlan the ServiceRequest is based on, from
            # the same request.pdhc context. cdr's ClinicalContext and
            # observation.care_plan_guid had no sender: 0 of 34 rows. None
            # when request.pdhc names none (or predates the field).
            'care_plan_guid': sr_care_plan_guid,
            # 2026-11-20: the patient's organisation from the same context.
            # cdr stores it as patient_org_guid, the column every org-scoped
            # read (Rule 24, the external read door) filters on; without it
            # gateway's rows reached no org-bound reader. None when request.pdhc
            # names none, never a guess.
            'patient_org_guid': sr_patient_org_guid,
            **review_context,
        },
    }


def _mark_retryable(log, error_msg):
    """Increment retry counter; mark 'failed' if budget spent."""
    log.last_error = error_msg[:500]
    if log.attempt_count >= MAX_ATTEMPTS:
        log.status = 'failed'
        logger.warning("Forwarding %s permanently failed after %d attempts: %s",
                       log.guid, log.attempt_count, error_msg)
    else:
        log.status = 'pending'  # Will be retried on next cycle
        logger.info("Forwarding %s attempt %d failed, will retry: %s",
                    log.guid, log.attempt_count, error_msg)


def _mark_failed_terminal(log, error_msg):
    """Mark 'failed' immediately without burning further retries."""
    log.last_error = error_msg[:500]
    log.status = 'failed'
    logger.warning("Forwarding %s terminally rejected: %s",
                   log.guid, error_msg)
