"""Vector construction and storage service (experimental).

Takes resolved GUID chain context and builds vector representations
for semantic querying.  The embedding strategy is intentionally simple
for now — a text-based context string stored as JSON array.  This will
be replaced with a proper embedding model as the design matures.
"""
import hashlib
import logging
from flask import current_app
from ..models import ObservationVector, InboundObservation
from ..extensions import db
from .guid_resolution import GuidResolutionService

logger = logging.getLogger(__name__)


class VectorService:

    @staticmethod
    def build_and_store(observation):
        """Resolve the GUID chain for an observation and store the vector.

        Args:
            observation: InboundObservation record

        Returns:
            ObservationVector or None if resolution failed
        """
        # Check if vector already exists
        existing = ObservationVector.query.filter_by(
            observation_guid=observation.guid,
        ).first()
        if existing:
            return existing

        # Resolve the GUID chain
        chain = GuidResolutionService.resolve_for_observation(observation)
        if not chain.resolved:
            logger.warning(
                'GUID chain resolution failed for observation %s: %s',
                observation.guid, chain.error,
            )
            return None

        # Build the context representation
        context = chain.to_context_dict()

        # Add observation-specific data
        context['observation_value'] = observation.value
        context['observation_response_type'] = observation.response_type
        context['provider_org_guid'] = observation.provider_org_guid
        context['contract_guid'] = observation.contract_guid

        # Build a simple text embedding (experimental)
        # This will be replaced with a proper embedding model
        embedding = _build_text_embedding(context)

        # Store the vector
        vector = ObservationVector(
            observation_guid=observation.guid,
            careplan_guid=chain.careplan_guid or None,
            plandef_guid=chain.plan_definition_guid or None,
            transaction_guid=chain.transaction_guid or None,
            resolved_context_json=context,
            embedding_json=embedding,
            vector_model='text-hash-v0',
        )
        db.session.add(vector)

        # Update observation status
        observation.resolution_status = 'vectorized'
        db.session.commit()

        return vector

    @staticmethod
    def build_batch(service_request_guid):
        """Build vectors for all pending observations under a service request.

        Returns:
            dict with counts
        """
        observations = InboundObservation.query.filter_by(
            service_request_guid=service_request_guid,
            resolution_status='pending',
        ).all()

        built = 0
        failed = 0
        for obs in observations:
            vector = VectorService.build_and_store(obs)
            if vector:
                built += 1
            else:
                failed += 1

        return {
            'service_request_guid': service_request_guid,
            'total': len(observations),
            'vectorized': built,
            'failed': failed,
        }

    @staticmethod
    def query_by_patient(patient_guid):
        """Get all vectors for a patient."""
        observations = InboundObservation.query.filter_by(
            patient_guid=patient_guid,
        ).all()
        obs_guids = [o.guid for o in observations]
        if not obs_guids:
            return []
        vectors = ObservationVector.query.filter(
            ObservationVector.observation_guid.in_(obs_guids),
        ).all()
        return [v.to_dict() for v in vectors]

    @staticmethod
    def query_by_careplan(careplan_guid):
        """Get all vectors under a careplan."""
        vectors = ObservationVector.query.filter_by(
            careplan_guid=careplan_guid,
        ).all()
        return [v.to_dict() for v in vectors]

    @staticmethod
    def query_similar(target_context, limit=10):
        """Find vectors similar to a target context (experimental).

        For now this does a simple text-hash comparison.
        Will be replaced with pgvector cosine similarity when
        proper embeddings are implemented.
        """
        target_embedding = _build_text_embedding(target_context)
        if not target_embedding:
            return []

        # Simple approach: find vectors with matching concept_guid
        concept_guid = target_context.get('concept_guid', '')
        if concept_guid:
            vectors = ObservationVector.query.filter(
                ObservationVector.resolved_context_json['concept_guid'].as_string() == concept_guid,
            ).limit(limit).all()
            return [v.to_dict() for v in vectors]

        return []


def _build_text_embedding(context):
    """Build a simple text-based embedding (experimental placeholder).

    This creates a deterministic hash-based representation of the
    clinical context.  It will be replaced with a proper embedding
    model (e.g., sentence-transformers) as the vector design matures.

    Returns:
        list of floats (dimension = PGVECTOR_DIMENSIONS)
    """
    # Build a text representation of the clinical context
    parts = [
        context.get('concept_name', ''),
        context.get('concept_guid', ''),
        context.get('activity_description', ''),
        context.get('careplan_title', ''),
        context.get('plandef_title', ''),
        context.get('response_type', ''),
        str(context.get('observation_value', '')),
    ]
    text = ' '.join(p for p in parts if p)
    if not text:
        return None

    # Create a deterministic hash-based "embedding"
    # This is NOT a real embedding — just a placeholder for the schema
    h = hashlib.sha256(text.encode()).hexdigest()
    # Convert hex chars to float values between -1 and 1
    dimensions = 384  # default, will use config when pgvector is live
    embedding = []
    for i in range(dimensions):
        char_idx = i % len(h)
        val = (int(h[char_idx], 16) - 8) / 8.0  # maps 0-15 to -1.0..+0.875
        embedding.append(round(val, 4))

    return embedding
