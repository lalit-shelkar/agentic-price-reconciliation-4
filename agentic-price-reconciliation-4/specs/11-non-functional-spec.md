# 11 — Non-functional requirements

> **Purpose:** the qualities the system must have regardless of feature — timing,
> reliability, security, scalability, observability — as distinct from the
> feature-level SHALL statements in `04-functional-requirements.md`.

## Timing / SLA targets

| Stage | Target |
|---|---|
| Detection → case created | Real-time / < 1 min |
| Price pull + term sheet resolution | < 5 min |
| Human gate 1 review | < 15 min (business hours) |
| Counterparty response window | Configurable per counterparty/product |
| Dispute escalation → SME first action | < 1 business day |

## Reliability
- Orchestrator state machine must be durable (survive process restarts) — a
  workflow engine is required, not an in-memory agent loop, given multi-day wait
  states.
- See `10-error-and-exception-spec.md` for idempotency and retry requirements.

## Security
- Least-privilege tool access per agent (see `architecture.md` §5 and
  `09-audit-and-compliance-spec.md`).
- All external market-data calls via the firm's existing licensed integration layer.

## Scalability
- Not specified for v0.1 (single product scope). Revisit case-volume assumptions
  before expanding beyond initial barrier FX/rates scope
  (`requirements.md` §4 out of scope).

## Observability
- Every case transition, tool call, and human action must be traceable end-to-end
  for both operational debugging and audit purposes — the audit log
  (`09-audit-and-compliance-spec.md`) doubles as the primary observability source,
  not a separate logging system.
