import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rate_engine import compute_rate, compute_rates_for_exposure, count_complaints_in_period


def test_compute_rate_basic():
    result = compute_rate("FRN", "2024-01-01", "2024-01-31", complaint_count=5, units_distributed=1000)
    assert result.can_compute is True
    assert result.rate_pct == 0.5
    assert result.exceeds_threshold is False  # 0.5% < 0.75% demonstration threshold


def test_compute_rate_exceeds_threshold():
    result = compute_rate("FRN", "2024-01-01", "2024-01-31", complaint_count=10, units_distributed=1000)
    assert result.rate_pct == 1.0
    assert result.exceeds_threshold is True  # 1.0% > 0.75%


def test_compute_rate_refuses_missing_denominator():
    result = compute_rate("FRN", "2024-01-01", "2024-01-31", complaint_count=5, units_distributed=None)
    assert result.can_compute is False
    assert result.rate_pct is None
    assert "No exposure data" in result.reason


def test_compute_rate_refuses_zero_denominator():
    result = compute_rate("FRN", "2024-01-01", "2024-01-31", complaint_count=5, units_distributed=0)
    assert result.can_compute is False
    assert result.rate_pct is None
    assert "zero denominator" in result.reason


def test_compute_rate_zero_complaints_is_valid():
    result = compute_rate("FRN", "2024-01-01", "2024-01-31", complaint_count=0, units_distributed=1000)
    assert result.can_compute is True
    assert result.rate_pct == 0.0
    assert result.exceeds_threshold is False


def test_count_complaints_in_period_filters_by_device_code_and_date():
    from datetime import date

    complaints = [
        {"device_report_product_code": "FRN", "date_received": "20240115"},
        {"device_report_product_code": "FRN", "date_received": "20240201"},  # outside period
        {"device_report_product_code": "SKI", "date_received": "20240115"},  # wrong device
        {"device_report_product_code": "FRN", "date_received": ""},  # unparseable, excluded
    ]
    count = count_complaints_in_period(complaints, "FRN", date(2024, 1, 1), date(2024, 1, 31))
    assert count == 1


def test_compute_rates_for_exposure_end_to_end():
    complaints = [
        {"device_report_product_code": "FRN", "date_received": "20240110"},
        {"device_report_product_code": "FRN", "date_received": "20240120"},
        {"device_report_product_code": "FRN", "date_received": "20240301"},  # outside period
    ]
    exposure_rows = [
        {"device_code": "FRN", "period_start": "2024-01-01", "period_end": "2024-01-31", "units_distributed": "200"},
    ]
    baseline_rows = [
        {
            "device_code": "FRN",
            "baseline_rate_pct": "0.5",
            "baseline_period_start": "2024-01-01",
            "baseline_period_end": "2024-01-31",
        },
    ]

    results = compute_rates_for_exposure(complaints, exposure_rows, baseline_rows)
    assert len(results) == 1
    result = results[0]
    assert result.complaint_count == 2
    assert result.rate_pct == 1.0  # 2 / 200 * 100
    assert result.exceeds_threshold is True
    assert result.baseline_rate_pct == 0.5


def test_compute_rates_for_exposure_missing_units_never_crashes():
    exposure_rows = [
        {"device_code": "FRN", "period_start": "2024-01-01", "period_end": "2024-01-31", "units_distributed": ""},
    ]
    results = compute_rates_for_exposure([], exposure_rows)
    assert results[0].can_compute is False


def test_compute_rates_for_exposure_malformed_period_never_crashes():
    exposure_rows = [
        {"device_code": "FRN", "period_start": "not-a-date", "period_end": "2024-01-31", "units_distributed": "100"},
    ]
    results = compute_rates_for_exposure([], exposure_rows)
    assert results[0].can_compute is False


def test_compute_rate_refuses_when_marked_rate_ineligible():
    """The Ivenix LVP-0004 real-data case this was built for: a real,
    nonzero denominator (1,546 recalled units) that still must never be
    divided into a complaint count, because it's a recall-scope quantity,
    not a time-aligned exposure denominator."""
    result = compute_rate(
        "FRN",
        "2021-10-27",
        "2023-01-30",
        complaint_count=14,
        units_distributed=1546,
        rate_eligible=False,
        blocking_reason="Recall-scope quantity is not a time-aligned exposure denominator.",
    )
    assert result.can_compute is False
    assert result.rate_pct is None
    assert result.reason == "Recall-scope quantity is not a time-aligned exposure denominator."


def test_compute_rate_eligible_true_computes_normally():
    result = compute_rate(
        "FRN", "2024-01-01", "2024-12-31", complaint_count=20, units_distributed=2000, rate_eligible=True
    )
    assert result.can_compute is True
    assert result.rate_pct == 1.0


def test_compute_rates_for_exposure_respects_rate_eligible_column():
    exposure_rows = [
        {
            "device_code": "FRN",
            "period_start": "2021-10-27",
            "period_end": "2023-01-30",
            "units_distributed": "1546",
            "rate_eligible": "false",
            "blocking_reason": "Recall-scope quantity, not a time-aligned exposure denominator.",
        },
    ]
    results = compute_rates_for_exposure([], exposure_rows)
    assert results[0].can_compute is False
    assert results[0].units_distributed == 1546  # the real number is preserved, just not used to compute
    assert results[0].reason == "Recall-scope quantity, not a time-aligned exposure denominator."


def test_compute_rates_for_exposure_blank_rate_eligible_falls_back_to_normal_rules():
    """An exposure row with no rate_eligible column at all (or blank)
    should behave exactly as before this feature existed -- only the
    missing/zero denominator check applies."""
    exposure_rows = [
        {"device_code": "FRN", "period_start": "2024-01-01", "period_end": "2024-01-31", "units_distributed": "200"},
    ]
    complaints = [{"device_report_product_code": "FRN", "date_received": "20240110"}]
    results = compute_rates_for_exposure(complaints, exposure_rows)
    assert results[0].can_compute is True
    assert results[0].rate_pct == 0.5


def test_baseline_matched_by_period_not_just_device_code():
    """Two exposure rows for the same device_code but different periods
    must each get their own matching baseline, never an arbitrary one."""
    exposure_rows = [
        {"device_code": "FRN", "period_start": "2024-01-01", "period_end": "2024-01-31", "units_distributed": "200"},
        {"device_code": "FRN", "period_start": "2023-01-01", "period_end": "2023-01-31", "units_distributed": "100"},
    ]
    baseline_rows = [
        {
            "device_code": "FRN",
            "baseline_rate_pct": "0.4",
            "baseline_period_start": "2024-01-01",
            "baseline_period_end": "2024-01-31",
        },
        {
            "device_code": "FRN",
            "baseline_rate_pct": "0.6",
            "baseline_period_start": "2023-01-01",
            "baseline_period_end": "2023-01-31",
        },
    ]
    results = compute_rates_for_exposure([], exposure_rows, baseline_rows)
    by_period = {(r.period_start, r.period_end): r for r in results}
    assert by_period[("2024-01-01", "2024-01-31")].baseline_rate_pct == 0.4
    assert by_period[("2023-01-01", "2023-01-31")].baseline_rate_pct == 0.6
