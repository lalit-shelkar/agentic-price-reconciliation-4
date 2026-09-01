# 09 — Audit and compliance spec

> **Purpose:** FINMA/regulatory-facing requirements for audit logging, retention,
> and governance sign-off — the compliance lens on top of the functional and
> architectural specs elsewhere in this repo.

Directly addresses the AS-IS "FINMA risk / incomplete manual log" pain point.

## Requirements
- Every state transition writes an **immutable, timestamped audit record**: actor
  (agent version or human user id), input references, output references, decision
  rationale.
- Structured comms (`05-agent-1-spec.md` §comms template) replace free-text email as
  the system of record — the audit trail is a byproduct of doing the work, not a
  separate manual step.
- Term sheet citations must reference the exact clause used, not just "term sheet
  reviewed" — supports FINMA lookback/inspection.
- Retention: align with existing firm policy for trade-related records (typically
  7+ years) — confirm with Compliance (`requirements.md` open question 5).

## Governance
- Given this workflow makes price-reconciliation decisions, confirm with Model Risk
  Management whether it falls under existing MRM policy before production rollout.
- Auto-close (`06-agent-2-spec.md`) should not go live until MRM has reviewed the
  classification confidence thresholds and auto-close criteria.

## Access & data handling
- Least privilege: Agent 1 has no write access to booking/settlement systems beyond
  the specific "update break record" scope.
- PII/counterparty-sensitive data: structured fields in the case DB; raw email
  bodies in a separate access-controlled store, referenced by pointer.
- External API calls (Bloomberg/Reuters/SIX) via the existing firm-licensed
  integration layer only.
