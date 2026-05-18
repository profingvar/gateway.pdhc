"""Vector query endpoints (experimental).

These endpoints expose the resolved GUID chain context as vectors
for semantic querying.  The design will evolve.
"""
from flask import jsonify, request as flask_request
from . import api_bp
from ..services.vector_service import VectorService


@api_bp.route('/vectors/by-patient/<patient_guid>', methods=['GET'])
def vectors_by_patient(patient_guid):
    """Get all vectors for a patient."""
    # TODO: Add SU auth when auth layer is ready
    vectors = VectorService.query_by_patient(patient_guid)
    return jsonify({'vectors': vectors, 'count': len(vectors)}), 200


@api_bp.route('/vectors/by-careplan/<careplan_guid>', methods=['GET'])
def vectors_by_careplan(careplan_guid):
    """Get all vectors under a careplan."""
    vectors = VectorService.query_by_careplan(careplan_guid)
    return jsonify({'vectors': vectors, 'count': len(vectors)}), 200


@api_bp.route('/vectors/similar', methods=['POST'])
def vectors_similar():
    """Find similar vectors (experimental)."""
    body = flask_request.get_json(silent=True) or {}
    context = body.get('context', {})
    limit = body.get('limit', 10)
    vectors = VectorService.query_similar(context, limit=limit)
    return jsonify({'vectors': vectors, 'count': len(vectors)}), 200


@api_bp.route('/vectors/resolve/<service_request_guid>', methods=['POST'])
def resolve_and_vectorize(service_request_guid):
    """Trigger GUID chain resolution and vector construction for a SR.

    Resolves all pending observations under the given service request.
    """
    result = VectorService.build_batch(service_request_guid)
    return jsonify(result), 200
