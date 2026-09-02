"""Structural guardrail tests — the boundaries that must hold by construction.

These read as unusual tests because they assert the *absence* of capability. That is
the point: spec 05 G1 and spec 06 G1 are least-privilege boundaries, and the cheapest
way to stop one eroding is a test that fails the moment someone adds the field or
method that would widen it.

None of these replace scoped credentials in deployment. A type annotation is not a
security control; it is a tripwire for accidental widening in this repo.
"""

from __future__ import annotations

from reconciliation.domain.case import AuditEntry, CommsMessage
from reconciliation.gates.service import HumanGateService
from reconciliation.tools.contracts import (
    Agent1Tools,
    Agent2Tools,
    BookingSystemApi,
    GateService,
    ParsedEmail,
)


# --------------------------------------------------------------------------- #
# spec 05 G1 — Agent 1 cannot send externally or write to a system of record
# --------------------------------------------------------------------------- #


def test_agent1_toolbox_has_no_send_or_write_capability():
    fields = set(Agent1Tools.model_fields)
    assert "booking_system" not in fields
    assert "counterparty_comms" not in fields
    assert "dashboard" not in fields


def test_agent1_toolbox_rejects_extra_tools():
    """`extra='forbid'` means a write tool can't be slipped in at construction."""
    assert Agent1Tools.model_config["extra"] == "forbid"


def test_agent2_toolbox_has_the_write_path():
    fields = set(Agent2Tools.model_fields)
    assert {"booking_system", "counterparty_comms", "dashboard"} <= fields


# --------------------------------------------------------------------------- #
# spec 06 G1 — booking write scoped to one operation
# --------------------------------------------------------------------------- #


def test_human_gate_service_satisfies_the_gate_service_protocol():
    """`Agent1Tools.gates` is typed as `GateService`, not the concrete class —
    this pins that `HumanGateService` structurally satisfies it, including
    `submit_for_approval` (Agent 1 step 1.9's entry point)."""
    assert isinstance(HumanGateService, type)
    for method in ("open_gate", "submit_for_approval"):
        assert hasattr(HumanGateService, method)
    assert issubclass(HumanGateService, GateService)


def test_booking_api_exposes_exactly_one_operation():
    """Widening this protocol widens the blast radius G1 exists to bound."""
    methods = {
        name
        for name in dir(BookingSystemApi)
        if not name.startswith("_")
    }
    assert methods == {"update_break_record"}


# --------------------------------------------------------------------------- #
# spec 05 G6 / spec 09 — data minimisation
# --------------------------------------------------------------------------- #


def test_comms_message_cannot_carry_a_raw_body():
    """Raw counterparty content lives behind `raw_ref`, never on the Case."""
    fields = set(CommsMessage.model_fields)
    assert "raw_ref" in fields
    for forbidden in ("body", "raw_body", "content", "text", "html"):
        assert forbidden not in fields


def test_parsed_email_exposes_only_the_permitted_extracted_fields():
    """spec 05 G3 — Agent 1 extracts defined structured fields, nothing else."""
    fields = set(ParsedEmail.model_fields)
    permitted = {
        "message_id",
        "case_reference_id",
        "sender",
        "received_at",
        "raw_ref",
        "trade_id",
        "counterparty_id",
        "quoted_price",
        "quoted_barrier_status",
        "field_confidence",
        "injection_suspected",
    }
    assert fields == permitted


def test_audit_entry_stores_references_not_payloads():
    """spec 09 — input/output are references; payloads stay in the raw store."""
    fields = set(AuditEntry.model_fields)
    assert {"input_ref", "output_ref"} <= fields
    for forbidden in ("input", "output", "payload", "body"):
        assert forbidden not in fields


# --------------------------------------------------------------------------- #
# Cross-branch contract
# --------------------------------------------------------------------------- #


def test_agent2_contract_stubs_are_importable():
    """`feat/agent-1` must be able to import `agent2` without it being finished."""
    from reconciliation.agent2 import Agent2, AutoCloseCheck, IntentClassifier

    for cls in (Agent2, AutoCloseCheck, IntentClassifier):
        assert cls is not None


def test_agent2_stubs_raise_rather_than_returning_a_permissive_default():
    """An unimplemented check must never read as 'criteria met' (spec 06 G2)."""
    import pytest

    from reconciliation.agent2.auto_close import AutoCloseCheck

    check = AutoCloseCheck.__new__(AutoCloseCheck)
    with pytest.raises(NotImplementedError):
        check.evaluate(None)  # type: ignore[arg-type]
