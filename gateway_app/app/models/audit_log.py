import uuid
from datetime import datetime, timezone
from ..extensions import db


class AuditLog(db.Model):
    __tablename__ = 'audit_log'

    id = db.Column(db.Integer, primary_key=True)
    guid = db.Column(db.String(36), unique=True, nullable=False,
                     default=lambda: str(uuid.uuid4()))

    event_type = db.Column(db.String(50), nullable=False, index=True)
    # pat.validated, pat.rejected, grant.used, report.received,
    # feed.accessed, bundle.downloaded, bundle.pushed, etc.

    actor_guid = db.Column(db.String(36), nullable=True, index=True)
    data_subject_guid = db.Column(db.String(36), nullable=True, index=True)
    # patient GUID — for GDPR subject access requests

    receipt_token = db.Column(db.String(255), nullable=True, index=True)
    payload_snapshot = db.Column(db.JSON, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    correlation_id = db.Column(db.String(36), nullable=True, index=True)

    created_at = db.Column(db.DateTime(timezone=True),
                           default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'guid': self.guid,
            'event_type': self.event_type,
            'actor_guid': self.actor_guid,
            'data_subject_guid': self.data_subject_guid,
            'receipt_token': self.receipt_token,
            'ip_address': self.ip_address,
            'correlation_id': self.correlation_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
