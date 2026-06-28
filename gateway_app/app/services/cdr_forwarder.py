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
from ..models import CdrDeliveryLog, InboundObservation, AuditLog
from .cdr_client import CdrClient, CdrRejected, CdrUnavailable
from .fhir_observation_builder import build_fhir_observation

logger = logging.getLogger(__name__)

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

    payload = _build_payload(log)

    db.session.add(AuditLog(
        event_type='cdr.delivery.attempt',
        actor_guid='gateway.pdhc',
        data_subject_guid=log.patient_guid,
        resource_guid=log.guid,
        payload_snapshot={'attempt_count': log.attempt_count},
    ))

    try:
        body = CdrClient.deliver_one(payload, request_id=log.guid)
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


def _build_payload(log_row):
    """Map a CdrDeliveryLog row to cdr1's ingest payload shape.

    Since #296/#297 the log row holds everything build_fhir_observation
    needs (fhir_observation_json, patient_guid, service_request_guid,
    transaction_guid, contract_guid, provider_org_guid, received_at,
    concept_guid). No InboundObservation lookup required.
    """
    fhir_obs = build_fhir_observation(log_row, sr_contexts=None,
                                      contract_scopes=None)
    return {
        'patient_guid': log_row.patient_guid,
        'source_type': 'fhir',
        'source_system_id': log_row.guid,
        'fhir_resource': fhir_obs,
        'clinical_context': {
            'service_request_guid': log_row.service_request_guid,
            'transaction_guid': log_row.transaction_guid,
            'contract_guid': log_row.contract_guid,
            'provider_org_guid': log_row.provider_org_guid,
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
