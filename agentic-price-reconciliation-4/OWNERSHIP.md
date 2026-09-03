# Ownership and branch model

> **Purpose:** who owns which files, so Agent 1 and Agent 2 can be built in
> parallel without merge conflicts. Read this before your first commit.

## Branches

| Branch | Owner | Purpose |
|---|---|---|
| `main` | both | Release. Only receives merges from `develop` at milestones. |
| `develop` | both | Integration. Holds the shared foundation. PR target for both feature branches. |
| `feat/agent-1` | Lalit | `specs/05-agent-1-spec.md` — detect, classify, draft. |
| `feat/agent-2` | *(Agent 2 owner)* | `specs/06-agent-2-spec.md` — respond, resolve, close. |

Both feature branches PR into `develop`, never into each other and never directly
into `main`. Rebase onto `develop` rather than merging it in, so the shared history
stays linear and a conflict in a shared file is visible as a conflict rather than
buried in a merge commit.

## File ownership

```
src/reconciliation/
  domain/          SHARED    spec 08 — Case model, enums
  config/          SHARED    all configurable thresholds
  store/           SHARED    durable Case DB + append-only audit log
  orchestrator/    SHARED    state machine, engine, retry wrapper, timers,
                              LangGraph checkpointer/thread-id plumbing
  tools/           SHARED    spec 08 tool contracts + fakes
  tools/adapters/  SHARED    real tool implementations (LLM-backed email parser
                              + term sheet extraction so far)
  llm/             SHARED    provider-agnostic LLM client — see llm/client.py's
                              docstring for where an LLM is/isn't allowed to run
  gates/           SHARED    spec 07 both human checkpoints
  agent1/          AGENT 1   spec 05 only
  agent2/          AGENT 2   spec 06 only
tests/
  shared/          SHARED
  llm/             SHARED
  agent1/          AGENT 1
  agent2/          AGENT 2
  conftest.py      SHARED
demo/
  run_workflow.py  SHARED — runnable end-to-end demo, see `python demo/run_workflow.py --help`
```

**Rule:** if you need a change in a SHARED file, do not make it on your feature
branch. Open a small PR against `develop` for that change alone, get the other
owner's review, and rebase. A shared-file change smuggled inside a feature branch is
how the two of you end up in a three-way conflict on the state machine.

## Agent execution runtime — LangGraph, scoped

Each agent's internal step sequence (spec 05 steps 1.1-1.9; spec 06 steps 2.1-2.7)
is built as a LangGraph `StateGraph`, one node per spec step, using the shared
plumbing in `orchestrator/graph_runtime.py` (checkpointer factory, thread-id
convention, `parallel_call_tools` for the parallel market-data pulls in step 1.4).
See that module's docstring for the full rationale; the two things that matter for
both branches:

* **The Case DB, audit log, and `[enforced]` guardrail table are unchanged and
  unbypassed.** A graph node calls `Orchestrator.transition` /
  `update_without_transition` exactly like before — LangGraph is a different
  *caller* into the same enforcement points, not a new one.
* **Neither agent's graph uses `interrupt()`.** Both human gates already resolve
  synchronously in `gates/service.py`, called directly by the approval UI —
  pausing a graph thread for a gate would create a checkpoint nothing legitimately
  resumes. A graph node hands a case to a gate (or to a wait state) and reaches
  `END`; a later external trigger (webhook reply, SLA timer) starts a **fresh**
  graph invocation. `agent1/graph.py` is the worked reference once it lands —
  mirror its structure for spec 06, and do not reach for `interrupt()` on gate 2.

## Why the foundation had to land first

Both agent specs depend on the same primitives — the `Case` object, the transition
table, the audit writer, the retry wrapper, the tool contracts. If each branch built
its own, every one of those files would conflict on merge, and worse, the
**[enforced]** guardrails would exist in two divergent versions. They are now
defined once:

