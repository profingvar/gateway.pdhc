"""Build a fully referenced FHIR R5 Observation from an InboundObservation row.

Used by:
  - app/api/observations.py — for analyse-phase consumers (dashboard
    pull). Wraps results in a searchset Bundle.
  - app/services/cdr_forwarder.py — for forwarding to cdr.pdhc. Sends
    the Observation as the fhir_resource payload to cdr1's
    /api/v1/ingest endpoint.

Includes back-references to ServiceRequest, PlanDefinition, Contract,
Organizations (requester + provider), Goals, and reference ranges.
The function is read-only — no db writes — so it is safe to call from
both request-handling and background-worker contexts.
"""
import json

UNIT_SYSTEM = 'https://plan.pdhc.se/api/v1/lookup/units'
VALUE_SYSTEM = 'https://plan.pdhc.se/api/v1/lookup/values'
# response_types that come from questionnaire/survey answers (not lab results).
_SURVEY_TYPES = ('categorical', 'boolean', 'text', 'dateTime', 'graph')


def _unit_fields(base, unit_display, unit_code):
    """Attach FHIR Quantity unit fields: human ``unit`` display + machine ``code``.

    #489.3: previously the human ``unit_display`` was emitted as the machine
    ``code``. FHIR wants unit=display, code=coded unit, system=code system.
    """
    if unit_display or unit_code:
        base['unit'] = unit_display or unit_code
        base['system'] = UNIT_SYSTEM
        base['code'] = unit_code or unit_display
    return base


