"""#1014: patient-generated or (absent) healthcare — gateway side.

Mirrors cdr.pdhc app/services/data_origin.py; the two must accept the same
values. OPT-IN (operator 2026-11-21): the only value is 'patient_generated';
absent means produced or endorsed by healthcare. Any other value is refused
at this hop with 422, so a misspelt flag never silently turns patient data
into healthcare data.

ENFORCED FROM THE CREDENTIAL. A PAT carrying the scope `patient_generated`
(issued by request.pdhc, e.g. `flask provider register-pat --scopes
read,write,patient_generated`) belongs to a patient-facing client: EVERY
observation it submits is stored as patient-generated, whatever the body says
or omits, the same way provider_org_guid comes from the PAT and never from the
body. A patient app therefore cannot forget the flag. A healthcare PAT may
still declare it per observation or per report (opt-in).
"""
from __future__ import annotations

PATIENT_GENERATED = 'patient_generated'
VALUES = (PATIENT_GENERATED,)

#: FHIR representation on the forwarded Observation.
TAG_SYSTEM = 'urn:pdhc:fhir:CodeSystem:data-origin'
TAG_CODE = 'patient-generated'
EXT_URL = 'urn:pdhc:fhir:extension:data-origin'


#: The PAT scope that marks a patient-facing client (#1014).
PAT_SCOPE = 'patient_generated'


def forced_by_credential(pat_result) -> str | None:
    """PATIENT_GENERATED when the authenticated PAT is a patient client."""
    has = getattr(pat_result, 'has_scope', None)
    return PATIENT_GENERATED if callable(has) and has(PAT_SCOPE) else None


def check(value):
    """(stored_value, error). Absent or empty → (None, None)."""
    if value is None or value == '':
        return None, None
    if value in VALUES:
        return value, None
    return None, (f'invalid data_origin {value!r}: the only accepted value is '
                  f"'{PATIENT_GENERATED}' (omit it for healthcare data)")
