# 10 — Error and exception spec

> **Purpose:** the single place failure handling is defined — tool failures, SLA
> timeouts, the clarification loop guard, idempotency, and the human override.
> This file exists because failure handling was previously scattered across the
> agent specs rather than defined once. Every failure mode below must resolve to a
> defined state — consistent with FR7 (no dead ends).

## 1. Tool / external system failures

| Failure | Handling |
|---|---|
| Bloomberg/Reuters/SIX API timeout or error | Retry with backoff (max 3 attempts); if still failing, proceed with available sources and flag `partial_price_data=true` on the Case; route to Human gate 1 with a warning rather than auto-blocking |
| Term sheet not found / extraction low-confidence | Do not auto-draft comms. Route directly to a human task ("term sheet needs manual lookup") before Agent 1 can proceed to step 1.7 |
| Pricing system API unavailable | Case creation blocked; retry on a schedule; alert on-call/ops if unavailable > N minutes |
| Booking system write failure (step 2.6b) | Case stays in RESOLVED (not CLOSED) until the write succeeds; retried with backoff; does not silently drop the update |

## 2. Classification confidence thresholds
- Break detection (Agent 1 step 1.3) is rule-based, not probabilistic — no
  confidence threshold needed, but the tolerance value itself must be configurable
  per product type (see `requirements.md` open question 1).
- Response-intent classification (Agent 2 step 2.1) is probabilistic. Below the
  configured confidence threshold, intent is treated as PARTIAL and routed to
  Human gate 2 rather than guessed — never auto-close on a low-confidence AGREE.

## 3. SLA timeouts
- **Human gate 1 timeout:** auto-reminder at threshold; if still unactioned after a
  second threshold, escalate to a backup approver (do not silently expire).
- **Counterparty response timeout:** handled explicitly at Agent 2 step 2.4 —
  auto-escalates to ESCALATED, not left in AWAITING_RESPONSE indefinitely.
- **Human gate 2 timeout:** same pattern as gate 1 — reminder, then backup routing.

## 4. Clarification loop guard
- `clarification_loop_count` increments each time Human gate 2 sends a case back to
  Agent 2 for more info (`06-agent-2-spec.md` step 2.6a).
- Default cap: 2–3 loops (configurable). On exceeding the cap, the case is
  auto-flagged for **mandatory** Legal escalation the next time it reaches
  Human gate 2 — the loop cannot continue indefinitely.
- Open question: whether the counter resets on materially new information from the
  counterparty, or always counts toward the same hard cap (`requirements.md`
  open question 7) — resolve before implementing the cap logic.

## 5. Idempotency
- Reprocessing an inbound email already tied to a `case_reference_id` must not
  create a duplicate Case — dedupe on case_reference_id + message_id before
  triggering Agent 1 or Agent 2.
- Duplicate tool calls (e.g. a retried price pull) must not double-write to the
  Case's `external_prices` array — use upsert keyed on (source, as_of).

## 6. Human override ("kill switch")
- A human can pull any case out of automated flow into fully manual handling at any
  state — this transition is always available regardless of current status, is
  logged like any other transition, and takes precedence over agent-initiated
  transitions in flight.

## 7. Confidence-based default: route to human
As a general principle: whenever an agent's confidence is below its configured
threshold for a decision that would otherwise auto-progress the case (detection,
intent classification, auto-close eligibility), the default behavior is to route to
the appropriate human gate rather than proceed. This is a deliberate bias toward
human review during the initial rollout phases (see `architecture.md` §6 build
sequence) and can be tuned as trust in the classifiers is established.
