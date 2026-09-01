# 08 — Tool and data spec

> **Purpose:** the single source of truth for the Case data model and every
> external tool/API contract the agents depend on. Any field or tool referenced
> elsewhere in the specs should be defined here, not redefined locally.

## Case data model
```yaml
Case:
  case_id: string (UUID)
  trade_id: string
  counterparty_id: string
  product_type: enum [barrier_fx_option, barrier_rate_note, ...]
  status: enum [
    NEW, DETECTED, PRICES_PULLED, TERM_SHEET_RESOLVED,
    COMMS_DRAFTED, PENDING_ANALYST_APPROVAL, SENT,
    AWAITING_RESPONSE, RESPONSE_RECEIVED, AGREED,
    DISPUTED, ESCALATED, AWAITING_CLARIFICATION,
    RESOLVED, CLOSED, CANCELLED
  ]
  detected_at: datetime
  sla_due_at: datetime
  clarification_loop_count: integer   # see 10-error-and-exception-spec.md
  internal_price:
    source: enum [pricing_system]
    value: decimal
    as_of: datetime
  external_prices:
    - source: enum [bloomberg, reuters, six]
      value: decimal
      as_of: datetime
      ticker: string
  divergence_bps: decimal
  fixing_source_ref: string        # from term sheet
  term_sheet_id: string
  term_sheet_extract:
    fixing_source_clause: string
    barrier_definition: string
    dispute_resolution_clause: string
  case_summary: string             # agent-generated, human-readable
  comms_thread:
    - message_id, direction[out/in], channel, sent_at, sender,
      structured_payload, raw_ref (pointer, not full body — see 09-audit-and-compliance-spec.md)
  human_gates:
    - gate_type: enum [pre_send_review, dispute_escalation]
      status: enum [pending, approved, rejected, reassigned]
      actor: string
      acted_at: datetime
      comments: string
  resolution:
    outcome: enum [agreed_internal, agreed_external, split, escalated_legal]
    final_price: decimal
    closed_by: enum [agent, human]
    closed_at: datetime
  audit_log: [ {step, actor, timestamp, input_ref, output_ref, model_version} ]
```

## External tool / API contracts

| Tool | Used by | Purpose | Notes |
|---|---|---|---|
| `email_parser_tool` | Agent 1, Agent 2 | Extract structured fields from inbound email | Must handle threaded replies via case_reference_id |
| `pricing_system_api` | Agent 1 | Fetch internal price for a trade | Read-only |
| `bloomberg_api` / `reuters_api` / `six_api` | Agent 1 | Fetch external reference prices | Via existing firm-licensed integration layer only — no scraping |
| `document_repository_api` + `term_sheet_extraction_tool` | Agent 1 | Retrieve and extract term sheet clauses | Targeted extraction, not full-document summarization |
| `notification_service` | Agent 1, Agent 2 | Alert internal parties / SMEs | |
| `dashboard_api` | Agent 2 | Populate SME dispute dashboard | |
| `booking_system_api` | Agent 2 | Write final resolution to break register | Least-privilege: scoped to break-record updates only |
| `case_db_write` / `audit_log_writer` | Both | Persist Case state and audit records | Audit log is append-only |

## Data handling
Structured fields go in the Case DB; raw email bodies go in a separate access-
controlled store, referenced by pointer — avoids sprawl of sensitive/counterparty
data across systems. See `09-audit-and-compliance-spec.md` for retention.
