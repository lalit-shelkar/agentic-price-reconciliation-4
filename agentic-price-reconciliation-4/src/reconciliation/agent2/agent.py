"""Agent 2 entry points — spec 06 steps 2.1 to 2.7. **CONTRACT STUB.**

Owned by `feat/agent-2`. The method set is fixed here so the orchestrator wiring and
the shared tests can be written against it now; the bodies are the Agent 2 author's
work.

Agent 2 is invoked at three moments, which is why there are three entry points
rather than one `run()`:

* `handle_response` — a reply arrives on the thread (AWAITING_RESPONSE or
  AWAITING_CLARIFICATION → RESPONSE_RECEIVED).
* `handle_sla_expiry` — the response window elapses (step 2.4).
* `close_resolved_case` — a case sits at RESOLVED and needs the booking write plus
  the audit record before CLOSED (steps 2.6b, 2.7).

Mutation rule (same as Agent 1): Agent 2 never calls `case_store.save` or
`audit_log.append` directly. Every state change goes through `Orchestrator`, so the
atomic case+audit write of spec 06 G6 cannot be bypassed.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config.settings import Settings
from ..domain.case import Case
from ..domain.enums import ResponseIntent
from ..orchestrator.engine import Orchestrator
from ..tools.contracts import Agent2Tools


@dataclass(frozen=True)
class Agent2Result:
    """What one Agent 2 invocation did. Returned for logging/assertions."""

    case: Case
    intent: ResponseIntent | None = None
    intent_confidence: float | None = None
    auto_closed: bool = False
    routed_to_gate2: bool = False
    notes: tuple[str, ...] = ()


class Agent2:
    """Respond, resolve, close."""

    #: Recorded as the audit actor identity (spec 09). Bump on prompt/model change.
    AGENT_VERSION = "agent2/0.1.0"

    def __init__(
        self,
        tools: Agent2Tools,
        orchestrator: Orchestrator,
        settings: Settings | None = None,
    ) -> None:
        self._tools = tools
        self._orch = orchestrator
        self._settings = settings or orchestrator.settings

    def handle_response(self, case_id: str, message_id: str) -> Agent2Result:
        """Steps 2.1–2.3, 2.5.

        Must dedupe on `(case_reference_id, message_id)` before doing any work —
        reprocessing the same reply must not double-count a clarification loop or
        create a second case (spec 10 §5).
        """
        raise NotImplementedError("Agent 2 branch: implement spec 06 steps 2.1–2.5")

    def handle_sla_expiry(self, case_id: str) -> Agent2Result:
        """Step 2.4 — no response by `sla_due_at` → ESCALATED → Human gate 2.

        Must not leave the case in AWAITING_RESPONSE (spec 10 §3).
        """
        raise NotImplementedError("Agent 2 branch: implement spec 06 step 2.4")

    def send_clarification_request(self, case_id: str, question: str) -> Agent2Result:
        """Step 2.6a — draft and send the SME's clarification request.

        `gates.service.HumanGateService.request_more_info` has already incremented
        the loop counter, moved the case to AWAITING_CLARIFICATION and re-armed the
        timer. This method drafts the structured request and sends it.
        """
        raise NotImplementedError("Agent 2 branch: implement spec 06 step 2.6a")

    def close_resolved_case(self, case_id: str) -> Agent2Result:
        """Steps 2.6b and 2.7 — booking write, then audit record, then CLOSED.

        Ordering is load-bearing. Per spec 10 §1 a failed booking write leaves the
        case at RESOLVED with a `BOOKING_WRITE_FAILED` manual task and is retried;
        per spec 06 G6 the CLOSED transition and its audit entry commit together.
        """
        raise NotImplementedError("Agent 2 branch: implement spec 06 steps 2.6b, 2.7")
