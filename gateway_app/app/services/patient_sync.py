"""Sync patient demographics (ips → CDR1).

Care-delivery dashboards (cd-assist) list a patient by the demographics stored
on CDR1's ``patient`` row. Observations alone never create that row, so a
patient delivering data shows up nameless. This module writes the row.

Trust model: the patient REFERENCE is the ``patient_guid`` off the *validated*
ServiceRequest (report_ingestion has already matched the request-GUID and
cross-checked the patient before calling here — that is the security gate). The
demographic DATA comes from ips (the registry of record), resolved by
request.pdhc into the SR context. A patient ips doesn't know stays pseudonymous
(no name → no write), and a sync failure is swallowed so it can never block a
report submission.
"""
import logging

from .cdr_client import CdrClient

logger = logging.getLogger(__name__)

# In-process idempotency: patient guids already handled (written, or confirmed
# name-less) by this worker process. cdr1's upsert is itself idempotent; this
# just avoids a redundant HTTP call on every observation for the same patient.
_synced = set()


def reset_cache():
    """Clear the in-process idempotency cache (tests / CLI use)."""
    _synced.clear()


def build_patient_resource(patient_guid, name, birth_date=None):
    """FHIR Patient with ``id`` = patient guid so cdr1's id-bearing POST does
    update-as-create and the live row's guid equals the patient guid."""
    resource = {
        'resourceType': 'Patient',
        'id': patient_guid,
        'identifier': [{'value': patient_guid}],
        'name': [{'text': name}],
        'active': True,
    }
    if birth_date:
        resource['birthDate'] = birth_date
    return resource


def ensure_cdr_patient(patient_guid, name, birth_date=None, *,
                       session_id=None, cache=True):
    """Upsert the patient's demographics into cdr1 once.

    Returns True if a write was issued, False if skipped (no guid / no name /
    already synced this process). Never raises — demographics are best-effort
    and must not affect report ingestion.
    """
    try:
        if not patient_guid:
            return False
        if cache and patient_guid in _synced:
            return False
        if not name:
            if cache:
                _synced.add(patient_guid)  # nothing to write; don't retry
            return False
        CdrClient.write_patient(
            build_patient_resource(patient_guid, name, birth_date),
            request_id=f'patient-sync-{patient_guid}',
            session_id=session_id,
        )
        if cache:
            _synced.add(patient_guid)
        logger.info('Synced patient demographics to cdr1 for %s',
                    str(patient_guid)[:12])
        return True
    except Exception as e:  # noqa: BLE001 — best-effort, never block ingestion
        logger.warning('cdr1 patient demographics sync failed for %s: %s',
                       str(patient_guid or '?')[:12], e)
        return False
