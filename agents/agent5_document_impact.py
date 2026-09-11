from dataclasses import asdict, is_dataclass
from typing import Any

from .base_agent import AgentResult, DenominatorAgent
from .document_impact_generator import generate_document_impact
from rate_engine import THRESHOLD_DISCLAIMER


class DocumentImpactAgent(DenominatorAgent):
    """Agent #5: Maps evidence to QMS documents and suggests actions.

    Planned: Day 9
    Uses 1 OpenAI (gpt-4o-mini) API call, cached after first run.
    See agents/llm_client.py for the shared client (switched from the
    original Claude plan to OpenAI on 2026-09-07).

    Takes a computed RateResult (rate_engine.py) plus Agent 3's regulatory
    context and a sample of in-scope complaints (Agent 4's output), and
    drafts a Safety Action Pack: a human-readable summary, any existing
    QMS documents worth reviewing, and suggested next steps.

    Non-negotiable (docs/architecture_spec.md): this agent never decides
    DISMISS / INVESTIGATE FURTHER / CONFIRM -- output is always labeled
    DRAFT and human_decision_required=True. The 0.75% threshold's "not a
    validated figure" disclaimer (docs/scope_lock.md) is reproduced
    verbatim, never left to the model to paraphrase.
    """

    name = "document_impact"
    description = "QMS document impact mapping + Safety Action Pack suggestion"

    def run(self, inputs: dict[str, Any]) -> AgentResult:
        rate_result = inputs.get("rate_result")
        if rate_result is None:
            return AgentResult(
                agent_name=self.name,
                success=False,
                output={},
                llm_calls_used=0,
                notes="No inputs['rate_result'] provided -- expected a RateResult (or equivalent dict) from rate_engine.py.",
            )

        rate_summary = asdict(rate_result) if is_dataclass(rate_result) else dict(rate_result)

        if rate_summary.get("can_compute") is False:
            return AgentResult(
                agent_name=self.name,
                success=False,
                output={},
                llm_calls_used=0,
                notes=(
                    f"rate_result.can_compute is False ({rate_summary.get('reason', '')}) -- "
                    "cannot draft a Safety Action Pack without a computed rate."
                ),
            )

        device_code = inputs.get("device_code", rate_summary.get("device_code", "FRN"))
        regulatory_context = inputs.get("regulatory_context", {})
        records = inputs.get("records", [])
        document_map_rows = inputs.get("document_map_rows", [])

        related_documents = [d for d in document_map_rows if d.get("device_code") == device_code]
        related_internal_documents = [
            d for d in related_documents if d.get("document_class") != "public_external_regulatory_reference"
        ]
        related_external_references = [
            d for d in related_documents if d.get("document_class") == "public_external_regulatory_reference"
        ]

        draft_content, llm_calls, prompt_tokens, completion_tokens = generate_document_impact(
            device_code,
            rate_summary,
            regulatory_context,
            records,
            related_documents,
            cache_manager=self.cache_manager,
        )

        output = {
            "status": "DRAFT",
            "device_code": device_code,
            "rate_summary": rate_summary,
            "threshold_disclaimer": THRESHOLD_DISCLAIMER,
            "regulatory_context": {
                "recall_total": regulatory_context.get("recall_total"),
                "classification": regulatory_context.get("classification"),
            },
            # Kept separate deliberately -- an external FDA reference (recall record,
            # safety communication, 510(k)) must never be presented as if it were one
            # of the manufacturer's own internal QMS records. See data/README.md.
            "related_internal_documents": related_internal_documents,
            "related_external_references": related_external_references,
            "evidence_sample_count": min(len(records), 5),
            "draft": draft_content,
            "human_decision_required": True,
        }

        rate_pct = rate_summary.get("rate_pct")
        rate_display = f"{rate_pct:.4f}" if rate_pct is not None else "N/A"
        notes = (
            f"Drafted Safety Action Pack (status=DRAFT) for {device_code}: "
            f"rate {rate_display}%, exceeds_threshold={rate_summary.get('exceeds_threshold')}, "
            f"{len(related_internal_documents)} internal QMS document(s) and "
            f"{len(related_external_references)} external regulatory reference(s) on file, "
            f"{llm_calls} OpenAI call(s) made ({prompt_tokens} prompt / {completion_tokens} completion tokens; "
            "0 if served from cache). No DISMISS/INVESTIGATE FURTHER/CONFIRM decision made -- requires human review."
        )

        return AgentResult(
            agent_name=self.name,
            success=True,
            output=output,
            llm_calls_used=llm_calls,
            notes=notes,
        )
