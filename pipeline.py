"""
Pipeline orchestrator: chains all 6 stages (Agents 1-5 + Rate Engine) into
one run, passing each stage's output into the next.

Restructured (2026-09-12): the manufacturer's exposure declaration --
device, period, region, units sold -- is now the PRIMARY input that drives
everything else, not a separate parameter that happened to exist alongside
an independently-chosen date range. Complaints are always fetched for
exactly the period and geography the exposure entry declares.

Why: an earlier version took product_code/start_date/end_date/geography as
independent CLI arguments, separate from whatever exposure rows existed in
data/exposure.csv. A real run showed the gap this leaves open: complaints
were fetched for one week while an exposure row declared a whole year as
its period, and the two got divided against each other anyway -- a
mismatched-scope rate that looked like a real number. Deriving the fetch
window from the exposure entry itself makes that particular mismatch
structurally impossible: there is no second date range to disagree with.

This does NOT by itself fix every scope-mismatch risk (e.g. an exposure
entry scoped to one complaint category like "Failure to Infuse" being
compared against complaints of every category) -- see rate_engine.py's
rate_eligible/blocking_reason gate for the other class of mismatch this
project has addressed so far, and docs/architecture_spec.md for what's
still open.

Stops cleanly -- returns a PipelineResult with halted_at/halt_reason set,
never crashes or silently passes bad data downstream -- the moment any
stage can't produce usable output: an agent reports failure, a fetch
returns zero records, or the rate engine refuses the exposure entry.

This is intentionally NOT the human decision gate. It stops right after
Agent 5 produces a DRAFT Safety Action Pack. Per docs/architecture_spec.md,
nothing past that point may happen without a human explicitly reviewing
and deciding DISMISS / INVESTIGATE FURTHER / CONFIRM -- no code here (or
anywhere else in this pipeline) makes that call.
"""

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents.agent1_maude_ingestion import MaudeIngestionAgent
from agents.agent2_product_identity import ProductIdentityAgent
from agents.agent3_regulatory_context import RegulatoryContextAgent
from agents.agent4_scope_validation import ScopeValidationAgent
from agents.agent5_document_impact import DocumentImpactAgent
from config.cache_manager import CacheManager
from rate_engine import compute_rates_for_exposure, load_csv_rows

DATA_DIR = Path(__file__).parent / "data"


@dataclass
class PipelineResult:
    product_code: str
    start_date: str
    end_date: str
    stage_results: dict[str, Any] = field(default_factory=dict)
    halted_at: str | None = None
    halt_reason: str | None = None

    @property
    def completed(self) -> bool:
        return self.halted_at is None


def _halt(result: PipelineResult, stage: str, reason: str) -> PipelineResult:
    result.halted_at = stage
    result.halt_reason = reason
    return result


