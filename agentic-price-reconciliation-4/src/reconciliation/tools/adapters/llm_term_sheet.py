"""LLM-backed `term_sheet_extraction_tool` (spec 08).

Targeted clause extraction, not document summarisation: the agent needs the
fixing-source clause, the barrier definition, the dispute-resolution clause, and
a citation locating them — spec 09 requires the audit trail to cite the exact
clause relied on, not "term sheet reviewed".

## Confidence is the guardrail, so it must be honest

`agent1/graph.py` refuses to draft when `confidence <
settings.term_sheet.min_extraction_confidence` and raises a human task instead
(spec 05 G4 / spec 10 §1). That check is only as good as the number feeding it,
so:

* the prompt tells the model to return a **low** confidence rather than a guessed
  clause, and
* `_clamp_confidence` drops anything malformed or out of range to **0.0**, not to
  a default — an unreadable confidence must fail closed into the human-task path,
  never sail past the threshold.

Reaching a clause verbatim matters too: the extraction returns clause *text*, and
the agent stores it on `Case.term_sheet_extract` for the reviewer to check
against the document at `citation`.
"""

from __future__ import annotations

from ...llm.client import (
    LlmAuthError,
    LlmClient,
    LlmInvalidResponse,
    LlmQuotaExceeded,
    LlmTimeout,
    LlmUnavailable,
)
from ..contracts import (
    ExtractedClauses,
    PermissionDenied,
    QuotaExceeded,
    ToolTimeout,
    ToolUnavailable,
    ValidationRejected,
)

PROMPT_VERSION = "term-sheet-extract/1"

_SYSTEM_PROMPT = """You extract specific contractual clauses from structured \
product term sheets for a bank's operations desk.

Rules you must follow:
- Return the clause text VERBATIM from the document. Never paraphrase, summarise, \
or reconstruct a clause.
- If a clause is not present, or you cannot locate it with confidence, return null \
for it. A null is correct and useful; an invented clause is a serious error that \
could cause a counterparty to be contacted on a false contractual basis.
- `citation` must locate the fixing-source clause in the document (e.g. \
'Section 4.2(a)', 'Schedule 1, para 3').
- `confidence` is your calibrated confidence, 0.0 to 1.0, that the fixing-source \
clause you returned is correct and complete. If you are unsure, return a LOW \
number. Downstream, a low confidence routes the case to a human for manual \
review, which is the desired outcome when you are unsure.
"""

_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "fixing_source_clause": {
            "type": ["string", "null"],
            "description": (
                "Verbatim text of the clause naming the authoritative price fixing "
                "source. Null if not present."
            ),
        },
        "barrier_definition": {
            "type": ["string", "null"],
            "description": "Verbatim text defining the barrier and how it is observed.",
        },
        "dispute_resolution_clause": {
            "type": ["string", "null"],
            "description": "Verbatim text of the dispute-resolution clause.",
        },
        "citation": {
            "type": ["string", "null"],
            "description": "Locator for the fixing-source clause, e.g. 'Section 4.2(a)'.",
        },
        "confidence": {
            "type": "number",
            "description": (
                "Calibrated confidence (0.0-1.0) that fixing_source_clause is "
                "correct and complete. Low is better than overconfident."
            ),
        },
    },
    "required": ["confidence"],
}


class LlmTermSheetExtraction:
    """`TermSheetExtractionTool` backed by an `LlmClient`.

    `document_loader` resolves a `document_ref` (from
    `DocumentRepositoryApi.find_term_sheet`) to document text. Which system that
    is remains OPEN QUESTION 6 in `requirements.md` §6, so it is injected rather
    than assumed here.
    """

    def __init__(
        self,
        llm: LlmClient,
        document_loader,
        *,
        max_document_chars: int = 60_000,
    ) -> None:
        self._llm = llm
        self._load = document_loader
        self._max_document_chars = max_document_chars

    def extract_clauses(self, document_ref: str) -> ExtractedClauses:
        text = self._load(document_ref)
        if not text or not text.strip():
            # An empty document is "found nothing", which the contract can
            # represent — and which routes to the human task rather than raising.
            return ExtractedClauses(
                fixing_source_clause=None,
                barrier_definition=None,
                dispute_resolution_clause=None,
                citation=None,
                confidence=0.0,
            )

        try:
            extraction = self._llm.extract_json(
                system=_SYSTEM_PROMPT,
                user=(
                    "Extract the schema fields from the term sheet below.\n\n"
                    "<term_sheet>\n"
                    f"{text[: self._max_document_chars]}\n"
                    "</term_sheet>"
                ),
                json_schema=_SCHEMA,
                schema_name="term_sheet_clauses",
                schema_description="Targeted clauses extracted from a term sheet.",
                max_tokens=2048,
            )
        except LlmTimeout as exc:
            raise ToolTimeout(f"term sheet extraction timed out: {exc}") from exc
        except LlmQuotaExceeded as exc:
            raise QuotaExceeded(f"term sheet extraction quota: {exc}") from exc
        except LlmAuthError as exc:
            raise PermissionDenied(
                f"term sheet extraction credential rejected: {exc}"
            ) from exc
        except LlmInvalidResponse as exc:
            raise ValidationRejected(f"term sheet extraction unusable: {exc}") from exc
        except LlmUnavailable as exc:
            raise ToolUnavailable(f"term sheet extraction unavailable: {exc}") from exc

        data = extraction.data
        return ExtractedClauses(
            fixing_source_clause=_clean(data.get("fixing_source_clause")),
            barrier_definition=_clean(data.get("barrier_definition")),
            dispute_resolution_clause=_clean(data.get("dispute_resolution_clause")),
            citation=_clean(data.get("citation")),
            confidence=_clamp_confidence(data.get("confidence")),
        )


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _clamp_confidence(value: object) -> float:
    """Malformed or out-of-range confidence becomes 0.0 — fail closed (G4)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    as_float = float(value)
    if as_float != as_float:  # NaN
        return 0.0
    return min(1.0, max(0.0, as_float))
