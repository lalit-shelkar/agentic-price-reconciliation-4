# Agentic price reconciliation

Reimagines the manual price/barrier source-divergence break reconciliation process
(7 manual steps, no agent) as a 2-agent, human-on-exception workflow.

## Reading order

1. [`requirements.md`](./requirements.md) — business problem, goals, scope, success metrics
2. [`architecture.md`](./architecture.md) — system design, Case data model, orchestration pattern
3. `specs/` — implementation-ready specs, one file per concern:

| File | Covers |
|---|---|
| `01-business-context.md` | Why this project exists |
| `02-as-is-process.md` | Current manual process, step by step |
| `03-to-be-process.md` | Target agentic process, step by step |
| `04-functional-requirements.md` | What the system must do |
| `05-agent-1-spec.md` | Detect, classify, draft |
| `06-agent-2-spec.md` | Respond, resolve, close (incl. dispute loop-back) |
| `07-human-gate-spec.md` | Both human checkpoints |
| `08-tool-and-data-spec.md` | Case data model + external tool/API contracts |
| `09-audit-and-compliance-spec.md` | FINMA / audit trail requirements |
| `10-error-and-exception-spec.md` | Failure modes, retries, loop guards, kill switch |
| `11-non-functional-spec.md` | SLAs, security, reliability |

## Status
Draft v0.1 — open questions tracked in `requirements.md` §6 must be resolved with
stakeholders before implementation starts.
