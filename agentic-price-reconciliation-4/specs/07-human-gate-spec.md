# 07 — Human gate spec

> **Purpose:** defines both human checkpoints — who acts, what they see, and what
> actions are available — referenced from `05-agent-1-spec.md` and
> `06-agent-2-spec.md` rather than duplicated in each.

Two human checkpoints exist in this workflow. Both are first-class orchestrator
states — the state machine cannot transition past either without an explicit,
recorded human action.

## Human gate 1 — Pre-send review (mandatory, every case)
- **Who:** Mid-Office Analyst (or delegated approver).
- **What they see:** full case_summary, all pulled prices, term sheet extract with
  citation, the drafted email (from `05-agent-1-spec.md`).
- **Actions:** Approve & Send / Edit & Send / Reject (kill case) / Reassign.
- **SLA:** target < 15 min human turnaround (configurable); auto-reminder after threshold.
- **Why a gate here even though drafting is automated:** outbound comms to a
  counterparty is an external, reputationally/legally sensitive action.

## Human gate 2 — Dispute escalation (only on dispute/no-response)
- **Who:** SME / senior analyst (routing rule based on product type / counterparty
  / notional size).
- **What they see:** full case context bundle — original divergence, term sheet
  clause, counterparty's stated rationale, comms thread, agent-generated
  (non-binding) suggested resolution options.
- **Actions — none of them a dead end:**
  1. **Resolve manually** — enter final price + rationale → RESOLVED → CLOSED.
  2. **Request more info from counterparty** — SME specifies what to ask; Agent 2
     drafts and sends it (§2.6a in `06-agent-2-spec.md`) → status returns to
     AWAITING_CLARIFICATION → on reply, routes back to Human gate 2 with updated
     context. Genuine loop, not a one-way handoff.
  3. **Escalate to Legal** — for disputes analysts can't resolve; Legal's outcome
     feeds back into the case and still terminates at RESOLVED → CLOSED.
- **Loop guard:** if `clarification_loop_count` exceeds a configurable threshold
  (recommend 2–3) without resolution, the case is auto-flagged for mandatory Legal
  escalation on the next Human gate 2 visit. See `10-error-and-exception-spec.md`.
- **This is the only place ad hoc, judgment-based human work remains** —
  intentionally, since contractual disputes require human/legal judgment.

## Common requirements for both gates
- Every action requires a free-text rationale field (audit + model-improvement).
- Every action is recorded with actor identity and timestamp in the audit log.
- A human can invoke the kill switch (pull the case to fully manual handling) from
  either gate.
