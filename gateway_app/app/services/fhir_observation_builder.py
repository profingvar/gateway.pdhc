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


def build_fhir_observation(row, sr_contexts=None, contract_scopes=None):
    """Build a fully referenced FHIR R5 Observation from an InboundObservation."""
    sr_contexts = sr_contexts or {}
    contract_scopes = contract_scopes or {}
    raw = row.fhir_observation_json or {}
    sr_ctx = sr_contexts.get(row.service_request_guid) or {}
    contract_scope = contract_scopes.get(row.contract_guid) or {}

    name = raw.get('concept_name') or ''
    unit = raw.get('unit_display') or raw.get('unit') or None
    recorded = raw.get('recorded_at')
    eff = recorded or (row.received_at.isoformat() if row.received_at else None)

    value = raw.get('value')
    if value is None:
        value = row.value
    try:
        value_num = float(value) if value is not None else None
    except (TypeError, ValueError):
        value_num = None

    obs = {
        'resourceType': 'Observation',
        'id': raw.get('id', row.guid),
        'status': 'final',
        'category': [{
            'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/observation-category',
                'code': 'laboratory',
                'display': 'Laboratory',
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

    # --- Value ---
    # Unit reference routes through plan.pdhc's unit catalog (platform
    # principle: only plan.pdhc emits external code-system refs).
    if value_num is not None:
        vq = {'value': value_num}
        if unit:
            vq['unit'] = unit
            vq['system'] = 'https://plan.pdhc.se/api/v1/lookup/units'
            vq['code'] = unit
        obs['valueQuantity'] = vq
    elif value is not None:
        obs['valueString'] = str(value)

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
            low = {'value': float(range_min)}
            if unit:
                low['unit'] = unit
                low['system'] = 'https://plan.pdhc.se/api/v1/lookup/units'
                low['code'] = unit
            ref_range['low'] = low
        if range_max is not None:
            high = {'value': float(range_max)}
            if unit:
                high['unit'] = unit
                high['system'] = 'https://plan.pdhc.se/api/v1/lookup/units'
                high['code'] = unit
            ref_range['high'] = high
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

    if extensions:
        obs['extension'] = extensions

    return obs
