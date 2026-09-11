"""
Rate Engine: computes complaint rates against exposure data and compares
them against a baseline rate and the demonstration review threshold. Pure
Python, deterministic -- no LLM calls, no network calls. Not one of the 5
pipeline agents (see docs/architecture_spec.md); it's the standalone stage
between Agent 4 (scope validation) and Agent 5 (document impact).

Non-negotiable (docs/architecture_spec.md): refuses to compute a rate on a
missing or zero denominator (units_distributed) rather than guessing --
returns an explicit can_compute=False result instead of a fabricated
number or a crash.

Also refuses when an exposure row is explicitly marked ineligible via
rate_eligible=false, even if units_distributed is a real, nonzero number.
This covers cases where a genuine public figure exists (e.g. an FDA
recall's "Quantity in Commerce") but does not share a coherent
observation period, exposure definition, or methodology with the
complaint numerator -- computing complaints/that-number would be a
fabricated rate dressed up in real inputs. See
data/README.md and the Ivenix LVP-0004 real-data package's
real_data_refusal_state.json for the concrete case this was built for:
14 complaints / 1,546 recalled units must never be presented as a 0.91%
complaint rate, because the 1,546 is a recall-scope quantity, not a
time-aligned exposure denominator.

This module only computes numbers and flags whether a rate exceeds the
threshold. It does NOT decide DISMISS / INVESTIGATE FURTHER / CONFIRM --
per docs/architecture_spec.md, that decision belongs to the mandatory
human decision gate downstream, and no agent or engine stage may bypass
it by pre-empting that call.

Expects complaints already scope-validated (Agent 4's output, in-scope
records) -- this module does not re-check product family/period/geography
scope itself.
"""

import csv
from dataclasses import dataclass
from datetime import date, datetime

DEMONSTRATION_THRESHOLD_PCT = 0.75
THRESHOLD_DISCLAIMER = (
    "This is a demonstration assumption for prototype purposes only -- "
    "it is not a clinical, statistical, or regulatory figure."
)


@dataclass
class RateResult:
    device_code: str
    period_start: str
    period_end: str
    complaint_count: int
    units_distributed: int | None
    rate_pct: float | None
    baseline_rate_pct: float | None
    exceeds_threshold: bool | None
    can_compute: bool
    reason: str


