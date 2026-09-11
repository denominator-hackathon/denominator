"""
Generic openFDA HTTP client shared by every openFDA-backed data source in
this project (device/event via maude_client.py, device/udi via
gudid_client.py, and any future endpoint). Keeps retry/backoff, rate-limit
budgeting, and the anonymous-vs-keyed page-size quirk in one place.
"""

import re
import sys
import time

import requests

PAGE_LIMIT_WITH_KEY = 1000  # openFDA max records per request
PAGE_LIMIT_NO_KEY = 999  # some openFDA endpoints (confirmed: device/event) reject
# anonymous requests at exactly limit=1000 with HTTP 403 API_KEY_MISSING.
REQUEST_DELAY_SECONDS = 0.3
DAILY_LIMIT_NO_KEY = 1000
DAILY_LIMIT_WITH_KEY = 120000


class RequestBudget:
    """Tracks how many openFDA requests we've made this run and warns before
    we run the account out of its daily allowance."""

    def __init__(self, has_api_key: bool):
        self.count = 0
        self.has_api_key = has_api_key
        self.daily_limit = DAILY_LIMIT_WITH_KEY if has_api_key else DAILY_LIMIT_NO_KEY
        self._warned = False

    def record(self):
        self.count += 1
        if not self._warned and self.count >= int(self.daily_limit * 0.9):
            self._warned = True
            print(
                f"WARNING: {self.count} openFDA requests made this run, "
                f"approaching the {self.daily_limit}/day limit for "
                f"{'an API key' if self.has_api_key else 'anonymous access'}. "
                f"{'Set OPENFDA_API_KEY to raise this limit.' if not self.has_api_key else ''}",
                file=sys.stderr,
            )


def page_limit(api_key: str | None) -> int:
    return PAGE_LIMIT_WITH_KEY if api_key else PAGE_LIMIT_NO_KEY


def _raise_with_openfda_error(response: requests.Response) -> None:
    """requests' default raise_for_status() only says '403 Client Error:
    Forbidden' -- it drops openFDA's actual JSON error body, which is the
    part that says *why* (e.g. API_KEY_INVALID vs API_KEY_MISSING vs
    rate-limit). Surface that instead so a failure is actually
    diagnosable from the traceback alone."""
    try:
        detail = response.json().get("error", {})
        message = f"{detail.get('code', 'UNKNOWN')}: {detail.get('message', response.text[:300])}"
    except (ValueError, AttributeError):
        message = response.text[:300]
    # Redact api_key from the URL before it ever reaches a traceback/log/chat --
    # the caller's key must never end up in plaintext output.
    safe_url = re.sub(r"api_key=[^&]+", "api_key=***REDACTED***", response.url)
    raise RuntimeError(f"openFDA request failed ({response.status_code} {safe_url}): {message}")


def api_get(base_url: str, params: dict, api_key: str | None, budget: RequestBudget) -> dict:
    """GET one page from an openFDA endpoint with basic retry/backoff on
    rate limiting.

    openFDA returns HTTP 404 (not an error payload) when a query matches
    zero records -- that's treated as an empty result set, not a failure.
    """
    if api_key:
        params = {**params, "api_key": api_key}

    max_retries = 5
    backoff = 1.0
    for attempt in range(max_retries):
        response = requests.get(base_url, params=params, timeout=30)
        budget.record()

        if response.status_code == 404:
            return {"meta": {"results": {"total": 0}}, "results": []}

        if response.status_code == 429 or response.status_code >= 500:
            if attempt == max_retries - 1:
                _raise_with_openfda_error(response)
            time.sleep(backoff)
            backoff *= 2
            continue

        if response.status_code >= 400:
            _raise_with_openfda_error(response)

        time.sleep(REQUEST_DELAY_SECONDS)
        return response.json()

    raise RuntimeError(f"openFDA request to {base_url} failed after retries")
