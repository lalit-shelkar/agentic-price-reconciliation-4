# Architecture

> **Purpose:** technical "how." Defines the system design, component boundaries,
> and the Case state machine that every spec in `specs/` implements against. Read
> `requirements.md` first for the business context this design serves.

## 1. Pattern
**Orchestrator-worker**: a durable state machine (the Case object) owns state
transitions; Agent 1 and Agent 2 are stateless, tool-using LLM agents invoked by
the orchestrator at defined states. Human gates are first-class states — the state
machine cannot transition past a gate without an explicit approve/reject/escalate
action recorded with actor identity and timestamp.

Full data contract lives in `specs/08-tool-and-data-spec.md`.

## 2. Why 2 agents, not one-per-task
Tool calls (pulling a Bloomberg price, writing to the case DB) are not agents —
they're deterministic API calls inside an agent's turn. A new agent boundary is
only justified when one of these holds:
1. A real async/time gap requires persisted state (waiting hours–days for a
   counterparty reply).
2. Different specialized reasoning/context is needed (pricing reasoning vs.
   response-intent/dispute judgment).
3. Different trust/permission boundaries (Agent 2 can write to the booking system;
   Agent 1 can't).

Agent 1 / Agent 2 are split at the one place that matters — the wait-for-external-
response boundary — not by task count.

## 3. Component diagram

```
┌────────────────────────────────────────────────────────────────────┐
│                         Orchestration Layer                        │
│         (durable workflow engine — owns the Case object)            │
└───────────────┬─────────────────────────────────┬──────────────────┘
                 │                                 │
        ┌────────▼────────┐               ┌────────▼────────┐
        │     AGENT 1      │               │     AGENT 2      │
        │  Detect·Classify │               │ Respond·Resolve │
        │  ·Draft Comms    │               │ ·Escalate·Close │
        └───┬─────────┬────┘               └───┬─────────┬────┘
            │         │                        │         │
   ┌────────▼──┐  ┌───▼────────┐      ┌────────▼──┐ ┌────▼─────────┐
   │ Price Feed │  │ Term Sheet │      │  Inbox /  │ │  Case DB /   │
   │ Connectors │  │  Repository │      │  Email/   │ │  Audit Log / │
   │(Bloomberg, │  │ (DMS / CLM) │      │  Chat API │ │  Booking Sys │
   │ Reuters,SIX)│  └────────────┘      └───────────┘ └──────────────┘
   └────────────┘
                 │                                 │
        ┌────────▼─────────────────────────────────▼────────┐
        │           Human-in-the-Loop Gate Service            │
        │  (approval UI, notifications, escalation routing)   │
        └───────────────────────────────────────────────────┘
```

## 4. Workflow / state flow

Trigger (email or manual upload) → Agent 1 → Human gate 1 → wait for response →
Agent 2 → branch on intent:
- Agreed → auto-close (subject to `05-agent-2-spec.md` §criteria) → closed
- Disputed/no response → Human gate 2 → resolve, escalate to Legal, **or** request
  more info (loops back into Agent 2, re-arms the wait state — no dead ends) → closed

See `03-to-be-process.md` for the full step table and `10-error-and-exception-spec.md`
for the loop guard that bounds the clarification loop.

## 5. Technology recommendations
- **Orchestrator:** durable workflow engine (e.g. Temporal, AWS Step Functions, or
  equivalent) — required because of multi-day wait states; do not use an in-memory
  agent loop.
- **Agents:** LLM agents with structured/constrained outputs (not free text) for
  every field that feeds the audit log or the counterparty-facing email.
- **Data store:** Case DB with immutable audit log table, append-only.
- **Least privilege:** Agent 1 has no write access to booking systems; Agent 2's
  write scope is limited to the specific "update break record" operation.

## 6. Build sequence
1. Data model + orchestrator skeleton (Case object, state machine, manual-trigger stub).
2. Agent 1 detection/price-pull/term-sheet extraction — backtest against historical cases.
3. Agent 1 drafting + Human gate 1 (structured comms + approval UI).
4. Agent 2 response parsing, auto-agree path only — route everything else to human.
5. Human gate 2 (dispute escalation UI + context bundle + loop-back).
6. Enable auto-close only after shadow/human-confirmed mode validates classification accuracy.
7. Audit/compliance hardening + MRM review before production rollout.
