# Data Contracts

Four CSV schemas shared across all agents. `device_code` is the join key
across all four files, matching the openFDA `device_report_product_code`
used throughout the pipeline (`FRN` — see `docs/scope_lock.md`, corrected
from an earlier SKI/SKJ/SKL placeholder).

Every row must be traceable: real data and synthetic/demo data are never
mixed silently. Use `data_status` on every row to say which one it is
(e.g. `synthetic_demo_only`, `real_public_official`) — see
`docs/scope_lock.md` and the Ivenix LVP-0004 real-data package this
schema was extended to support.

## complaints.csv

Not currently read by the pipeline — Agent 1 (`agents/agent1_maude_ingestion.py`)
fetches complaints live from openFDA instead. This file remains as a
documented contract for a future path that ingests complaints from a
file rather than a live API, but populating it has no effect on
`pipeline.py` today.

| column | type | notes |
|---|---|---|
| complaint_id | string | unique per complaint |
| device_code | string | joins to the other three files |
| event_date | date (YYYY-MM-DD) | |
| event_type | string | e.g. malfunction, injury, death |
| narrative | string | free-text complaint description |
| source | string | e.g. MAUDE |

## exposure.csv

Read by `rate_engine.compute_rates_for_exposure()` via `pipeline.py`.
**A device_code may have more than one row** (e.g. a synthetic demo
period alongside a real-but-ineligible one) — each row is matched to
complaints by its own `period_start`/`period_end`, never merged.

| column | type | notes |
|---|---|---|
| device_code | string | joins to complaints.device_code |
| period_start | date (YYYY-MM-DD) | |
| period_end | date (YYYY-MM-DD) | |
| units_distributed | int | denominator input |
| units_in_field | int | optional, not used in the rate calculation itself |
| rate_eligible | `true`/`false`/blank | **optional.** If `false`, the rate engine refuses to compute a rate for this row regardless of whether units_distributed looks like a valid number — see `blocking_reason`. Blank/absent falls back to the plain missing-or-zero-denominator check. |
| blocking_reason | string | **required if rate_eligible=false.** Why this real-looking number still can't be used as an exposure denominator (e.g. a recall's "Quantity in Commerce" isn't a time-aligned shipment/installed-base figure). Becomes the RateResult's `reason` field verbatim. |
| data_status | string | e.g. `synthetic_demo_only`, `real_public_official`. Not read by code — documentation/audit trail only. |
| source | string | citation for where the number came from. Not read by code — documentation/audit trail only. |

## baseline.csv

Read by `rate_engine.compute_rates_for_exposure()`. A baseline row is
matched to an exposure row by **device_code AND matching
baseline_period_start/baseline_period_end to the exposure row's own
period** — not by device_code alone, since a device_code can now have
multiple periods. A device_code/period combination with no matching
baseline row gets `baseline_rate_pct=None` in the result rather than an
arbitrary guess.

| column | type | notes |
|---|---|---|
| device_code | string | |
| baseline_rate_pct | float | historical complaint rate, %. Leave blank if no authorised comparable baseline exists — never fabricate one. |
| baseline_period_start | date (YYYY-MM-DD) | must match the exposure row's period_start exactly to be used |
| baseline_period_end | date (YYYY-MM-DD) | must match the exposure row's period_end exactly to be used |
| source | string | |
| data_status | string | e.g. `synthetic_demo_assumption`, `real_public_recall_context_not_rate_baseline`. Documentation only. |
| limitations | string | free text on why this isn't (or is) a valid rate baseline. Documentation only. |

## document_map.csv

| column | type | notes |
|---|---|---|
| document_id | string | |
| document_type | string | one of: CAPA, PMS/PSUR, SOP, Risk Management File, External Reference |
| device_code | string | |
| qms_reference | string | QMS document identifier |
| status | string | DRAFT only — never auto-published |
| document_class | string | `internal_qms_document` or `public_external_regulatory_reference`. **Load-bearing** — Agent 5 (`agents/document_impact_generator.py`) must never present an external regulatory reference (an FDA recall record, safety communication, 510(k), etc.) as if it were one of the manufacturer's own internal QMS records. |
| data_status | string | e.g. `synthetic_template`, `real_public_official`. Documentation only. |
| source_url | string | for `public_external_regulatory_reference` rows, the public FDA source. Blank for internal placeholders. |
| notes | string | free text — owner, due date, rationale, or limitations. Documentation only. |

## Validation rules
- `device_code` must exist consistently across files that reference it.
- Dates must be ISO 8601 (`YYYY-MM-DD`).
- Rate calculations (complaints / exposure * 100) must never divide by a
  missing or zero denominator, and must never compute against an
  exposure row marked `rate_eligible=false` — the rate engine refuses
  the calculation instead of guessing either way.
- `document_map.status` must always start as `DRAFT`; nothing in this
  pipeline auto-publishes to QMS.
- `document_map.document_class=public_external_regulatory_reference` rows
  must never be described as internal CAPA/PMS/PSUR/SOP/risk-management
  records — they're external evidence a reviewer might cross-reference,
  not the manufacturer's own controlled documents.
