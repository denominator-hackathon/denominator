"""
LLM-based problem categorization for Agent 1 (MAUDE Ingestion).

Classifies each record's raw event_description narrative into one of a
fixed set of infusion-pump problem categories, via OpenAI (see
agents/llm_client.py). This is additive to openFDA's own product_problems
codes -- it exists because ~1% of real records have no product_problems
code at all, and a consistent category is useful input for the rate
engine's signal grouping regardless.

Batches multiple records into a single LLM call (MAX_RECORDS_PER_LLM_CALL
per call) to keep cost and call count down, and caches each batch's result
so re-running on the same records never re-spends budget.
"""

import hashlib
import json

from .llm_client import chat_json

MAX_RECORDS_PER_LLM_CALL = 150  # raised from 40 after a real run hit a 50-requests/day
# OpenAI account cap -- gpt-4o-mini's context window comfortably fits far
# larger batches than 40 records, so this cuts total call count ~4x for
# the same work. See MAX_DESCRIPTION_CHARS below for the other half of
# that fix (per-record prompt size, which is what actually bounds how far
# batch size can be pushed before hitting output-token limits).
MAX_DESCRIPTION_CHARS = 600  # narratives can run 3000+ chars; category signal
# is reliably in the opening sentences, so truncating keeps per-record
# prompt cost down without hurting classification quality

PROBLEM_CATEGORIES = [
    "Over-Infusion / Excess Flow",
    "Under-Infusion / No Flow",
    "Free Flow (Unintended)",
    "Occlusion / Blockage",
    "Alarm Malfunction (False or Failed Alarm)",
    "Battery / Power Failure",
    "Software / Display Error",
    "Mechanical / Component Failure",
    "User / Use Error",
    "Other / Unclear",
]

NO_DESCRIPTION_CATEGORY = "Insufficient Data"


def _chunk_cache_key(chunk: list[dict]) -> str:
    basis = "|".join(f"{r['mdr_report_key']}:{r['event_description']}" for r in chunk)
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return f"problem_category_chunk:{digest}"


def _build_prompt(chunk: list[dict]) -> str:
    categories_list = "\n".join(f"- {c}" for c in PROBLEM_CATEGORIES)
    items = "\n".join(
        json.dumps(
            {"mdr_report_key": r["mdr_report_key"], "text": r["event_description"][:MAX_DESCRIPTION_CHARS]}
        )
        for r in chunk
    )
    return (
        "You are classifying FDA MAUDE adverse event reports for infusion "
        "pumps into exactly one problem category each. Choose only from "
        f"this fixed list:\n{categories_list}\n\n"
        f"Reports (one JSON object per line):\n{items}\n\n"
        'Respond with a single JSON object mapping each mdr_report_key to '
        'its chosen category, e.g. {"12345": "Over-Infusion / Excess Flow"}. '
        "Use exactly the category text as given, nothing else."
    )


def categorize_problems(
    records: list[dict], cache_manager=None
) -> tuple[dict[str, str], int, int, int]:
    """Classify each record's event_description into a fixed problem
    category. Records with a blank event_description are assigned
    NO_DESCRIPTION_CATEGORY without calling the LLM.

    Returns (mdr_report_key -> category, llm_calls_made, prompt_tokens,
    completion_tokens).
    """
    categories: dict[str, str] = {}
    to_classify = []
    for r in records:
        if not r.get("event_description"):
            categories[r["mdr_report_key"]] = NO_DESCRIPTION_CATEGORY
        else:
            to_classify.append(r)

    llm_calls = 0
    prompt_tokens_total = 0
    completion_tokens_total = 0

    for i in range(0, len(to_classify), MAX_RECORDS_PER_LLM_CALL):
        chunk = to_classify[i : i + MAX_RECORDS_PER_LLM_CALL]
        cache_key = _chunk_cache_key(chunk)
        cached = cache_manager.get(cache_key) if cache_manager else None
        if cached is not None:
            categories.update(cached)
            continue

        raw_json, prompt_tokens, completion_tokens = chat_json(_build_prompt(chunk))
        llm_calls += 1
        prompt_tokens_total += prompt_tokens
        completion_tokens_total += completion_tokens

        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError:
            parsed = {}

        valid_keys = {r["mdr_report_key"] for r in chunk}
        chunk_result = {
            key: category
            for key, category in parsed.items()
            if key in valid_keys and category in PROBLEM_CATEGORIES
        }
        # Anything the model omitted or returned an invalid category for
        # falls back to "Other / Unclear" rather than being left missing.
        for r in chunk:
            chunk_result.setdefault(r["mdr_report_key"], "Other / Unclear")

        if cache_manager:
            cache_manager.set(cache_key, chunk_result)
        categories.update(chunk_result)

    return categories, llm_calls, prompt_tokens_total, completion_tokens_total
