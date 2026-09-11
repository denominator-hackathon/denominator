"""
Generates the draft narrative for Agent 5's Safety Action Pack: one OpenAI
call synthesizing the rate engine's finding, Agent 3's regulatory context,
and a few example complaints into a human-readable summary plus a list of
things worth looking at next.

Hard requirement (docs/scope_lock.md, docs/architecture_spec.md): this
module NEVER issues a DISMISS / INVESTIGATE FURTHER / CONFIRM verdict --
that decision belongs to the mandatory human decision gate. The prompt
explicitly instructs the model not to decide, and the output is always
labeled DRAFT. The 0.75% threshold's "not a validated figure" disclaimer
is reproduced verbatim in every output, never left to the model to
paraphrase.
"""

import hashlib
import json

from .llm_client import chat_json
from rate_engine import THRESHOLD_DISCLAIMER

MAX_EVIDENCE_SAMPLES = 5


def _cache_key(device_code: str, rate_summary: dict, evidence: list[dict], related_documents: list[dict]) -> str:
    basis = json.dumps(
        {
            "device_code": device_code,
            "rate_summary": rate_summary,
            "evidence_keys": sorted(e.get("mdr_report_key", "") for e in evidence),
            "related_documents": sorted(d.get("document_id", "") for d in related_documents),
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return f"document_impact:{digest}"


def _build_prompt(
    device_code: str,
    rate_summary: dict,
    regulatory_summary: dict,
    evidence: list[dict],
    related_documents: list[dict],
) -> str:
    evidence_text = "\n".join(
        f'- [{e.get("mdr_report_key", "")}] {e.get("problem_category", "uncategorized")}: '
        f'{(e.get("event_description", "") or "")[:300]}'
        for e in evidence[:MAX_EVIDENCE_SAMPLES]
    )
    internal_docs = [d for d in related_documents if d.get("document_class") != "public_external_regulatory_reference"]
    external_docs = [d for d in related_documents if d.get("document_class") == "public_external_regulatory_reference"]

    def _format_docs(docs: list[dict]) -> str:
        return "\n".join(
            f'- {d.get("document_id", "")} ({d.get("document_type", "")}): {d.get("qms_reference", "")}'
            for d in docs
        )

    internal_text = _format_docs(internal_docs) if internal_docs else "(none on file for this device_code)"
    external_text = _format_docs(external_docs) if external_docs else "(none on file for this device_code)"

    recalls_text = f"{regulatory_summary.get('recall_total', 0)} total recalls on file"
    classification = regulatory_summary.get("classification") or {}

    return (
        "You are drafting a DRAFT summary for a human safety reviewer at a medical "
        "device company, based on a computed complaint-rate signal. You must NOT "
        "decide whether this is a real problem or what action to take -- that "
        "decision is made by a human reviewer, never by you. Your job is only to "
        "summarize the evidence clearly and suggest what the reviewer should look "
        "at, phrased as suggestions requiring human confirmation, not conclusions.\n\n"
        f"Device (product code): {device_code}\n"
        f"Computed rate: {rate_summary.get('reason', 'N/A')}\n"
        f"Regulatory context: device class {classification.get('device_class', 'unknown')}, "
        f"{recalls_text}.\n\n"
        f"Example complaint evidence (up to {MAX_EVIDENCE_SAMPLES} samples):\n{evidence_text}\n\n"
        f"Internal QMS documents on file for this device_code (the manufacturer's own "
        f"controlled records):\n{internal_text}\n\n"
        f"External public regulatory references on file for this device_code (FDA recall "
        f"records, safety communications, 510(k)s, etc. -- these are NOT the manufacturer's "
        f"own internal documents; never describe them as if they were a CAPA, PMS/PSUR, SOP, "
        f"or risk-management record):\n{external_text}\n\n"
        "Respond as a JSON object with exactly these fields:\n"
        '  "summary": a 2-4 sentence neutral summary of the finding and evidence, '
        "for a reviewer who hasn't seen any of this yet.\n"
        '  "documents_to_review": a list of strings naming which existing documents '
        "(by document_id, if any were listed) seem most relevant to review -- when citing "
        "an external reference, keep it clearly labeled as external/public evidence, not an "
        "internal QMS record -- or an "
        "empty list if none apply or none exist.\n"
        '  "suggested_next_steps": a list of 1-4 short strings suggesting what a '
        'human reviewer might want to check or do next -- phrase each as a '
        'suggestion ("Consider reviewing...", "Worth checking whether...") never as '
        "a completed decision or instruction to act."
    )


def generate_document_impact(
    device_code: str,
    rate_summary: dict,
    regulatory_summary: dict,
    evidence: list[dict],
    related_documents: list[dict],
    cache_manager=None,
) -> tuple[dict, int, int, int]:
    """Returns (draft_content, llm_calls_made, prompt_tokens, completion_tokens).

    draft_content is {summary, documents_to_review, suggested_next_steps} --
    always additionally wrapped by the caller (Agent 5) with status=DRAFT and
    the verbatim threshold disclaimer, never left to the model to produce.
    """
    cache_key = _cache_key(device_code, rate_summary, evidence, related_documents)
    cached = cache_manager.get(cache_key) if cache_manager else None
    if cached is not None:
        return cached, 0, 0, 0

    prompt = _build_prompt(device_code, rate_summary, regulatory_summary, evidence, related_documents)
    raw_json, prompt_tokens, completion_tokens = chat_json(prompt)

    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        parsed = {}

    draft_content = {
        "summary": parsed.get("summary", ""),
        "documents_to_review": parsed.get("documents_to_review", []) if isinstance(parsed.get("documents_to_review"), list) else [],
        "suggested_next_steps": parsed.get("suggested_next_steps", []) if isinstance(parsed.get("suggested_next_steps"), list) else [],
    }

    if cache_manager:
        cache_manager.set(cache_key, draft_content)

    return draft_content, 1, prompt_tokens, completion_tokens
