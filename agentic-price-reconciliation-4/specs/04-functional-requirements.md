# 04 — Functional requirements

> **Purpose:** testable, SHALL-level statements of what the system must do.
> Guardrails (what agents must *not* do) live separately in the agent specs —
> see the note at the end of this file.

## FR1 — Detection
The system SHALL detect a price/barrier divergence between the internal pricing
system and a counterparty communication, applying a configurable per-product-type
basis-point tolerance to distinguish real breaks from noise.

## FR2 — Price & term sheet retrieval
The system SHALL retrieve reference prices from Bloomberg, Reuters, and SIX in
parallel, and SHALL retrieve and extract the governing term sheet's fixing-source
clause for the affected trade.

## FR3 — Case creation
The system SHALL create a single Case record per detected break, containing all
fields defined in `08-tool-and-data-spec.md`, before any outbound communication occurs.

## FR4 — Structured outbound communication
The system SHALL draft outbound counterparty communication using the fixed
structured template in `05-agent-1-spec.md` §comms template — not free text — and
SHALL NOT send it without passing Human gate 1.

## FR5 — Response classification
The system SHALL classify inbound counterparty responses into AGREE / DISPUTE /
PARTIAL / NO_RESPONSE with a confidence score; low-confidence classifications SHALL
route to human review by default.

## FR6 — Auto-close
The system SHALL close a case without human sign-off only when all criteria in
`06-agent-2-spec.md` §auto-close criteria are met.

## FR7 — Dispute handling with no dead ends
Every path out of Human gate 2 SHALL resolve to either (a) a resumed wait state via
Agent 2, or (b) RESOLVED → CLOSED. No case status SHALL exist without a defined
next action (see `10-error-and-exception-spec.md`).

## FR8 — Audit logging
The system SHALL write an immutable, timestamped audit record for every state
transition, including actor identity, input/output references, and model version.

## FR9 — Human override
A human SHALL be able to pull any case out of automated flow into fully manual
handling at any point ("kill switch").

---

**Note — guardrails are not repeated here.** These FRs describe what the system
must *do*; the corresponding constraints on what each agent must *not* do (write
scope, send permissions, prompt-injection handling, retry limits) are defined once,
per agent, in the **Guardrails / security constraints** tables in
`05-agent-1-spec.md` and `06-agent-2-spec.md` — not duplicated here to avoid the
two going out of sync. Read those tables alongside this file before implementation;
several guardrails (e.g. G1 in both specs) are what actually make FR4, FR6, and FR9
enforceable rather than just stated.

