"""`LlmTermSheetExtraction` — spec 05 G4's fail-closed confidence contract."""

from __future__ import annotations

import pytest

from reconciliation.llm import FakeLlmClient, LlmUnavailable
from reconciliation.tools.adapters.llm_term_sheet import LlmTermSheetExtraction
from reconciliation.tools.contracts import ToolUnavailable


def _extractor(client: FakeLlmClient | None = None, documents: dict | None = None):
    docs = documents or {}
    return LlmTermSheetExtraction(client or FakeLlmClient(), lambda ref: docs.get(ref, ""))


def test_empty_document_is_found_nothing_not_an_error():
    """spec 05 G4: absent evidence must be representable as low confidence, not
    force a raised exception the caller has to translate."""
    extractor = _extractor(documents={"dms://x": ""})

    result = extractor.extract_clauses("dms://x")

    assert result.fixing_source_clause is None
    assert result.confidence == 0.0


def test_missing_confidence_is_clamped_to_zero_not_defaulted_high():
    client = FakeLlmClient()
    client.seed({"fixing_source_clause": "SIX fixing", "citation": "4.2(a)"})
    extractor = _extractor(client, documents={"dms://x": "some term sheet text"})

    result = extractor.extract_clauses("dms://x")

    assert result.confidence == 0.0  # missing "confidence" key -> fail closed


def test_out_of_range_confidence_is_clamped_into_0_to_1():
    client = FakeLlmClient()
    client.seed({"fixing_source_clause": "SIX fixing", "confidence": 42})
    extractor = _extractor(client, documents={"dms://x": "text"})

    assert extractor.extract_clauses("dms://x").confidence == 1.0


def test_nan_confidence_is_clamped_to_zero():
    client = FakeLlmClient()
    client.seed({"fixing_source_clause": "SIX fixing", "confidence": float("nan")})
    extractor = _extractor(client, documents={"dms://x": "text"})

    assert extractor.extract_clauses("dms://x").confidence == 0.0


def test_llm_unavailable_maps_to_the_retryable_tool_error():
    client = FakeLlmClient(raise_error=LlmUnavailable("down"))
    extractor = _extractor(client, documents={"dms://x": "text"})

    with pytest.raises(ToolUnavailable):
        extractor.extract_clauses("dms://x")


def test_valid_high_confidence_extraction_passes_through():
    client = FakeLlmClient()
    client.seed(
        {
            "fixing_source_clause": "SIX fixing at 11:00 CET",
            "barrier_definition": "Continuously observed.",
            "dispute_resolution_clause": "Calculation Agent decides.",
            "citation": "Section 4.2(a)",
            "confidence": 0.95,
        }
    )
    extractor = _extractor(client, documents={"dms://x": "text"})

    result = extractor.extract_clauses("dms://x")

    assert result.fixing_source_clause == "SIX fixing at 11:00 CET"
    assert result.confidence == 0.95
