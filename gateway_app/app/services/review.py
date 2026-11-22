"""#1043: the EPS review marker (contract plans/patient_app_contract.md s3.2).

A patient's answers to the EPS form must be approved by care before they can
reach the patient summary. Gateway's part is to recognise such a report and
say so to the CDR: every extracted answer is forwarded with
``review_required: true`` in its clinical context, which the CDR stores as
``review_status = 'pending'``.

A report asks for review when its FHIR QuestionnaireResponse

* answers the EPS canonical (``questionnaire``, with or without ``|version``), or
* carries the tag ``https://pdhc.se/fhir/tag | eps-review``, or
* the report body says ``"review_required": true``.

Review exists only for patient-generated data, and that is decided by the
CREDENTIAL (a PAT with scope ``patient_generated``, #1014), never by the body.
So a review claim from any other PAT is REFUSED BY NAME. It is never quietly
accepted without the marker: that would store an unreviewed answer as
healthcare's own.
"""
from __future__ import annotations

EPS_CANONICAL = 'https://plan.pdhc.se/fhir/Questionnaire/eps-patient'
TAG_SYSTEM = 'https://pdhc.se/fhir/tag'
TAG_CODE = 'eps-review'

#: cdr_delivery_log.transaction_guid / concept_guid, and the CDR's
#: clinical_context.transaction_guid, are String(36). A review answer is keyed
#: by its linkId in exactly those columns.
MAX_LINK_ID = 36

REFUSED_NOT_PATIENT_GENERATED = 'REVIEW_REQUIRES_PATIENT_GENERATED'
REFUSED_NOT_A_QUESTIONNAIRE_RESPONSE = 'REVIEW_REQUIRES_QUESTIONNAIRE_RESPONSE'
REFUSED_INVALID_FLAG = 'INVALID_REVIEW_REQUIRED'
REFUSED_LINK_ID = 'REVIEW_LINK_ID_UNUSABLE'
REFUSED_NO_PATIENT_ORG = 'REVIEW_PATIENT_ORG_UNAVAILABLE'


def is_questionnaire_response(payload) -> bool:
    return (isinstance(payload, dict)
            and payload.get('resourceType') == 'QuestionnaireResponse')


def answers_eps_canonical(payload) -> bool:
    q = payload.get('questionnaire') if isinstance(payload, dict) else None
    return isinstance(q, str) and q.split('|', 1)[0].strip() == EPS_CANONICAL


def carries_review_tag(payload) -> bool:
    meta = payload.get('meta') if isinstance(payload, dict) else None
    tags = meta.get('tag') if isinstance(meta, dict) else None
    return any(isinstance(t, dict) and t.get('system') == TAG_SYSTEM
               and t.get('code') == TAG_CODE for t in (tags or []))


def claim(body) -> tuple[bool, str | None]:
    """(asks_for_review, error_code) for a report body.

    ``error_code`` is set when the body's own ``review_required`` is present
    and is not a JSON boolean: "yes" or 1 is a mistake to name, not a value
    to interpret.
    """
    payload = body.get('report_payload') if isinstance(body, dict) else None
    flags = [body.get('review_required') if isinstance(body, dict) else None]
    if isinstance(payload, dict):
        flags.append(payload.get('review_required'))
    for f in flags:
        if f is not None and not isinstance(f, bool):
            return False, REFUSED_INVALID_FLAG
    asked = any(f is True for f in flags)
    if is_questionnaire_response(payload):
        asked = asked or answers_eps_canonical(payload) or carries_review_tag(payload)
    return asked, None
