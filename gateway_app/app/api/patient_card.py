"""#1049 — POST /api/v1/patient-card (contract plans/patient_app_contract.md s7.3).

The patient app sends one section of the patient's card ("Mitt patientkort").
Gateway checks with eps that the patient is listed at the care unit, stores
one review row per answer and forwards them to the CDR through the existing
delivery log and forwarder. No ServiceRequest, no grant, no PAT.
"""
from flask import jsonify, request

from . import api_bp
from .auth import require_patientapp_key
from ..services import patient_card


@api_bp.route('/patient-card', methods=['POST'])
@require_patientapp_key
def submit_patient_card():
    """202 {"receipt_token", "rows"}; 401 invalid_service_credentials;
    403 patient_not_at_unit; 409 card_version_reused; 422 validation;
    503 eps_unavailable (nothing stored)."""
    body = request.get_json(silent=True)
    result, status = patient_card.submit(body)
    resp = jsonify(result)
    resp.headers['Cache-Control'] = 'no-store'
    return resp, status
