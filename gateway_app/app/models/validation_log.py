import uuid
from datetime import datetime, timezone
from ..extensions import db


class ValidationLog(db.Model):
    __tablename__ = 'validation_log'

    id = db.Column(db.Integer, primary_key=True)
    guid = db.Column(db.String(36), unique=True, nullable=False,
                     default=lambda: str(uuid.uuid4()))

    observation_guid = db.Column(db.String(36),
                                 db.ForeignKey('inbound_observations.guid'),
                                 nullable=False, index=True)
    validation_type = db.Column(db.String(50), nullable=False)
    # fhir_schema, composite_key, response_type, value_range
    passed = db.Column(db.Boolean, nullable=False)
    error_details = db.Column(db.JSON, nullable=True)

    validated_at = db.Column(db.DateTime(timezone=True),
                             default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'guid': self.guid,
            'observation_guid': self.observation_guid,
            'validation_type': self.validation_type,
            'passed': self.passed,
            'error_details': self.error_details,
            'validated_at': self.validated_at.isoformat() if self.validated_at else None,
        }
