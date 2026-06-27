import uuid
from datetime import datetime, timezone
from ..extensions import db


class CdrDeliveryLog(db.Model):
    """Tracks forwarding of inbound_observations rows to cdr.pdhc (cdr1).

    Mirrors cdr.pdhc/cdr_app/app/models/__init__.py CambioDeliveryLog
    (the cdr1 → real-Cambio sender) — same insert-then-send pattern,
    same retry-via-status semantics, simplified to a single delivery
    type (FHIR; gateway never emits openEHR).
    """
    __tablename__ = 'cdr_delivery_log'

    guid = db.Column(db.String(36), primary_key=True,
                    default=lambda: str(uuid.uuid4()))

    inbound_observation_guid = db.Column(
        db.String(36),
        db.ForeignKey('inbound_observations.guid'),
        nullable=False,
        unique=True,
        index=True,
    )
    patient_guid = db.Column(db.String(36), nullable=False, index=True)

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

    inbound_observation = db.relationship(
        'InboundObservation',
        backref=db.backref('cdr_delivery', uselist=False),
    )