def load_data_contracts(product_code: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Load exposure/baseline/document_map rows for this product_code only
    (these CSVs may hold multiple device_codes). A missing or empty file
    is not an error here -- it's surfaced as a halt reason by the stage
    that actually needs the data, so the message is specific rather than
    a generic file-not-found."""

    def _safe_load(name: str) -> list[dict]:
        path = DATA_DIR / name
        if not path.exists():
            return []
        return load_csv_rows(str(path))

    exposure = [r for r in _safe_load("exposure.csv") if r.get("device_code") == product_code]
    baseline = [r for r in _safe_load("baseline.csv") if r.get("device_code") == product_code]
    document_map = [r for r in _safe_load("document_map.csv") if r.get("device_code") == product_code]
    return exposure, baseline, document_map


def _iso_to_yyyymmdd(iso_date: str) -> str:
    return iso_date.replace("-", "")


def run_pipeline_for_entry(entry: dict, cache_dir: str = "cache") -> PipelineResult:
    """Run the full pipeline for exactly one exposure entry -- one
    manufacturer's declaration of {device_code, period_start, period_end,
    geography, units_distributed[, units_in_field, rate_eligible,
    blocking_reason]}.

    The complaint fetch (Agent 1) and scope validation (Agent 4) are always
    scoped to this entry's own period_start/period_end and geography --
    there is no separate, independently-chosen date range that could
    disagree with it.
    """
    product_code = entry["device_code"]
    period_start_iso = entry["period_start"]
    period_end_iso = entry["period_end"]
    geography = entry.get("geography", "US")
    start_date = _iso_to_yyyymmdd(period_start_iso)
    end_date = _iso_to_yyyymmdd(period_end_iso)

    result = PipelineResult(product_code=product_code, start_date=start_date, end_date=end_date)
    cache = CacheManager(cache_dir=cache_dir)

    r1 = MaudeIngestionAgent(cache_manager=cache).run(
        {"product_code": product_code, "start_date": start_date, "end_date": end_date}
    )
    result.stage_results["agent1"] = r1
    if not r1.success:
        return _halt(result, "agent1", r1.notes)
    if not r1.output.get("records"):
        return _halt(result, "agent1", "No MAUDE records fetched for this exposure entry's product_code/period.")

    r2 = ProductIdentityAgent(cache_manager=cache).run(
        {"records": r1.output["records"], "product_code": product_code}
    )
    result.stage_results["agent2"] = r2
    if not r2.success:
        return _halt(result, "agent2", r2.notes)

    categories = sorted(
        {r.get("problem_category", "") for r in r2.output["records"] if r.get("problem_category")}
    )
    r3 = RegulatoryContextAgent(cache_manager=cache).run(
        {"product_code": product_code, "problem_categories": categories}
    )
    result.stage_results["agent3"] = r3
    if not r3.success:
        return _halt(result, "agent3", r3.notes)

    r4 = ScopeValidationAgent(cache_manager=cache).run(
        {
            "records": r2.output["records"],
            "start_date": start_date,
            "end_date": end_date,
            "product_code": product_code,
            "geography": geography,
        }
    )
    result.stage_results["agent4"] = r4
    if not r4.success:
        return _halt(result, "agent4", r4.notes)

    in_scope_records = [r for r in r4.output["records"] if r["in_scope"]]

    _, baseline_rows, document_map_rows = load_data_contracts(product_code)

    rate_result = compute_rates_for_exposure(in_scope_records, [entry], baseline_rows)[0]
    result.stage_results["rate_engine"] = [rate_result]
    if not rate_result.can_compute:
        return _halt(result, "rate_engine", rate_result.reason)

    pack = DocumentImpactAgent(cache_manager=cache).run(
        {
            "rate_result": rate_result,
            "device_code": product_code,
            "regulatory_context": r3.output,
            "records": in_scope_records,
            "document_map_rows": document_map_rows,
        }
    )
    result.stage_results["agent5"] = [pack]
    if not pack.success:
        return _halt(result, "agent5", pack.notes)

    return result


def run_pipeline_for_product(product_code: str = "FRN", cache_dir: str = "cache") -> list[PipelineResult]:
    """Batch mode: run one full pipeline pass per existing data/exposure.csv
    row for this product_code, each scoped to its own declared
    period/geography -- i.e. (re-)evaluate every manufacturer submission
    already on file for this device."""
    exposure_rows, _, _ = load_data_contracts(product_code)
    if not exposure_rows:
        empty_result = PipelineResult(product_code=product_code, start_date="", end_date="")
        return [
            _halt(
                empty_result,
                "rate_engine",
                f"data/exposure.csv has no rows for device_code={product_code} -- cannot run without at "
                "least one manufacturer exposure declaration. Add a row or pass --period-start/--period-end/"
                "--units-distributed directly for a one-off entry.",
            )
        ]
    return [run_pipeline_for_entry(row, cache_dir=cache_dir) for row in exposure_rows]


def _print_result(result: PipelineResult) -> None:
    print(f"Pipeline run: product_code={result.product_code}, period={result.start_date} to {result.end_date}")
    print("=" * 60)
    for stage, stage_result in result.stage_results.items():
        items = stage_result if isinstance(stage_result, list) else [stage_result]
        for item in items:
            print(f"[{stage}] {getattr(item, 'notes', item)}")

    print("=" * 60)
    if result.completed:
        print("Pipeline completed. Draft Safety Action Pack(s) ready for human review.")
        print("NOTE: no DISMISS/INVESTIGATE FURTHER/CONFIRM decision has been made -- that requires a human.")
    else:
        print(f"Pipeline halted at stage '{result.halted_at}': {result.halt_reason}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Run the Denominator pipeline. Default (batch) mode (re-)evaluates every "
        "existing data/exposure.csv row for --product-code. Pass --period-start/--period-end/"
        "--units-distributed together to instead run one ad-hoc manufacturer exposure entry, "
        "entered live rather than read from the CSV."
    )
    parser.add_argument("--product-code", default="FRN", help="Used in batch mode to select which exposure.csv rows to process.")
    parser.add_argument("--device-code", default=None, help="Ad-hoc mode only; defaults to --product-code.")
    parser.add_argument("--period-start", default=None, help="Ad-hoc mode: manufacturer-declared exposure period start, YYYY-MM-DD.")
    parser.add_argument("--period-end", default=None, help="Ad-hoc mode: manufacturer-declared exposure period end, YYYY-MM-DD.")
    parser.add_argument("--geography", default="US", help="Ad-hoc mode only.")
    parser.add_argument("--units-distributed", type=int, default=None, help="Ad-hoc mode: units sold/distributed in the period.")
    parser.add_argument("--units-in-field", type=int, default=None, help="Ad-hoc mode, optional.")
    parser.add_argument("--cache-dir", default="cache")
    args = parser.parse_args()

    ad_hoc = args.period_start and args.period_end and args.units_distributed is not None
    if ad_hoc:
        entry = {
            "device_code": args.device_code or args.product_code,
            "period_start": args.period_start,
            "period_end": args.period_end,
            "geography": args.geography,
            "units_distributed": str(args.units_distributed),
            "units_in_field": str(args.units_in_field) if args.units_in_field is not None else "",
        }
        results = [run_pipeline_for_entry(entry, cache_dir=args.cache_dir)]
    else:
        results = run_pipeline_for_product(args.product_code, cache_dir=args.cache_dir)

    for result in results:
        _print_result(result)


if __name__ == "__main__":
    main()
