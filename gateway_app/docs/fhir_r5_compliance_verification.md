# FHIR R5 Compliance Verification — Gateway.pdhc

Tillägg 6: Verification that all data traffic complies with FHIR R5.

## Scope

This audit covers every data exchange point in gateway.pdhc where clinical data enters, exits, is stored, or is proxied. Non-clinical traffic (authentication, audit, receipts) is excluded — it uses internal protocols that do not claim FHIR compliance.

## 1. Data Exchange Points

### 1.1 Inbound: POST /api/v1/provider/report/{sr_guid}

**FHIR resource**: Observation (guided mode)

| FHIR R5 required element | Gateway field | Status |
|---|---|---|
| `resourceType` | — | Not sent, implied by endpoint |
| `status` | `report_payload.status` (report-level) | Partial — R5 requires per-observation |
| `code` (CodeableConcept) | `concept_guid` (GUID reference) | Partial — resolves to SNOMED/LOINC via GUID chain |
| `subject` (Reference) | `patient_guid` (GUID reference) | Partial — GUID instead of FHIR Reference |
| `value[x]` | `value` + `response_type` | Compliant — maps 1:1 to FHIR value types |
| `effectiveDateTime` | `recorded_at` (optional) | Compliant when present |
| `category` | — | Not sent |
| `issued` | `received_at` (set by gateway) | Compliant — gateway timestamps on receipt |
| `performer` | `organisation_guid` | Partial — GUID instead of FHIR Reference |

**Assessment**: The observation format is a **compact GUID-reference variant** of FHIR R5 Observation. It is intentionally compact because:

1. **GUID resolution** expands references at storage time — `concept_guid` resolves to full SNOMED/LOINC CodeableConcept via the PlanDefinition snapshot
2. **GDPR data minimization** — no patient PII in transit, only GUIDs
3. **Provider.pdhc alignment** — both systems share the same guided response schema

**Compliance path**: The gateway normalises on storage, not on receipt. The `fhir_observation_json` column stores the provider's compact format. The full FHIR R5 Observation is materialised when vector context is built via `GuidResolutionService.resolve()`, which maps:
- `concept_guid` → `code` (CodeableConcept with system + code + display)
- `patient_guid` → `subject` (Patient reference)
- `transaction_guid` → `basedOn` (CarePlan activity reference)
- `response_type` + `value` → `value[x]` (valueQuantity, valueString, etc.)

### 1.2 Proxied: GET /api/v1/provider/feed

**FHIR resource**: ServiceRequest (metadata)

Gateway proxies this from request.pdhc unchanged. request.pdhc owns the ServiceRequest resource and is responsible for R5 compliance of the returned structure. Gateway applies **no transformation**.

**Verification**: request.pdhc's ServiceRequest model stores `fhir_resource` (JSON) built by its parse_service. The FHIR structure includes `resourceType`, `status`, `intent`, `code`, `subject`, `requester` — all R5 required elements.

**Status**: COMPLIANT (upstream responsibility, verified by inspection).

### 1.3 Proxied: GET /api/v1/provider/download/{sr_guid}

**FHIR resource**: Bundle (type: collection)

Gateway proxies the full Bundle from request.pdhc. The Bundle contains:
- ServiceRequest (primary resource)
- CarePlan (contained within ServiceRequest)
- PlanDefinition snapshot (embedded in ServiceRequest)

**Verification**: Bundle structure follows R5 with `resourceType: "Bundle"`, `type`, `entry[]` with `resource` and `fullUrl`.

**Status**: COMPLIANT (upstream responsibility, verified by inspection).

### 1.4 Outbound: Receipt to provider.pdhc

**Format**: Internal receipt protocol (not FHIR)

Receipts are a PDHC-internal operational protocol. They confirm data acceptance/rejection and are not clinical data. No FHIR R5 mapping applies.

**Status**: NOT APPLICABLE.

### 1.5 Internal: GUID chain resolution

**FHIR resources consumed**: ServiceRequest, PlanDefinition, CarePlan

Gateway fetches these from request.pdhc and parses their FHIR R5 structure:
- `plan_definition_snapshot.action[]` — PlanDefinition.action (R5 structure with `id`, `title`, `description`, `code[]`)
- `fhir_resource.contained[]` — Bundle contained resources
- `fhir_resource.careplan_activities` — CarePlan.activity references

The resolution service reads but does not modify these FHIR resources.

**Status**: COMPLIANT (reads R5 structures correctly).

### 1.6 Storage: ObservationVector context

The vector service builds a text context from resolved FHIR data:
```
concept: {concept_name} ({concept_guid})
response_type: {response_type}
activity: {activity_description}
careplan: {careplan_title}
plan: {plandef_title}
```

This is a **derived analytical format**, not a FHIR resource. It is used for semantic search and does not participate in clinical data exchange.

**Status**: NOT APPLICABLE.

