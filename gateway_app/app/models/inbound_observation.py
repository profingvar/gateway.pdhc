import uuid
import hashlib
import json
from datetime import datetime, timezone
from ..extensions import db


class InboundObservation(db.Model):
    __tablename__ = 'inbound_observations'

    id = db.Column(db.Integer, primary_key=True)
    guid = db.Column(db.String(36), unique=True, nullable=False,
                     default=lambda: str(uuid.uuid4()))

    # Composite key fields (from provider submission)
    service_request_guid = db.Column(db.String(36), nullable=False, index=True)
    transaction_guid = db.Column(db.String(36), nullable=True, index=True)
    concept_guid = db.Column(db.String(36), nullable=True, index=True)
    patient_guid = db.Column(db.String(36), nullable=False, index=True)
    provider_org_guid = db.Column(db.String(36), nullable=False, index=True)
    contract_guid = db.Column(db.String(36), nullable=False, index=True)
    grant_token = db.Column(db.String(64), nullable=True)

    # Observation data
    fhir_observation_json = db.Column(db.JSON, nullable=False)
    value = db.Column(db.Text, nullable=True)
    response_type = db.Column(db.String(50), nullable=True)
    payload_hash = db.Column(db.String(64), nullable=True)
    # Per-observation idempotency key — sha256(patient|tx|recorded_at).
    # Null when recorded_at is unknown; partial unique index on
    # (service_request_guid, dedup_key) prevents re-POSTed duplicates.
    dedup_key = db.Column(db.String(64), nullable=True, index=True)

    # Resolution status (GUID chain resolution outcome)
    resolution_status = db.Column(db.String(20), nullable=False, default='pending')
    # pending → resolved | failed

    # Validation
    validation_status = db.Column(db.String(20), nullable=False, default='pending')
    # pending → valid → invalid

    # True when the submission arrived after the SR's period_end. Late
    # reports are still accepted (ticket #90) — the flag lets downstream
    # consumers separate in-window from out-of-window data.
    is_late = db.Column(db.Boolean, nullable=False, default=False, index=True)

    # Timestamps
    received_at = db.Column(db.DateTime(timezone=True),
                            default=lambda: datetime.now(timezone.utc))
    created_at = db.Column(db.DateTime(timezone=True),
                           default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'guid': self.guid,
            'service_request_guid': self.service_request_guid,
            'transaction_guid': self.transaction_guid,
            'concept_guid': self.concept_guid,
            'patient_guid': self.patient_guid,
            'provider_org_guid': self.provider_org_guid,
            'contract_guid': self.contract_guid,
            'value': self.value,
            'response_type': self.response_type,
            'resolution_status': self.resolution_status,
            'validation_status': self.validation_status,
            'is_late': self.is_late,
            'received_at': self.received_at.isoformat() if self.received_at else None,
        }

    @staticmethod
    def hash_payload(payload):
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()

    @staticmethod
    def compute_dedup_key(patient_guid, transaction_guid, recorded_at):
        """Return sha256 hex of patient|tx|recorded_at, or None if
        recorded_at is missing (the field that disambiguates two readings
        of the same parameter). NULL dedup_keys are not constrained by
        the partial unique index → no per-obs dedup attempted.
        """
        if not recorded_at:
            return None
        parts = '|'.join([patient_guid or '', transaction_guid or '',
                          recorded_at])
        return hashlib.sha256(parts.encode()).hexdigest()
