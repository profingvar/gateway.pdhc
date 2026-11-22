"""#1049 — Mitt patientkort: one section of the patient's card, sent by the
patient app, stored for care's review (contract plans/patient_app_contract.md
s7.3; s3.2 and s6 still apply).

There is NO ServiceRequest, no grant, no contract and no PAT on this path. The
patient reaches the app without a care order. What stands in for the chain of
s3:

* the caller is patientapp.pdhc, proven by its service key
  (``require_patientapp_key``), and it took ``patient_guid`` and
  ``care_unit_guid`` from the patient's own SSO session;
* eps says the patient is listed, ACTIVELY, at that care unit. Asked on every
  submission; eps unreachable is 503 and nothing is stored (fail closed).

The rows are built exactly as the s3.2 EPS path builds them — the same
extraction (`_walk_qr_items`, `_extract_qr_answer(prefer_code=True)`,
`_qr_child_json(review_required=True)`) — so the CDR cannot tell a card answer
from an EPS-form answer except by what s7.3 ADDS: ``card_section``,
``card_version`` and ``source_service = patientapp.pdhc`` in the ingest
context, and the patient as performer.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from flask import request as flask_request

from ..errors import APIError
from ..extensions import db
from ..models import AuditLog, CdrDeliveryLog
from . import data_origin as _origin
from . import review as _review
from .ips_client import EpsUnavailable, patient_listed_at_unit
from .report_ingestion import _extract_qr_answer, _qr_child_json, _walk_qr_items
from .sso_service import current_session_id

SOURCE_SERVICE = 'patientapp.pdhc'
SECTIONS = ('allergy', 'medication', 'history', 'vaccination', 'contact')
EPS_VERSION = '1'

#: The marker the forwarder and the builder recognise a card row by.
PERFORMER_PATIENT = 'patient'

ERR_VALIDATION = 'validation'
ERR_NOT_AT_UNIT = 'patient_not_at_unit'
ERR_EPS_UNAVAILABLE = 'eps_unavailable'
ERR_VERSION_REUSED = 'card_version_reused'

_UUID = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                   r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')


def _is_uuid(v) -> bool:
    return isinstance(v, str) and bool(_UUID.match(v))


def _invalid(message, fields):
    return APIError(message, code=ERR_VALIDATION, status_code=422, details=fields)


def _all_items(items):
    """Every item of a QuestionnaireResponse at any depth, under groups and
    under answers alike — the set the section rule applies to."""
    for item in items or []:
        if not isinstance(item, dict):
            yield item
            continue
        yield item
        yield from _all_items(item.get('item'))
        for a in item.get('answer') or []:
            if isinstance(a, dict):
                yield from _all_items(a.get('item'))


def validate(body):
    """The checked body, or a 422 naming every problem at once."""
    if not isinstance(body, dict):
        raise _invalid('JSON object body required', [{'field': None, 'code': 'required'}])
    fields = []
    for name in ('patient_guid', 'care_unit_guid', 'card_version'):
        v = body.get(name)
        if v in (None, ''):
            fields.append({'field': name, 'code': 'required'})
        elif not _is_uuid(v):
            fields.append({'field': name, 'code': 'wrong_type'})
    section = body.get('section')
    if section in (None, ''):
        fields.append({'field': 'section', 'code': 'required'})
    elif section not in SECTIONS:
        fields.append({'field': 'section', 'code': 'wrong_type'})
    qr = body.get('questionnaire_response')
    if not isinstance(qr, dict):
        fields.append({'field': 'questionnaire_response', 'code': 'required'})
    if fields:
        raise _invalid('The patient card body is incomplete or malformed', fields)

    if qr.get('resourceType') not in (None, 'QuestionnaireResponse'):
        fields.append({'field': 'questionnaire_response.resourceType',
                       'code': 'wrong_type'})
    q = qr.get('questionnaire')
    if q not in (None, '') and not _review.answers_eps_canonical(qr):
        fields.append({'field': 'questionnaire_response.questionnaire',
                       'code': 'wrong_type'})
    if not isinstance(qr.get('item'), list) or not qr.get('item'):
        fields.append({'field': 'questionnaire_response.item', 'code': 'required'})
    if fields:
        raise _invalid('questionnaire_response is not a response to the EPS form',
                       fields)

    prefix = f'{section}.'
    for item in _all_items(qr['item']):
        link = item.get('linkId') if isinstance(item, dict) else None
        if not isinstance(link, str) or not link:
            fields.append({'linkId': None, 'code': 'required'})
        elif not link.startswith(prefix):
            # s7.3: items outside the section are a 422, never dropped — a
            # card version supersedes the previous one, so a silently lost
            # answer would read as the patient removing it.
            fields.append({'linkId': link, 'code': 'outside_section'})
        elif len(link) > _review.MAX_LINK_ID:
            fields.append({'linkId': link, 'code': 'wrong_type'})
    if fields:
        raise _invalid(f'Every item must belong to the section {section!r} '
                       f'of the EPS form', fields)
    return {
        'patient_guid': body['patient_guid'],
        'care_unit_guid': body['care_unit_guid'],
        'section': section,
        'card_version': body['card_version'],
        'questionnaire_response': qr,
    }


def _extract(qr, receipt_guid):
    """[(item, group, answer, value, response_type)] — or a 422 when an
    answered item carries an answer gateway cannot store (the EPS path skips
    it; a card cannot, for the reason given in ``validate``)."""
    out, bad = [], []
    for item, group in _walk_qr_items(qr.get('item'), receipt_guid):
        for answer in item.get('answer') or []:
            if not isinstance(answer, dict):
                continue
            value, rtype = _extract_qr_answer(answer, prefer_code=True)
            if value is None:
                bad.append({'linkId': item.get('linkId'), 'code': 'wrong_type'})
                continue
            out.append((item, group, answer, value, rtype))
    if bad:
        raise _invalid('An answer has a type gateway cannot store', bad)
    if not out:
        raise _invalid('The card section has no answer',
                       [{'linkId': None, 'code': 'required'}])
    return out


def _stored_qr(card):
    """The QuestionnaireResponse as gateway records it: the server fills the
    fields that identify it, whatever the device sent for them."""
    qr = dict(card['questionnaire_response'])
    qr['resourceType'] = 'QuestionnaireResponse'
    qr['questionnaire'] = f'{_review.EPS_CANONICAL}|{EPS_VERSION}'
    qr['subject'] = {'reference': f"Patient/{card['patient_guid']}"}
    meta = dict(qr.get('meta') or {})
    tags = [t for t in (meta.get('tag') or []) if isinstance(t, dict)]
    if not _review.carries_review_tag({'meta': {'tag': tags}}):
        tags.append({'system': _review.TAG_SYSTEM, 'code': _review.TAG_CODE})
    meta['tag'] = tags
    qr['meta'] = meta
    qr.setdefault('status', 'completed')
    return qr


def _version_key(card_version: str) -> str:
    """dedup_key of the PARENT row: one card version, one receipt."""
    return hashlib.sha256(f'patient-card|{card_version.lower()}'.encode()).hexdigest()


def _audit(event, card, detail=None, **snapshot):
    try:
        db.session.add(AuditLog(
            event_type=event,
            actor_guid=SOURCE_SERVICE,
            data_subject_guid=(card or {}).get('patient_guid'),
            resource_guid=(card or {}).get('card_version'),
            ip_address=flask_request.remote_addr,
            correlation_id=flask_request.headers.get('X-Correlation-Id'),
            payload_snapshot={
                'section': (card or {}).get('section'),
                'care_unit_guid': (card or {}).get('care_unit_guid'),
                **({'detail': str(detail)[:500]} if detail else {}),
                **snapshot,
            },
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()


def submit(body):
    """POST /api/v1/patient-card. Returns ``(response_dict, http_status)``."""
    try:
        card = validate(body)
    except APIError as e:
        _audit('patient_card.rejected', body if isinstance(body, dict) else None,
               reason=e.code, fields=e.details)
        raise

    # ── eps: is the patient listed, actively, at that unit? ──────────────
    try:
        listed = patient_listed_at_unit(card['patient_guid'], card['care_unit_guid'])
    except EpsUnavailable as e:
        _audit('patient_card.rejected', card, reason=ERR_EPS_UNAVAILABLE,
               eps_reason=e.reason)
        raise APIError('eps.pdhc could not confirm where the patient is listed. '
                       'Nothing was stored; retry.',
                       code=ERR_EPS_UNAVAILABLE, status_code=503)
    if not listed:
        _audit('patient_card.rejected', card, reason=ERR_NOT_AT_UNIT)
        raise APIError('The patient is not listed at that care unit',
                       code=ERR_NOT_AT_UNIT, status_code=403)

    stored_qr = _stored_qr(card)
    payload_hash = CdrDeliveryLog.hash_payload({
        'patient_guid': card['patient_guid'],
        'care_unit_guid': card['care_unit_guid'],
        'section': card['section'],
        'questionnaire_response': card['questionnaire_response'],
    })

    # ── one card version, one receipt ────────────────────────────────────
    version_key = _version_key(card['card_version'])
    prior = (CdrDeliveryLog.query
             .filter(CdrDeliveryLog.dedup_key == version_key,
                     CdrDeliveryLog.service_request_guid.is_(None))
             .first())
    if prior is not None:
        if prior.payload_hash == payload_hash and prior.patient_guid == card['patient_guid']:
            rows = (CdrDeliveryLog.query
                    .filter(CdrDeliveryLog.service_request_guid.is_(None),
                            CdrDeliveryLog.patient_guid == prior.patient_guid,
                            CdrDeliveryLog.guid != prior.guid)
                    .all())
            n = sum(1 for r in rows
                    if isinstance(r.fhir_observation_json, dict)
                    and r.fhir_observation_json.get('parent_guid') == prior.guid)
            _audit('patient_card.duplicate', card, receipt_token=prior.guid, rows=n)
            return {'receipt_token': prior.guid, 'rows': n}, 202
        _audit('patient_card.rejected', card, reason=ERR_VERSION_REUSED)
        raise APIError('This card_version was already used for different content',
                       code=ERR_VERSION_REUSED, status_code=409)

    now = datetime.now(timezone.utc)
    sid = current_session_id()
    parent = CdrDeliveryLog(
        patient_guid=card['patient_guid'],
        service_request_guid=None,           # s7.3: no ServiceRequest
        contract_guid=None,                  # no contract, no grant
        provider_org_guid=None,              # no PAT, so no provider org
        data_origin=_origin.PATIENT_GENERATED,
        payload_hash=payload_hash,
        dedup_key=version_key,
        received_at=now,
        fhir_observation_json={
            'patient_card': True,
            'source_service': SOURCE_SERVICE,
            'card_section': card['section'],
            'card_version': card['card_version'],
            'care_unit_guid': card['care_unit_guid'],
            'questionnaire_response': stored_qr,
        },
        operator_session_id=sid,
        status='skipped',                    # the whole QR is never forwarded
    )
    db.session.add(parent)
    db.session.flush()

    try:
        answers = _extract(stored_qr, parent.guid)
    except APIError as e:
        db.session.rollback()
        _audit('patient_card.rejected', card, reason=e.code, fields=e.details)
        raise

    children = []
    for item, group, answer, value, rtype in answers:
        link_id = item.get('linkId')
        text = item.get('text', '') or ''
        doc = _qr_child_json(
            link_id, text, '', text, answer, stored_qr['questionnaire'],
            parent.guid, value, rtype, group=group, review_required=True,
            patient_org_guid=card['care_unit_guid'])
        # s7.3 additions — read by the forwarder (into clinical_context) and
        # the builder (performer), from the row and nowhere else.
        doc.update({
            'card_section': card['section'],
            'card_version': card['card_version'],
            'source_service': SOURCE_SERVICE,
            'performer': PERFORMER_PATIENT,
        })
        child = CdrDeliveryLog(
            patient_guid=card['patient_guid'],
            service_request_guid=None,
            transaction_guid=link_id,
            concept_guid=link_id,            # the form has no concepts (s3.2)
            contract_guid=None,
            provider_org_guid=None,
            data_origin=_origin.PATIENT_GENERATED,
            received_at=now,
            fhir_observation_json=doc,
            operator_session_id=sid,
            status='pending',
        )
        db.session.add(child)
        children.append(child)
    db.session.flush()

    db.session.add(AuditLog(
        event_type='patient_card.received',
        actor_guid=SOURCE_SERVICE,
        data_subject_guid=card['patient_guid'],
        resource_guid=card['card_version'],
        ip_address=flask_request.remote_addr,
        correlation_id=flask_request.headers.get('X-Correlation-Id'),
        payload_snapshot={
            'section': card['section'],
            'card_version': card['card_version'],
            'care_unit_guid': card['care_unit_guid'],
            'receipt_token': parent.guid,
            'rows': len(children),
            'payload_hash': payload_hash,
        },
    ))
    db.session.commit()
    return {'receipt_token': parent.guid, 'rows': len(children)}, 202