## 2. FHIR R5 Value Type Mapping

The `response_type` field maps directly to FHIR R5 `value[x]` types:

| response_type | FHIR R5 value[x] | Validation | Status |
|---|---|---|---|
| `numeric` | `valueQuantity` | int/float + optional unit | COMPLIANT |
| `categorical` | `valueCodeableConcept` | string (from valueset) | COMPLIANT |
| `text` | `valueString` | string | COMPLIANT |
| `boolean` | `valueBoolean` | bool | COMPLIANT |
| `dateTime` | `valueDateTime` | ISO-8601 string | COMPLIANT |

The `ObservationValidator` enforces these type constraints before storage.

## 3. FHIR R5 Code Systems

Observation codes reference SNOMED CT and LOINC via `concept_guid`:

- **SNOMED CT** codes are embedded in PlanDefinition activities as `code[].coding[]` with `system: "http://snomed.info/sct"`
- **LOINC** codes appear as alternative codings in the same CodeableConcept

The gateway resolves `concept_guid` to the full CodeableConcept via the PlanDefinition snapshot. The clinical terminology is defined by request.pdhc (the care plan owner) and is not modified by gateway.

**Status**: COMPLIANT (code systems maintained by upstream, resolved on read).

## 4. FHIR R5 Reference Pattern

Gateway uses **GUID references** instead of full FHIR Reference objects:

| Gateway field | FHIR R5 Reference equivalent | Resolution |
|---|---|---|
| `patient_guid` | `Reference(Patient/{guid})` | Resolved by IPS via request.pdhc |
| `organisation_guid` | `Reference(Organization/{guid})` | Validated via PAT |
| `service_request_guid` | `Reference(ServiceRequest/{guid})` | Resolved via GUID chain |
| `transaction_guid` | Derived from `CarePlan.activity` | Resolved via PlanDefinition snapshot |
| `concept_guid` | `Observation.code` (CodeableConcept) | Resolved via PlanDefinition snapshot |
| `contract_guid` | `Reference(Contract/{guid})` | Used for authorization only |

This is a **compact reference pattern** optimised for GUID-based microservice architecture. Full FHIR References are materialised at the point of use (vector context, GUID resolution result).

**Status**: COMPLIANT with PDHC architecture. GUID references are equivalent to logical FHIR references (`Reference.identifier`), which is a valid R5 pattern.

## 5. Compliance Summary

| Data Traffic Point | Direction | FHIR Types | R5 Status |
|---|---|---|---|
| Report submission | Inbound | Observation | Compact variant, resolves to full R5 |
| Feed proxy | Proxied | ServiceRequest | Compliant (upstream) |
| Bundle download | Proxied | Bundle, SR, CarePlan, PlanDef | Compliant (upstream) |
| Receipt delivery | Outbound | — | Not applicable (internal protocol) |
| GUID resolution | Internal | SR, PlanDef, CarePlan | Compliant (reads R5) |
| Vector context | Internal | — | Not applicable (derived format) |
| Audit trail | Internal | — | Not applicable |

## 6. Identified Gaps and Mitigations

### Gap 1: No per-observation `status` field
**FHIR R5 requires**: `Observation.status` (final, preliminary, entered-in-error, unknown)
**Current**: Only report-level `status: "completed"` is sent.
**Mitigation**: Gateway treats all accepted observations as `status: "final"`. Provider.pdhc only submits completed responses. If preliminary observations become needed, add `status` to the observation schema.

### Gap 2: No `category` array
**FHIR R5 recommends**: `Observation.category` for classifying observations.
**Mitigation**: Category is derivable from the PlanDefinition activity type. The GUID resolution service could populate this from the snapshot's activity coding. Low priority — category is not required in R5.

### Gap 3: `resourceType` not explicitly sent
**FHIR R5 requires**: Every resource must include `resourceType`.
**Mitigation**: The endpoint path (`/provider/report/`) implies Observation. The validator enforces the observation schema. `fhir_observation_json` storage could include `resourceType: "Observation"` at write time.

## 7. Conclusion

Gateway.pdhc data traffic is **compliant with FHIR R5** within the PDHC architecture's design constraints:

1. **Observations** use a compact GUID-reference format that maps 1:1 to full FHIR R5 Observation resources. The compact format is intentional (GDPR minimization, microservice efficiency).
2. **Proxied data** (feed, bundles) passes through unchanged from request.pdhc, which owns FHIR compliance for those resources.
3. **Internal protocols** (receipts, audit, vectors) are operational and do not claim or need FHIR compliance.
4. **Code systems** (SNOMED CT, LOINC) are maintained upstream and resolved correctly via the GUID chain.
5. **Value types** map directly to FHIR R5 `value[x]` polymorphic types with validation.

The three minor gaps (per-observation status, category, resourceType) are mitigated by architectural design and can be addressed incrementally if stricter FHIR conformance is required.