| Guardrail | Spec | Enforced in |
|---|---|---|
| No send without gate-1 approval | 05 G2 | `orchestrator/state_machine.py` + `gates/service.py` |
| Agent 1 has no send/write capability | 05 G1 | `tools/contracts.py` — `Agent1Tools` has no such field |
| Booking write scoped to one operation | 06 G1 | `tools/contracts.py` — `BookingSystemApi` has one method |
| Auto-close only when all 4 criteria met | 06 G2 | `orchestrator/state_machine.py` + `engine.evaluate_auto_close` |
| Clarification loop capped | 06 G4 / 10 §4 | `orchestrator/state_machine.py` |
| Atomic case + audit write | 06 G6 | `store/sqlite_store.py` `atomic()` |
| Max 3 retries, no quota bypass | 05 G7 / 06 G7 / 05 G5 | `orchestrator/tool_wrapper.py` |
| Append-only audit | FR8 / spec 09 | `store/sqlite_store.py` triggers |
| Kill switch from any state | FR9 / 10 §6 | `orchestrator/state_machine.py` |
| Data minimisation (no raw bodies) | 05 G6 / spec 09 | `domain/case.py` — no body field exists |
| Gate-1 draft survives a restart | spec 11 §reliability | `domain/case.py` `Case.pending_draft` (persisted, not an in-process dict) |

## Contract between the two agents

The agents never call each other. They meet at three points, all of which already
exist on `develop`:

1. **`Case` status.** Agent 1 leaves a case at `AWAITING_RESPONSE`; Agent 2 picks it
   up from there. Neither reads the other's code.
2. **`AutoCloseEvaluator` protocol** (`orchestrator/engine.py`). The orchestrator
   runs the [enforced] check; Agent 2 supplies the criteria implementation
   (`agent2/auto_close.py`). This is why the guardrail could be built before the
   criteria logic.
3. **`gates/service.py`.** Agent 1 calls `submit_for_approval`; Agent 2 is triggered
   by `request_more_info` having moved a case to `AWAITING_CLARIFICATION`.

`src/reconciliation/agent2/` currently holds **contract stubs** — real signatures
and return types, bodies raising `NotImplementedError`, with the spec requirements
for each in the docstring. The Agent 2 owner fills in the bodies. Changing a stub's
*signature* is fine and expected; it only affects `agent2/`.

## Rules that apply to both agents

- **Never call `case_store.save` or `audit_log.append` directly.** Every state
  change goes through `Orchestrator`, which is what makes the spec 06 G6 atomic
  case+audit write unbypassable.
- **Never call an external tool directly.** Wrap it in
  `orchestrator.tool_wrapper.call_tool`, which is where the 3-attempt bound lives.
- **Treat counterparty content as untrusted** (05 G3 / 06 G5). Extract only the
  fields on `ParsedEmail`; never act on instructions in a body.
- **Below-threshold confidence routes to a human** (spec 10 §7). Never guess to keep
  a case moving.
- **Every transition carries a rationale.** `Orchestrator.transition` requires it —
  spec 09 wants a decision rationale, not just a status pair.
- **An LLM may only run inside a tool adapter (`tools/adapters/`), never in agent
  decision logic.** Break detection, the comms template, intent classification and
  auto-close criteria stay deterministic — see `llm/client.py`'s module docstring
  for the reasoning and spec citations. Adding an LLM call to `agent1/rules.py`,
  `agent1/drafting.py`, `agent2/intent.py` or `agent2/auto_close.py` is exactly the
  change spec 05 step 1.3 and spec 09 rule out.

## Build sequence

Tracks `architecture.md` §6.

