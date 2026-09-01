# 02 — AS-IS process (manual, 7 steps, no agent)

> **Purpose:** documents the current manual process as the baseline that the
> success metrics in `requirements.md` §5 are measured against, and the source of
> the pain points each TO-BE design decision responds to.

| # | Step | Owner | Detail | Pain point |
|---|---|---|---|---|
| 1 | Barrier status generated; analyst detects mismatch | System + Analyst | Pricing system flags status; analyst notices counterparty disagrees | Manual trigger, delay |
| 2 | Pull prices manually | Analyst | Bloomberg, Reuters, SIX — multiple tabs | ~45 min |
| 3 | Retrieve term sheet | Analyst | Find correct contractual fixing source | ~30 min |
| 4 | Send unstructured email | Analyst | Freeform query, no template, no evidence | No audit trail |
| 5 | Wait for response | Counterparty | No SLA enforced | Hours–days |
| 6 | Ad hoc escalation | Analyst | Unstructured, no workflow if disputed | Inconsistent |
| 7 | Manual audit log | Analyst | Analyst updates record, often incomplete | FINMA risk |

**Total analyst hands-on time:** ~75+ minutes across steps 2–3, plus unbounded time
in steps 4/6/7 which have no structure or SLA.
