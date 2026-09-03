"""Patient demographics sync: SR-context properties + cdr1 upsert.

Reference is the validated SR patient_guid; the DATA came from ips via the SR
context. Writing must be idempotent (once per patient), skip name-less patients
(pseudonymous), and never raise (a sync failure must not affect a report).
"""
from unittest.mock import patch

import app.services.patient_sync as ps
from app.services.cdr_client import CdrUnavailable, CdrRejected
from app.services.sr_context import SRContextResult


def setup_function(_):
    ps.reset_cache()


# --- SRContextResult properties --------------------------------------------

def test_context_exposes_demographics():
    r = SRContextResult(found=True, data={'patient_name': 'Anna A',
                                          'patient_birth_date': '1980-01-01'})
    assert r.patient_name == 'Anna A'
    assert r.patient_birth_date == '1980-01-01'


def test_context_demographics_default_empty():
    r = SRContextResult(found=True, data={})
    assert r.patient_name == ''
    assert r.patient_birth_date == ''


# --- ensure_cdr_patient ----------------------------------------------------

def test_writes_once_then_idempotent_by_cache():
    with patch.object(ps.CdrClient, 'write_patient', return_value={}) as w:
        assert ps.ensure_cdr_patient('pat-1', 'Anna A', '1980-01-01') is True
        assert ps.ensure_cdr_patient('pat-1', 'Anna A', '1980-01-01') is False
    assert w.call_count == 1
    resource = w.call_args[0][0]
    assert resource['id'] == 'pat-1'
    assert resource['identifier'] == [{'value': 'pat-1'}]
    assert resource['name'] == [{'text': 'Anna A'}]
    assert resource['birthDate'] == '1980-01-01'
    assert resource['resourceType'] == 'Patient'


def test_no_birthdate_omits_key():
    with patch.object(ps.CdrClient, 'write_patient', return_value={}) as w:
        assert ps.ensure_cdr_patient('pat-x', 'No Birth') is True
    assert 'birthDate' not in w.call_args[0][0]


def test_skips_when_no_name():
    with patch.object(ps.CdrClient, 'write_patient') as w:
        assert ps.ensure_cdr_patient('pat-2', '') is False
    w.assert_not_called()


def test_skips_when_no_guid():
    with patch.object(ps.CdrClient, 'write_patient') as w:
        assert ps.ensure_cdr_patient('', 'Anna') is False
    w.assert_not_called()


def test_failsoft_on_cdr_unavailable():
    with patch.object(ps.CdrClient, 'write_patient',
                      side_effect=CdrUnavailable('boom')):
        assert ps.ensure_cdr_patient('pat-3', 'Bob', cache=False) is False


def test_failsoft_on_cdr_rejected():
    with patch.object(ps.CdrClient, 'write_patient',
                      side_effect=CdrRejected(400, 'bad')):
        assert ps.ensure_cdr_patient('pat-4', 'Bob', cache=False) is False


def test_cache_false_writes_every_call():
    with patch.object(ps.CdrClient, 'write_patient', return_value={}) as w:
        ps.ensure_cdr_patient('pat-5', 'Anna', cache=False)
        ps.ensure_cdr_patient('pat-5', 'Anna', cache=False)
    assert w.call_count == 2
