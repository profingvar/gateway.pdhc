"""Tracks completion status of inbound service requests.

A ServiceRequest is considered:
- 'active': observations are being received, grant not yet expired
- 'completed': all expected transactions have been delivered
- 'expired': grant expired before all transactions were delivered
- 'partial': some observations received but grant expired
"""
import uuid
from datetime import datetime, timezone
from ..extensions import db


class ServiceRequestStatus(db.Model):
    __tablename__ = 'service_request_status'

    id = db.Column(db.Integer, primary_key=True)
    guid = db.Column(db.String(36), unique=True, nullable=False,
                     default=lambda: str(uuid.uuid4()))

    service_request_guid = db.Column(db.String(36), unique=True,
                                     nullable=False, index=True)
    patient_guid = db.Column(db.String(36), nullable=False, index=True)
    provider_org_guid = db.Column(db.String(36), nullable=False, index=True)
    contract_guid = db.Column(db.String(36), nullable=False)

    # Status: active | completed | expired | partial
    status = db.Column(db.String(20), nullable=False, default='active')

    # Delivery tracking
    expected_transactions = db.Column(db.Integer, nullable=True)
    delivered_transactions = db.Column(db.Integer, nullable=False, default=0)
    total_observations = db.Column(db.Integer, nullable=False, default=0)

    # Grant expiry (from the composite key)
    grant_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Timestamps
    first_received_at = db.Column(db.DateTime(timezone=True),
                                  default=lambda: datetime.now(timezone.utc))
    last_received_at = db.Column(db.DateTime(timezone=True),
                                 default=lambda: datetime.now(timezone.utc))
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    expired_at = db.Column(db.DateTime(timezone=True), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True),
                           default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'guid': self.guid,
            'service_request_guid': self.service_request_guid,
            'patient_guid': self.patient_guid,
            'provider_org_guid': self.provider_org_guid,
            'contract_guid': self.contract_guid,
            'status': self.status,
            'expected_transactions': self.expected_transactions,
            'delivered_transactions': self.delivered_transactions,
            'total_observations': self.total_observations,
            'grant_expires_at': self.grant_expires_at.isoformat() if self.grant_expires_at else None,
            'first_received_at': self.first_received_at.isoformat() if self.first_received_at else None,
            'last_received_at': self.last_received_at.isoformat() if self.last_received_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'expired_at': self.expired_at.isoformat() if self.expired_at else None,
        }

    @property
    def is_expired(self):
        """Check if the grant has expired."""
        if not self.grant_expires_at:
            return False
        return datetime.now(timezone.utc) > self.grant_expires_at

    @property
    def delivery_progress(self):
        """Return delivery fraction as string, e.g. '3/5' or '3/?'."""
        expected = self.expected_transactions
        if expected:
            return f'{self.delivered_transactions}/{expected}'
        return f'{self.delivered_transactions}/?'