def _to_bool(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ('true', '1', 'yes', 'y', 't')


CANONICAL_OBS_EXT = 'urn:pdhc:fhir:extension:canonical-observation'


def build_canonical_observation(row):
    """The canonical typed observation core (#500).

    The single source of BOTH the FHIR and openEHR projections. Values are
    carried in their DECLARED type (never float-coerced), so a downstream
    renderer (rosetta openEHR, #501) does not inherit FHIR's lossiness — a
    categorical stays a string, a boolean stays a bool. ``unit`` is the plan.pdhc
    unit code (rosetta translates it to UCUM via its concept map's unit bridge).
    """
    raw = row.fhir_observation_json or {}
    received = getattr(row, 'received_at', None)
    eff = raw.get('recorded_at') or (received.isoformat() if received else None)
    return {
        'concept_guid': getattr(row, 'concept_guid', None) or raw.get('concept_guid'),
        'value': raw.get('value'),
        'response_type': raw.get('response_type'),
        'unit': raw.get('unit') or None,
        'unit_display': raw.get('unit_display') or None,
        'range_min': raw.get('range_min'),
        'range_max': raw.get('range_max'),
        'effective_at': eff,
    }


def _add_typed_value(obs, value, rtype, unit_display, unit_code):
    """Emit the correct value[x] for the DECLARED response_type (#489.1/2).

    numeric→valueQuantity, categorical→valueCodeableConcept, boolean→valueBoolean,
    dateTime→valueDateTime, text→valueString, graph→valueString (marker; the rich
    data rides the provider-graph extension). ``value is None`` = missing data,
    no value[x]. Never blind-float()s a categorical into a quantity.
    """
    if value is None:
        return
    if rtype == 'numeric':
        try:
            obs['valueQuantity'] = _unit_fields({'value': float(value)}, unit_display, unit_code)
        except (TypeError, ValueError):
            obs['valueString'] = str(value)
    elif rtype == 'boolean':
        obs['valueBoolean'] = _to_bool(value)
    elif rtype == 'categorical':
        s = str(value)
        obs['valueCodeableConcept'] = {
            'coding': [{'system': VALUE_SYSTEM, 'code': s, 'display': s}],
            'text': s,
        }
    elif rtype == 'dateTime':
        obs['valueDateTime'] = str(value)
    elif rtype in ('text', 'graph'):
        obs['valueString'] = str(value)
    else:
        # unknown/legacy response_type: best-effort, but still never mislabel a
        # non-numeric as a quantity.
        try:
            obs['valueQuantity'] = _unit_fields({'value': float(value)}, unit_display, unit_code)
        except (TypeError, ValueError):
            obs['valueString'] = str(value)


def build_fhir_observation(row, sr_contexts=None, contract_scopes=None):
    """Build a fully referenced FHIR R5 Observation from an InboundObservation."""
    sr_contexts = sr_contexts or {}
    contract_scopes = contract_scopes or {}
    raw = row.fhir_observation_json or {}
    sr_ctx = sr_contexts.get(row.service_request_guid) or {}
    contract_scope = contract_scopes.get(row.contract_guid) or {}

    name = raw.get('concept_name') or ''
    unit_display = raw.get('unit_display') or raw.get('unit') or None  # human label
    unit_code = raw.get('unit') or raw.get('unit_display') or None      # machine code (#489.3)
    recorded = raw.get('recorded_at')
    eff = recorded or (row.received_at.isoformat() if row.received_at else None)

    value = raw.get('value')  # #490: no row.value fallback — CdrDeliveryLog has no such column
    rtype = (raw.get('response_type') or '').strip()

    # #489.4: questionnaire/survey answers are not laboratory results.
    _cat = ('survey', 'Survey') if rtype in _SURVEY_TYPES else ('laboratory', 'Laboratory')

    obs = {
        'resourceType': 'Observation',
        'id': raw.get('id', row.guid),
        'meta': {'profile': ['http://hl7.org/fhir/StructureDefinition/Observation']},
        'status': 'final',
        'category': [{
            'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/observation-category',
                'code': _cat[0],
                'display': _cat[1],
            }]
        }],
        'code': {
            'coding': [
                {
                    'system': 'urn:pdhc:concept',
                    'code': row.concept_guid or '',
                    'display': name,
                },
                {
                    'system': 'https://plan.pdhc.se/api/v1/concepts',
                    'code': row.concept_guid or '',
                    'display': name,
                },
            ],
            'text': name,
        },
        'subject': {'reference': f'Patient/{row.patient_guid}'},
        'effectiveDateTime': eff,
        'issued': (row.received_at.isoformat() if row.received_at else None),
    }

    # --- Value: typed on the declared response_type (#489.1/2/3) ---
    # Unit reference routes through plan.pdhc's unit catalog (platform
    # principle: only plan.pdhc emits external code-system refs).
    _add_typed_value(obs, value, rtype, unit_display, unit_code)

    # --- basedOn: ServiceRequest + PlanDefinition ---
    based_on = []
    if row.service_request_guid:
        based_on.append({
            'reference': f'https://request.pdhc.se/api/v1/service-requests/{row.service_request_guid}',
            'type': 'ServiceRequest',
            'identifier': {'value': row.service_request_guid},
        })
    plan_guid = sr_ctx.get('plan_definition_guid')
    if plan_guid:
        based_on.append({
            'reference': f'https://plan.pdhc.se/api/v1/plandefinitions/{plan_guid}',
            'type': 'PlanDefinition',
            'identifier': {'value': plan_guid},
        })
    if based_on:
        obs['basedOn'] = based_on

    # --- performer: provider organization ---
    if row.provider_org_guid:
        obs['performer'] = [{
            'reference': f'https://sso.pdhc.se/api/organisations/{row.provider_org_guid}',
            'type': 'Organization',
            'display': 'Provider',
            'identifier': {'value': row.provider_org_guid},
        }]

    # --- referenceRange from raw or sr_context goals ---
    range_min = raw.get('range_min')
    range_max = raw.get('range_max')
    if range_min is None or range_max is None:
        for goal in (sr_ctx.get('goals') or []):
            if goal.get('concept_guid') == row.concept_guid:
                range_min = range_min if range_min is not None else goal.get('range_min')
                range_max = range_max if range_max is not None else goal.get('range_max')
                break
    if range_min is not None or range_max is not None:
        ref_range = {}
        if range_min is not None:
            ref_range['low'] = _unit_fields({'value': float(range_min)}, unit_display, unit_code)
        if range_max is not None:
            ref_range['high'] = _unit_fields({'value': float(range_max)}, unit_display, unit_code)
        obs['referenceRange'] = [ref_range]

    # --- extension: pdhc-specific context ---
    extensions = []

    # Contract reference
    if row.contract_guid:
        extensions.append({
            'url': 'urn:pdhc:fhir:extension:contract',
            'valueReference': {
                'reference': f'https://contract.pdhc.se/fhir/Contract/{row.contract_guid}',
                'type': 'Contract',
                'identifier': {'value': row.contract_guid},
            }
        })

    # Requesting organization (from contract scope)
    req_org = (contract_scope.get('parties') or {}).get('requesting_org_guid')
    if req_org:
        extensions.append({
            'url': 'urn:pdhc:fhir:extension:requesting-organization',
            'valueReference': {
                'reference': f'https://sso.pdhc.se/api/organisations/{req_org}',
                'type': 'Organization',
                'display': 'Requesting Organization',
                'identifier': {'value': req_org},
            }
        })

    # Requester user
    req_user = sr_ctx.get('requester_user_guid')
    req_user_name = sr_ctx.get('requester_user_name')
    if req_user:
        ext = {
            'url': 'urn:pdhc:fhir:extension:requester',
            'valueReference': {
                'reference': f'Practitioner/{req_user}',
                'type': 'Practitioner',
            }
        }
        if req_user_name:
            ext['valueReference']['display'] = req_user_name
        extensions.append(ext)

    # Transaction/activity identifier
    tx_guid = raw.get('transaction_guid') or row.transaction_guid
    if tx_guid:
        extensions.append({
            'url': 'urn:pdhc:fhir:extension:transaction',
            'valueString': tx_guid,
        })
        extensions.append({
            'url': 'urn:pdhc:fhir:extension:transaction-url',
            'valueUrl': f'https://plan.pdhc.se/api/v1/concepts/{tx_guid}',
        })

    # Requirement type (required/optional)
    req_type = raw.get('requirement_type')
    if req_type:
        extensions.append({
            'url': 'urn:pdhc:fhir:extension:requirement-type',
            'valueCode': req_type,
        })

    # Goals from plan definition
    for goal in (sr_ctx.get('goals') or []):
        goal_concept_guid = goal.get('concept_guid', '')
        goal_ext = {
            'url': 'urn:pdhc:fhir:extension:goal',
            'extension': [
                {'url': 'concept', 'valueString': goal_concept_guid},
                {'url': 'concept_url', 'valueUrl': f'https://plan.pdhc.se/api/v1/concepts/{goal_concept_guid}'},
                {'url': 'description', 'valueString': goal.get('description', '')},
            ]
        }
        if goal.get('target_value') is not None:
            goal_ext['extension'].append({
                'url': 'target-value',
                'valueDecimal': float(goal['target_value']),
            })
        if goal.get('target_comparator'):
            goal_ext['extension'].append({
                'url': 'target-comparator',
                'valueString': goal['target_comparator'],
            })
        extensions.append(goal_ext)

    # Provider graph (rich visualization data from trusted providers)
    graph_type = raw.get('graph_type')
    graph_data = raw.get('graph_data')
    if graph_type and graph_data:
        graph_ext = {
            'url': 'urn:pdhc:fhir:extension:provider-graph',
            'extension': [
                {'url': 'graph-type', 'valueString': graph_type},
                {'url': 'graph-data', 'valueString': json.dumps(graph_data)},
            ]
        }
        graph_provider = raw.get('graph_provider')
        if graph_provider:
            graph_ext['extension'].append(
                {'url': 'graph-provider', 'valueString': graph_provider})
        graph_provider_url = raw.get('graph_provider_url')
        if graph_provider_url:
            graph_ext['extension'].append(
                {'url': 'graph-provider-url', 'valueUrl': graph_provider_url})
        extensions.append(graph_ext)

    # #500: carry the canonical typed observation as an extension so the openEHR
    # projection (rosetta, #501) reads the real typed value — not the lossy FHIR
    # value[x]. This rides the resource through cdr1 storage + analyse federation.
    extensions.append({
        'url': CANONICAL_OBS_EXT,
        'valueString': json.dumps(build_canonical_observation(row)),
    })

    if extensions:
        obs['extension'] = extensions

    return obs
