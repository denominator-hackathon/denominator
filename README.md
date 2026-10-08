# Denominator

Medical Device Post-Market Safety System — a signal-detection pipeline that
ingests real FDA MAUDE adverse-event data for a medical device, cross-references
it against the FDA's official device registry and regulatory history, computes
a complaint rate against manufacturer-supplied exposure data, and drafts a
Safety Action Pack for human review — with a mandatory human decision gate
before anything reaches QMS.

This was built as a hackathon prototype. The pipeline, every agent, and the
rate engine are fully implemented and verified against live data. What's
*not* included is real manufacturer exposure data at scale (see
[Data: what's real vs. synthetic](#data-whats-real-vs-synthetic)) and the
human decision gate's own interface (the pipeline stops exactly where that
gate belongs and does not simulate it).

## The pipeline

```
Agent 1: MAUDE Ingestion
   fetches real adverse-event reports from openFDA (device/event), AI-categorizes
   each into a fixed problem-category taxonomy
        |
Agent 2: Product Identity
   matches each complaint's raw device name/manufacturer text to a canonical
   record in the FDA's GUDID device registry (openFDA device/udi mirror) --
   exact match, then fuzzy candidates, then AI disambiguation; "no match" is
   a valid, honest outcome
        |
Agent 3: Regulatory Context          Agent 4: Scope Validation
   openFDA recall/classification        deterministic check: right product
   history + PubMed literature,         family, right time period, right
   scoped to the categories Agent 1     geography -- every exclusion has an
   actually found                       explicit, auditable reason
        |                                      |
        +--------------------+-----------------+
                             |
                      Rate Engine
   complaint_count / units_distributed, compared against a baseline and a
   demonstration review threshold. Refuses to compute -- explicitly, with a
   reason -- on a missing/zero denominator, OR on a real nonzero denominator
   that doesn't share the numerator's time period, geography, or event
   category (see "Two scope-mismatch bugs" below)
                             |
                      Agent 5: Document Impact
   drafts a Safety Action Pack: plain-language summary, which existing QMS
   documents are relevant, suggested next steps -- phrased as suggestions,
   never a decision
                             |
                 Human Decision Gate  (not implemented here, by design)
         DISMISS | INVESTIGATE FURTHER | CONFIRM -- a person's call, always
                             |
                   Safety Action Pack --> QMS handoff (DRAFT only)
```

Run the whole thing with `python3 pipeline.py` (see [Running it](#running-it)).

## Non-negotiables

These are enforced in code, not just policy:

- **The human decision gate sits before any QMS action.** No agent, and no
  stage of `pipeline.py`, ever outputs DISMISS / INVESTIGATE FURTHER /
  CONFIRM. Every Safety Action Pack is stamped `status: DRAFT` and
  `human_decision_required: True` directly by the code, regardless of what
  an AI call returns.
- **The rate engine refuses rather than guesses.** A missing or zero
  `units_distributed` blocks computation. So does an exposure row explicitly
  marked `rate_eligible=false` -- even when the number itself is real and
  nonzero (see the Ivenix case below). A mismatched event category is
  filtered out rather than silently inflating the count.
- **Real data and synthetic/demo data are never mixed silently.** Every
  exposure/baseline/document row carries a `data_status` field saying which
  one it is.
- **The 0.75% demonstration threshold is not a real regulatory figure.**
  Its disclaimer is reproduced verbatim in every Safety Action Pack, not
  left to an LLM to paraphrase or drop.

## Two scope-mismatch bugs found and fixed during testing

Both were caught by running the pipeline against real data, not by
inspection -- worth documenting because the fixes generalize beyond this
project:

1. **Period/geography mismatch.** An earlier version took the complaint
   fetch window as an independent CLI argument, separate from whatever
   period an exposure row declared. A real run fetched one week of
   complaints and divided it against a denominator scoped to a full year --
   a number that looked plausible but was quietly wrong. Fixed by making the
   manufacturer's exposure declaration (device, period, region, units) the
   *primary* input: the complaint fetch window is now always derived from
   it, never a separate parameter that could disagree.
2. **Event-category mismatch.** Even after fixing #1, a full-year run
   produced an impossible 1,894% complaint rate. The exposure fixture's
   2,000-unit denominator was only ever scoped to one failure type
   ("Failure to Infuse"), but the numerator counted every complaint type.
   Fixed with an opt-in `event_category` column on exposure rows that
   filters the complaint count to match the denominator's actual scope.
   Dropped the rate from 1,894% to 91.1% -- still high, but no longer
   mathematically impossible.

## Data: what's real vs. synthetic

`data/exposure.csv` currently holds two rows, deliberately kept side by
side as a demonstration of both states the rate engine needs to handle
correctly:

| Row | Status | What happens |
|---|---|---|
| FRN, 2024, 2,000 units | `synthetic_demo_only` | Computes normally -- this is a reproducibility fixture, not a real sales/installed-base figure |
| FRN (Ivenix LVP-0004), 2021-2023, 1,546 units | `real_public_official` | **Refuses to compute.** The 1,546 is a real FDA recall "Quantity in Commerce" figure, but it isn't a time-aligned exposure denominator -- see `data/README.md` and `docs/scope_lock.md` for the full reasoning this refusal is based on |

`data/document_map.csv` similarly holds both internal QMS document
placeholders and real external FDA references (recall record, safety
communication, 510(k)s) -- kept in strictly separate columns
(`document_class`) so Agent 5 can never present one as the other.

## Structure

- `agents/` -- `DenominatorAgent` base class + all 5 pipeline agents, each
  with its own client/logic module (`maude_client.py`, `gudid_client.py`,
  `problem_categorizer.py`, `product_identity_matcher.py`,
  `regulatory_context_client.py`, `scope_validator.py`,
  `document_impact_generator.py`), plus shared `openfda_client.py` and
  `llm_client.py`
- `rate_engine.py` -- pure Python, deterministic, the standalone stage
  between Agent 4 and Agent 5
- `pipeline.py` -- orchestrates all 6 stages end-to-end; CLI supports both
  batch mode (evaluate every exposure.csv row) and an ad-hoc single-entry
  mode
- `scripts/fetch_maude_events.py` -- standalone CLI for pulling raw MAUDE
  data to CSV, independent of the agent pipeline
- `config/` -- `CacheManager` (disk cache; every LLM call is cached so a
  repeat run costs nothing) and `token_counter.py` (budget tracker)
- `data/` -- data contract CSVs + full schema docs in `data/README.md`
- `docs/` -- `architecture_spec.md`, `scope_lock.md` (locked product scope
  and the reasoning behind every correction made to it)
- `tests/` -- unit tests (20 passing; mainly `rate_engine.py`'s refusal
  logic, since that's the most safety-critical and most heavily tested
  path)

## Running it

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

(A `Dockerfile`/`docker-compose.yml` are also available: `docker compose build`.)

Set your OpenAI key (used for Agents 1, 2, and 5's AI calls):

```bash
export OPENAI_API_KEY="sk-..."
```

`OPENFDA_API_KEY` is optional -- raises the anonymous rate limit on openFDA
calls but isn't required for a run to work.

Run the full pipeline (batch mode -- evaluates every exposure.csv row,
each scoped to its own declared period):

```bash
python3 pipeline.py
```

Or run one ad-hoc manufacturer entry directly, without touching the CSV:

```bash
python3 pipeline.py --period-start 2024-01-01 --period-end 2024-01-07 --units-distributed 2000
```

Run the tests:

```bash
pytest tests/ -v
```

## Budget

147,000 tokens (21,000 x 7 people via Manus). Only 3 planned LLM calls
(Agents #1, #2, #5), on OpenAI `gpt-4o-mini` (~$0.002 total estimated,
batched to minimize call count) -- everything else is free public APIs or
pure Python. See `config/token_counter.py`.