def _parse_iso_date(value: str) -> date | None:
    """Parse a YYYY-MM-DD date (the data contract's format for
    exposure/baseline periods -- see data/README.md). Returns None for
    blank/malformed values rather than raising."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_record_date(value: str) -> date | None:
    """Parse a MAUDE-style YYYYMMDD complaint date_received value."""
    if not value or len(value) != 8:
        return None
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return None


def compute_rate(
    device_code: str,
    period_start: str,
    period_end: str,
    complaint_count: int,
    units_distributed: int | None,
    baseline_rate_pct: float | None = None,
    threshold_pct: float = DEMONSTRATION_THRESHOLD_PCT,
    rate_eligible: bool | None = None,
    blocking_reason: str = "",
) -> RateResult:
    """Compute one device_code's complaint rate for one period.

    Refuses to compute (can_compute=False, rate_pct=None) if:
    - rate_eligible is explicitly False (checked first, regardless of
      whether units_distributed looks like a valid number) -- this covers
      real, nonzero figures that still can't be used as an exposure
      denominator for this numerator (see module docstring); or
    - units_distributed is missing or zero -- never substitutes a default
      or guessed denominator.
    """
    if rate_eligible is False:
        return RateResult(
            device_code,
            period_start,
            period_end,
            complaint_count,
            units_distributed,
            None,
            baseline_rate_pct,
            None,
            False,
            blocking_reason or "This exposure row is marked rate_eligible=false.",
        )
    if units_distributed is None:
        return RateResult(
            device_code,
            period_start,
            period_end,
            complaint_count,
            None,
            None,
            baseline_rate_pct,
            None,
            False,
            "No exposure data (units_distributed) found for this device_code and period.",
        )
    if units_distributed == 0:
        return RateResult(
            device_code,
            period_start,
            period_end,
            complaint_count,
            0,
            None,
            baseline_rate_pct,
            None,
            False,
            "units_distributed is 0 -- cannot compute a rate against a zero denominator.",
        )

    rate_pct = (complaint_count / units_distributed) * 100
    exceeds_threshold = rate_pct > threshold_pct
    reason = (
        f"{complaint_count} complaints / {units_distributed} units distributed = "
        f"{rate_pct:.4f}%. Demonstration threshold: {threshold_pct}%. {THRESHOLD_DISCLAIMER}"
    )

    return RateResult(
        device_code,
        period_start,
        period_end,
        complaint_count,
        units_distributed,
        rate_pct,
        baseline_rate_pct,
        exceeds_threshold,
        True,
        reason,
    )


def count_complaints_in_period(
    complaints: list[dict],
    device_code: str,
    period_start: date,
    period_end: date,
    device_code_field: str = "device_report_product_code",
    date_field: str = "date_received",
) -> int:
    """Count complaints matching a device_code whose date falls within
    [period_start, period_end]. Complaints with an unparseable/missing date
    are excluded from the count rather than guessed into or out of the
    period."""
    count = 0
    for complaint in complaints:
        if complaint.get(device_code_field, "") != device_code:
            continue
        received = _parse_record_date(complaint.get(date_field, ""))
        if received is not None and period_start <= received <= period_end:
            count += 1
    return count


def compute_rates_for_exposure(
    complaints: list[dict],
    exposure_rows: list[dict],
    baseline_rows: list[dict] | None = None,
    threshold_pct: float = DEMONSTRATION_THRESHOLD_PCT,
    device_code_field: str = "device_report_product_code",
    date_field: str = "date_received",
) -> list[RateResult]:
    """For each exposure row (one device_code + reporting period), count
    matching complaints and compute a rate. One RateResult per exposure
    row; an exposure row with an unparseable period always yields
    can_compute=False rather than being silently skipped.

    A device_code can now have multiple exposure rows (e.g. a synthetic
    demo period alongside a real-but-ineligible one) -- baseline rows are
    matched by device_code AND matching baseline_period_start/
    baseline_period_end to the exposure row's own period, not by
    device_code alone, so two periods for the same device never collide.
    A device_code with no period-matching baseline row gets baseline=None
    rather than an arbitrary guess.
    """
    baseline_by_device: dict[str, list[dict]] = {}
    for b in baseline_rows or []:
        baseline_by_device.setdefault(b.get("device_code", ""), []).append(b)

    def _matching_baseline(device_code: str, period_start_raw: str, period_end_raw: str) -> float | None:
        for b in baseline_by_device.get(device_code, []):
            if b.get("baseline_period_start") == period_start_raw and b.get("baseline_period_end") == period_end_raw:
                raw = b.get("baseline_rate_pct")
                return float(raw) if raw not in (None, "") else None
        return None

    def _parse_rate_eligible(raw: str) -> bool | None:
        if raw is None or raw == "":
            return None
        return raw.strip().lower() in ("true", "1", "yes")

    results = []
    for exp in exposure_rows:
        device_code = exp.get("device_code", "")
        period_start_raw = exp.get("period_start", "")
        period_end_raw = exp.get("period_end", "")
        period_start = _parse_iso_date(period_start_raw)
        period_end = _parse_iso_date(period_end_raw)

        units_raw = exp.get("units_distributed")
        units_distributed = int(units_raw) if units_raw not in (None, "") else None

        rate_eligible = _parse_rate_eligible(exp.get("rate_eligible", ""))
        blocking_reason = exp.get("blocking_reason", "")

        baseline = _matching_baseline(device_code, period_start_raw, period_end_raw)

        if period_start is None or period_end is None:
            results.append(
                RateResult(
                    device_code,
                    period_start_raw,
                    period_end_raw,
                    0,
                    units_distributed,
                    None,
                    baseline,
                    None,
                    False,
                    "Exposure row has an unparseable period_start/period_end (expected YYYY-MM-DD).",
                )
            )
            continue

        count = count_complaints_in_period(
            complaints, device_code, period_start, period_end, device_code_field, date_field
        )
        results.append(
            compute_rate(
                device_code,
                period_start_raw,
                period_end_raw,
                count,
                units_distributed,
                baseline,
                threshold_pct,
                rate_eligible,
                blocking_reason,
            )
        )

    return results


def load_csv_rows(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))
