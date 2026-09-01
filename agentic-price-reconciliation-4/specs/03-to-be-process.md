# 03 — TO-BE process (agent-assisted, human-on-exception)

> **Purpose:** the target-state process at a glance, mapped step-by-step back to
> the AS-IS pain points it fixes. High-level orientation only — implementation
> detail for each step lives in the agent and human-gate specs it links to.

| # | Step | Owner | Detail | Target |
|---|---|---|---|---|
| 1 | Auto-trigger | Agent 1 | Reads counterparty email, detects mismatch | Automated, real-time |
| 2–3 | Ingest & classify | Agent 1 | Pulls price feeds + term sheet, spots mismatch, creates a case, alerts all parties | ~5 min (almost 90% efficiency gain vs. AS-IS steps 2–3) |
| 4 | Draft & send comms | Agent 1 + Human gate 1 | Agent creates structured email; analyst reviews & sends | Structured, audit-logged |
| 5 | Agent 2 receives response | Agent 2 | Parses response, alerts SME via dashboard if needed, auto-closes if agreed | SLA-bound; risk reduction |
| 6 | Human gate (disputes only) | Agent 2 + Human gate 2 | Counterparty-disputed breaks escalated with full context; **loops back into Agent 2 if more info is requested — no dead end** | Consistent, contextual |
| 7 | Auto audit & close | Agent 2 | Updates relevant systems/databases, closes break, generates audit record | Audit compliant |

## Mapping to AS-IS
AS-IS steps 2–3 (75 min manual) → TO-BE step 2–3 (~5 min agent).
AS-IS step 4 (no audit trail) → TO-BE step 4 (structured, human-approved).
AS-IS step 5 (no SLA) → TO-BE step 5 (SLA-bound wait state).
AS-IS step 6 (inconsistent) → TO-BE step 6 (consistent human gate, non-dead-end).
AS-IS step 7 (FINMA risk) → TO-BE step 7 (audit-compliant, generated automatically).

Full step-level agent behavior: `05-agent-1-spec.md`, `06-agent-2-spec.md`.
Human checkpoint detail: `07-human-gate-spec.md`.
