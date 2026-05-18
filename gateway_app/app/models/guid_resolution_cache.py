"""Cache for upstream GUID resolution lookups.

Avoids repeated calls to request.pdhc when resolving
transaction → careplan → plandefinition chains.
"""
import uuid
from datetime import datetime, timezone
from ..extensions import db


class GuidResolutionCache(db.Model):
    __tablename__ = 'guid_resolution_cache'

    id = db.Column(db.Integer, primary_key=True)
    guid = db.Column(db.String(36), unique=True, nullable=False,
                     default=lambda: str(uuid.uuid4()))

    # What was resolved
    source_guid = db.Column(db.String(36), nullable=False, index=True)
    source_type = db.Column(db.String(50), nullable=False)
    # transaction, careplan, plandefinition

    # Resolved data
    resolved_json = db.Column(db.JSON, nullable=True)
    fetched_from = db.Column(db.String(512), nullable=True)

    # Cache control
    fetched_at = db.Column(db.DateTime(timezone=True),
                           default=lambda: datetime.now(timezone.utc))
    ttl_seconds = db.Column(db.Integer, nullable=False, default=3600)

    created_at = db.Column(db.DateTime(timezone=True),
                           default=lambda: datetime.now(timezone.utc))

    def is_expired(self):
        if not self.fetched_at:
            return True
        now = datetime.now(timezone.utc)
        fetched = self.fetched_at
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        age = (now - fetched).total_seconds()
        return age > self.ttl_seconds

    def to_dict(self):
        return {
            'guid': self.guid,
            'source_guid': self.source_guid,
            'source_type': self.source_type,
            'resolved_json': self.resolved_json,
            'fetched_from': self.fetched_from,
            'is_expired': self.is_expired(),
            'fetched_at': self.fetched_at.isoformat() if self.fetched_at else None,
        }
