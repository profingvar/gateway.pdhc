"""Observation vector storage (experimental).

Stores the reconstructed context from the GUID chain resolution
as vector embeddings for semantic querying.

Design will evolve during development.
"""
import uuid
from datetime import datetime, timezone
from ..extensions import db


class ObservationVector(db.Model):
    __tablename__ = 'observation_vectors'

    id = db.Column(db.Integer, primary_key=True)
    guid = db.Column(db.String(36), unique=True, nullable=False,
                     default=lambda: str(uuid.uuid4()))

    # Link to source observation
    observation_guid = db.Column(db.String(36),
                                 db.ForeignKey('inbound_observations.guid'),
                                 nullable=False, index=True)

    # Resolved GUID chain references
    careplan_guid = db.Column(db.String(36), nullable=True, index=True)
    plandef_guid = db.Column(db.String(36), nullable=True, index=True)
    transaction_guid = db.Column(db.String(36), nullable=True, index=True)

    # The full reconstructed context (JSON)
    resolved_context_json = db.Column(db.JSON, nullable=True)

    # Vector embedding — stored as JSON array for now.
    # When pgvector is available, this will migrate to a vector column.
    # embedding = db.Column(Vector(384))  # pgvector — enabled after extension setup
    embedding_json = db.Column(db.JSON, nullable=True)

    # Metadata
    vector_model = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True),
                           default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'guid': self.guid,
            'observation_guid': self.observation_guid,
            'careplan_guid': self.careplan_guid,
            'plandef_guid': self.plandef_guid,
            'transaction_guid': self.transaction_guid,
            'resolved_context_json': self.resolved_context_json,
            'vector_model': self.vector_model,
            'has_embedding': self.embedding_json is not None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
