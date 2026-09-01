# 01 — Business context

> **Purpose:** explains why this project exists and who its stakeholders are —
> the rationale behind the goals stated in `requirements.md`.

## Why this project exists
Barrier-triggered products are priced off a firm's internal pricing system, but
counterparties may independently price the same instrument and quote a different
value or barrier status. When these disagree, it's a "break" that must be
reconciled against the trade's contractual fixing source before it can be closed.

Today this is entirely manual (see `02-as-is-process.md`), owned by a Mid-Office
Analyst, with no SLA, no structured audit trail, and inconsistent escalation —
creating both operational cost and regulatory (FINMA) exposure.

## Stakeholders
- **Mid-Office Analyst** — current process owner; becomes the Human gate 1 approver.
- **SME / senior analyst** — becomes the Human gate 2 approver for disputes.
- **Legal** — final escalation path for genuine contractual disputes.
- **Compliance / Model Risk Management** — governance sign-off given FINMA context
  and the fact that agents make price-reconciliation decisions.
- **Trading desk** — alerted on break detection; not a workflow actor.

## Regulatory driver
FINMA audit/inspection risk from incomplete manual logs is the primary compliance
driver for structured, immutable audit logging (`09-audit-and-compliance-spec.md`).
