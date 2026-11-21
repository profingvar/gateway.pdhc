"""#1014: patient-generated or (absent) healthcare — gateway side.

Mirrors cdr.pdhc app/services/data_origin.py; the two must accept the same
values. OPT-IN (operator 2026-11-21): the only value is 'patient_generated';
absent means produced or endorsed by healthcare. Any other value is refused
at this hop with 422, so a misspelt flag never silently turns patient data
into healthcare data.

Known gap: the flag is DECLARED by the submitter. When the patient app has
its own credential, gateway should SET it from the authenticated client, as
provider_org_guid comes from the PAT.
"""
from __future__ import annotations

PATIENT_GENERATED = 'patient_generated'
VALUES = (PATIENT_GENERATED,)

#: FHIR representation on the forwarded Observation.
TAG_SYSTEM = 'urn:pdhc:fhir:CodeSystem:data-origin'
TAG_CODE = 'patient-generated'
EXT_URL = 'urn:pdhc:fhir:extension:data-origin'


def check(value):
    """(stored_value, error). Absent or empty → (None, None)."""
    if value is None or value == '':
        return None, None
    if value in VALUES:
        return value, None
    return None, (f'invalid data_origin {value!r}: the only accepted value is '
                  f"'{PATIENT_GENERATED}' (omit it for healthcare data)")
