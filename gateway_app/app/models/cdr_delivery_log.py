import hashlib
import json
import uuid
from datetime import datetime, timezone
from ..extensions import db


class CdrDeliveryLog(db.Model):
    """Tracks forwarding of inbound_observations rows to cdr.pdhc (cdr1).

    Mirrors cdr.pdhc/cdr_app/app/models/__init__.py CambioDeliveryLog
    (the cdr1 → real-Cambio sender) — same insert-then-send pattern,
    same retry-via-status semantics, simplified to a single delivery
    type (FHIR; gateway never emits openEHR).

    Carries enough denormalised dedup keys (payload_hash, dedup_key,
    service_request_guid) that the report-ingestion dedup checks can
    use this table directly, and the row survives deletion of the
    source InboundObservation row (phase 5 of the SSOT cutover; see
    docs/cdr1_ssot_cutover_plan.md).
    """
    __tablename__ = 'cdr_delivery_log'

    guid = db.Column(db.String(36), primary_key=True,
                    default=lambda: str(uuid.uuid4()))

    patient_guid = db.Column(db.String(36), nullable=False, index=True)

    # Denormalised dedup + traceability columns — populated by the
    # report_ingestion hook and used by the dedup queries in the same
    # module. Survive deletion of the InboundObservation row.
    payload_hash = db.Column(db.String(64), nullable=True, index=True)
    dedup_key = db.Column(db.String(64), nullable=True, index=True)
    service_request_guid = db.Column(db.String(36), nullable=True, index=True)
    concept_guid = db.Column(db.String(36), nullable=True, index=True)
    transaction_guid = db.Column(db.String(36), nullable=True, index=True)
    contract_guid = db.Column(db.String(36), nullable=True)
    provider_org_guid = db.Column(db.String(36), nullable=True)
    received_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Full FHIR R5 Observation resource. After SSOT phase 6 (#285)
    # this is the wire-shape source the forwarder reads when building
    # the cdr1 ingest payload; inbound_observations is no longer
    # involved.
    fhir_observation_json = db.Column(db.JSON, nullable=True)

    cdr_resource_id = db.Column(db.String(128), nullable=True)

    status = db.Column(db.String(32), nullable=False,
                       default='pending', index=True)

    attempt_count = db.Column(db.Integer, nullable=False, default=0)
    last_attempt_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    delivered_at = db.Column(db.DateTime(timezone=True), nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    @staticmethod
    def hash_payload(payload):
        """SHA-256 of a sorted-keys JSON serialisation. Used as the
        batch-level dedup index by report_ingestion. Moved here from
        InboundObservation in #299 (SSOT phase 6 closure)."""
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()

    @staticmethod
    def compute_dedup_key(patient_guid, transaction_guid, recorded_at):
        """Per-observation idempotency key —
        sha256(patient|transaction|recorded_at). None when recorded_at
        is missing (no time-disambiguation possible). Moved here from
        InboundObservation in #299."""
        if not recorded_at:
            return None
        parts = '|'.join([patient_guid or '', transaction_guid or '',
                          recorded_at])
        return hashlib.sha256(parts.encode()).hexdigest()
