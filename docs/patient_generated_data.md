# Patient-generated data (#1014)

For anyone building a patient-facing client, such as the patient app, against
gateway.pdhc. A patient app has the same architecture as provider1: it
authenticates with a PAT and submits reports to
`POST /api/v1/provider/report/<service_request_guid>`.

## The rule

Patient-generated data (PGD) has a different legal status from data that is
produced or endorsed by healthcare, and the professional has to see which is
which. The platform records this as `data_origin`:

| value | meaning |
|---|---|
| `"patient_generated"` | registered by the patient |
| absent / null | produced or endorsed by healthcare (opt-in policy, operator 2026-11-21) |

Any other value is refused with **422** at gateway, and again at the CDR.
Nothing is stored.

## How a patient app gets it right: its PAT carries the scope

Issue the patient app's PAT with the extra scope `patient_generated`
(request.pdhc):

    flask provider register-pat --org-guid <org> --contract-guid <contract> \
        --scopes read,write,patient_generated

Gateway then sets `data_origin = "patient_generated"` on **every** observation
the PAT submits, questionnaire answers included. The body can neither omit
nor change it, the same way `provider_org_guid` comes from the PAT and never
from the body. A rotated PAT keeps its scopes (`pat_service.py`).

**The app does not need to send anything.** If it does send `data_origin`, the
only accepted value is `"patient_generated"`.

## Healthcare clients (opt-in)

A healthcare PAT, such as provider1's, may still flag individual data as
patient-generated: put `data_origin` on the report body, or on a single
observation (the observation value wins). Leave it out and the data is healthcare data.

## Where it shows up

- **gateway:** `cdr_delivery_log.data_origin`. The FHIR Observation carries
  `meta.tag` `urn:pdhc:fhir:CodeSystem:data-origin|patient-generated` and the
  extension `urn:pdhc:fhir:extension:data-origin`. `performer` still names
  the submitter.
- **cdr1/cdr2/cdr3:** `clinical_context.data_origin` and the column on each
  per-type row. Returned per point by `/api/v1/clinical/patient/<g>/series`
  and in `/api/v1/clinical/observation/<g>`.
- **dashboard:** a "Patient" badge in the event list, an "Ursprung" row and a
  note in the detail panel, hollow points in the chart (explained in the
  legend), and a `data_origin` column (last) in the CSV export.

Not yet: analyse does not stratify by origin.
