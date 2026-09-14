"""
Matches raw MAUDE (brand_name, manufacturer_name) pairs to canonical GUDID
device records fetched by gudid_client.py.

Three-tier strategy:
1. Exact match on normalized (brand_name, company_name) -- free, no LLM.
2. Fuzzy candidate generation (difflib) for anything left over -- narrows
   946 canonical records down to a handful of plausible candidates per pair.
3. LLM disambiguation among those candidates, batched and cached per batch
   (same pattern as agents/problem_categorizer.py) -- picks the correct
   match or says "no_match" rather than forcing a wrong guess.
"""

import difflib
import hashlib
import json
import re

from .llm_client import chat_json

MAX_PAIRS_PER_LLM_CALL = 60  # raised from 20 after a real run hit a
# 50-requests/day OpenAI account cap -- each item here is short (a brand/
# manufacturer pair plus 3 short candidates), so a larger batch stays well
# within gpt-4o-mini's context window while cutting total call count 3x
CANDIDATE_COUNT = 3
CANDIDATE_MIN_SCORE = 0.3

EMPTY_MATCH = {
    "gudid_record_key": "",
    "gudid_brand_name": "",
    "gudid_company_name": "",
    "match_method": "no_match",
}


def normalize(text: str) -> str:
    text = (text or "").upper()
    text = re.sub(r"[^A-Z0-9 ]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _build_exact_index(canonical: list[dict]) -> dict[tuple[str, str], dict]:
    index: dict[tuple[str, str], dict] = {}
    for c in canonical:
        key = (normalize(c["brand_name"]), normalize(c["company_name"]))
        index.setdefault(key, c)
    return index


def _candidates_for(query_brand: str, query_company: str, canonical: list[dict]) -> list[dict]:
    """Company similarity weighted higher than brand similarity -- company
    names tend to be more consistently formatted across MAUDE and GUDID
    than product/brand names.

    A blank brand_name is deliberately given zero candidates: with no brand
    text at all, ranking only by company name would hand the LLM a set of
    same-company products it has no real basis to pick between, risking a
    confident-looking but ungrounded match.
    """
    q_brand = normalize(query_brand)
    if not q_brand:
        return []

    q_company = normalize(query_company)
    scored = []
    seen_identity: set[tuple[str, str]] = set()
    for c in canonical:
        identity = (normalize(c["brand_name"]), normalize(c["company_name"]))
        if identity in seen_identity:
            continue  # skip near-duplicate SKU variants that would show up
            # as identical, undifferentiated text to the LLM
        score = 0.6 * _similarity(q_company, normalize(c["company_name"])) + 0.4 * _similarity(
            q_brand, normalize(c["brand_name"])
        )
        if score >= CANDIDATE_MIN_SCORE:
            scored.append((score, c))
            seen_identity.add(identity)
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:CANDIDATE_COUNT]]


def _chunk_cache_key(chunk: list[dict]) -> str:
    basis = "|".join(
        f"{p['brand_name']}::{p['manufacturer_name']}::"
        + ",".join(c["gudid_record_key"] for c in p["candidates"])
        for p in chunk
    )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return f"product_identity_chunk:{digest}"


def _build_prompt(chunk: list[dict]) -> str:
    items = []
    for i, p in enumerate(chunk):
        candidates_text = "\n".join(
            f'    {j}. record_key={c["gudid_record_key"]} brand="{c["brand_name"]}" '
            f'company="{c["company_name"]}" model="{c["version_or_model_number"]}"'
            for j, c in enumerate(p["candidates"])
        )
        items.append(
            f'{i}. Complaint device: brand="{p["brand_name"]}" manufacturer="{p["manufacturer_name"]}"\n'
            f"   Candidates:\n{candidates_text}"
        )
    items_text = "\n".join(items)
    return (
        "You are matching medical device adverse-event complaint records to "
        "the correct entry in the FDA's official device registry (GUDID). "
        "For each numbered complaint below, decide which candidate (if any) "
        "is actually the same physical device/model, accounting for typos, "
        "abbreviations, and formatting differences. If none of the "
        "candidates are a genuine match, say so -- do not force a match.\n\n"
        f"{items_text}\n\n"
        "Respond with a single JSON object mapping each item number (as a "
        'string) to either the chosen candidate\'s record_key, or the '
        'string "no_match". Example: {"0": "abc-123", "1": "no_match"}.'
    )


def match_products(
    pairs: list[tuple[str, str]], canonical: list[dict], cache_manager=None
) -> tuple[dict[tuple[str, str], dict], int, int, int]:
    """Match each (brand_name, manufacturer_name) pair to a canonical GUDID
    record. Returns (pair -> match result, llm_calls_made, prompt_tokens,
    completion_tokens).

    match result is {gudid_record_key, gudid_brand_name, gudid_company_name,
    match_method} where match_method is one of "exact", "llm", "no_match".
    """
    exact_index = _build_exact_index(canonical)
    results: dict[tuple[str, str], dict] = {}
    needs_llm: list[dict] = []

    for brand, manufacturer in pairs:
        key = (normalize(brand), normalize(manufacturer))
        exact = exact_index.get(key)
        if exact:
            results[(brand, manufacturer)] = {
                "gudid_record_key": exact["gudid_record_key"],
                "gudid_brand_name": exact["brand_name"],
                "gudid_company_name": exact["company_name"],
                "match_method": "exact",
            }
            continue

        candidates = _candidates_for(brand, manufacturer, canonical)
        if not candidates:
            results[(brand, manufacturer)] = dict(EMPTY_MATCH)
            continue

        needs_llm.append({"brand_name": brand, "manufacturer_name": manufacturer, "candidates": candidates})

    llm_calls = 0
    prompt_tokens_total = 0
    completion_tokens_total = 0

    for i in range(0, len(needs_llm), MAX_PAIRS_PER_LLM_CALL):
        chunk = needs_llm[i : i + MAX_PAIRS_PER_LLM_CALL]
        cache_key = _chunk_cache_key(chunk)
        cached = cache_manager.get(cache_key) if cache_manager else None

        if cached is None:
            raw_json, prompt_tokens, completion_tokens = chat_json(_build_prompt(chunk))
            llm_calls += 1
            prompt_tokens_total += prompt_tokens
            completion_tokens_total += completion_tokens

            try:
                parsed = json.loads(raw_json)
            except json.JSONDecodeError:
                parsed = {}

            chunk_result: dict[str, dict] = {}
            for idx, p in enumerate(chunk):
                choice = parsed.get(str(idx), "no_match")
                candidate = next((c for c in p["candidates"] if c["gudid_record_key"] == choice), None)
                if candidate:
                    chunk_result[str(idx)] = {
                        "gudid_record_key": candidate["gudid_record_key"],
                        "gudid_brand_name": candidate["brand_name"],
                        "gudid_company_name": candidate["company_name"],
                        "match_method": "llm",
                    }
                else:
                    chunk_result[str(idx)] = dict(EMPTY_MATCH)

            if cache_manager:
                cache_manager.set(cache_key, chunk_result)
        else:
            chunk_result = cached

        for idx, p in enumerate(chunk):
            results[(p["brand_name"], p["manufacturer_name"])] = chunk_result[str(idx)]

    return results, llm_calls, prompt_tokens_total, completion_tokens_total