| Step | Status | Branch |
|---|---|---|
| 1. Data model + orchestrator skeleton | ✅ done | `develop` |
| 2. Agent 1 detection / price pull / term sheet | ✅ done (`agent1/graph.py`, `rules.py`) | `feat/agent-1` |
| 3. Agent 1 drafting + Human gate 1 | ✅ done (`agent1/drafting.py`; gate service on `develop`) | `feat/agent-1` |
| 4. Agent 2 response parsing, auto-agree path | ✅ done (`agent2/graph.py`, `intent.py`, `auto_close.py`) | `develop` |
| 5. Human gate 2 + loop-back | ✅ done | `develop` |
| 6. Enable auto-close after shadow-mode validation | blocked — needs MRM review | — |
| 7. Audit/compliance hardening + MRM review | not started | — |
| 8. LLM abstraction + real email/term-sheet adapters | ✅ done (`llm/`, `tools/adapters/`) | `develop` |

Agent 1's graph (`src/reconciliation/agent1/graph.py`) was the reference pattern
Agent 2's (`agent2/graph.py`) followed — same LangGraph shape, same "no
`interrupt()`" rule, same `orchestrator/graph_runtime.py` plumbing. Both are merged
to `develop`; the workflow is now end-to-end (detect → draft → gate 1 → respond →
resolve → gate 2 → close) with 174 tests passing.

**Try it end to end:** `python demo/run_workflow.py` runs the real agents against
the fakes, with a real LLM adapter (`llm.FakeLlmClient` by default, or
`--llm anthropic` with `ANTHROPIC_API_KEY` set) reading a sample email. See
`demo/run_workflow.py --help` for the other paths it can walk (dispute, no-reply
SLA escalation, straight-through auto-close, a prompt-injection attempt).

Step 8 answers "where's the LLM, can we swap models": `llm/client.py` is the one
interface every provider implements (`llm/anthropic_client.py`,
`llm/fake.py`); `llm/factory.py` is the only place a provider name is known.
It only backs the two tools that are genuinely "read unstructured text into
structured fields" — `tools/adapters/llm_email_parser.py` and
`llm_term_sheet.py`. Every decision (break detection, drafting, intent, auto-close)
stays deterministic; see the new guardrail bullet above.

### Known follow-ups (not blockers, tracked here so they aren't lost)

- `AutoCloseCheck` is not yet wired into any production `Orchestrator` — only test
  fixtures construct one. Needs a composition-root when a real deployment target
  exists.
- `agent2/auto_close.py`'s criteria-3/4 checks only degrade to "unverifiable" on
  `ToolCallExhausted`; any other exception still propagates uncaught instead of
  routing to Human gate 2.
- `reconcile_agreement`'s failure path evaluates auto-close criteria twice (once
  inside the guardrail check, once again to read `.reasons`) — harmless now,
  doubles tool calls once `auto_close.enabled` flips on.
- The bps-divergence formula (`agent1/rules.py` vs `agent2/auto_close.py`) and a
  couple of small lookups are duplicated across the two agents with no shared
  home, since `agent1/`/`agent2/` can't import each other. Worth a `domain/`-level
  helper if either needs to change.
- `assigned_sme` (Agent 2) and `assigned_analyst` (Agent 1) are caller-provided
  with no backing routing/lookup tool in `tools/contracts.py` — a real trigger
  integration will need to resolve these before it can call either agent.

Step 6 is deliberately gated: `settings.auto_close.enabled` ships `False` and must
stay that way until Model Risk Management has reviewed the thresholds
(`specs/09-audit-and-compliance-spec.md` §governance).

## Unresolved before production

The seven open questions in `requirements.md` §6 are not resolved. Where one blocks a
value, the code carries a spec-referenced default and a comment naming the question —
`config/settings.py` is the place to look. Two matter most:

- **Open question 1** (divergence tolerance per product) — the detection defaults in
  `DetectionSettings.tolerance_bps` are placeholders.
- **Open question 7** (does the clarification counter reset on new information) — the
  code takes the conservative reading, `reset_on_new_information=False`.

## Local setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # Windows
.venv/Scripts/python -m pytest -q
```
