"""Provider-agnostic LLM contract.

## Where an LLM is allowed to run in this system

Only inside a **tool adapter**, never inside an agent's decision logic. The two
spec 08 tools whose job is genuinely "turn unstructured text into structured
fields" are `email_parser_tool` and `term_sheet_extraction_tool`; both already
exist as protocols in `tools/contracts.py`, so an LLM-backed implementation slots
in behind a boundary the agents already depend on and cannot see through.

Everything that *decides* anything stays deterministic and stays where it is:
break detection (`agent1/rules.py`), the comms template (`agent1/drafting.py`),
response-intent classification (`agent2/intent.py`) and the auto-close criteria
(`agent2/auto_close.py`). That split is a spec requirement, not a preference —
spec 05 step 1.3 requires the break decision be a deterministic threshold
comparison "to avoid non-determinism on a decision that directly gates whether a
counterparty gets contacted", and spec 09 needs a decision rationale a reviewer
can reproduce. An LLM extracting `quoted_price` from an email is a *reading* of
untrusted input; it is not a decision, and its output is validated against a
fixed schema before anything acts on it.

## Swapping providers

`LlmClient` is the only interface the adapters know about. To add a provider,
implement this protocol and register it in `llm/factory.py` — nothing in
`tools/adapters/`, `agent1/` or `agent2/` changes. `extract_json` takes a JSON
Schema because every current provider can constrain output to one (Anthropic via
a forced tool call, OpenAI via `response_format`, local models via grammar or
`format=json`), which is what makes "structured extraction" portable rather than
each provider needing its own parsing quirks.

## Determinism

`temperature` defaults to 0 in `LlmSettings` and should stay there. This runs in a
regulated workflow where spec 09 requires an auditor to be able to reconstruct why
a case was actioned; a sampled extraction that differs between two runs on the
same email undermines that. Temperature 0 is not a guarantee of identical output
across model versions, which is why `LlmExtraction.model_id` is recorded alongside
every result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class LlmError(Exception):
    """Base for LLM failures.

    Adapters translate these into the `ToolError` subclasses in
    `tools/contracts.py`, because that is the hierarchy
    `orchestrator/tool_wrapper.call_tool` already understands — the retry bound
    (spec 05 G7 / spec 06 G7: max 3 attempts) lives there and must not be
    duplicated inside an adapter.
    """


class LlmTimeout(LlmError):
    """Provider did not respond in time. Transient."""


class LlmUnavailable(LlmError):
    """Provider reachable but not serving (connection error, 5xx). Transient."""


class LlmQuotaExceeded(LlmError):
    """Rate limit or spend cap hit.

    Deliberately distinct from `LlmUnavailable`: spec 05 G5 forbids using retries
    to work around a quota, so adapters map this to the non-retryable
    `QuotaExceeded` rather than letting `call_tool` hammer a throttled endpoint.
    """


class LlmAuthError(LlmError):
    """Credentials missing, invalid, or not scoped for this call. Not transient."""


class LlmInvalidResponse(LlmError):
    """Provider returned something that isn't a usable structured result.

    Not retryable: the same prompt at temperature 0 will produce the same
    unusable output, so a retry burns quota for nothing.
    """


@dataclass(frozen=True)
class LlmExtraction:
    """One structured extraction, plus what produced it.

    `model_id` is carried through rather than assumed from config because spec 09
    wants the audit trail to name the model that actually read the document — a
    provider can silently resolve an alias like `claude-sonnet-5` to a dated
    snapshot, and "which model read this email" is exactly the question a
    post-incident review asks.
    """

    data: dict[str, Any]
    model_id: str
    input_tokens: int | None = None
    output_tokens: int | None = None


@runtime_checkable
class LlmClient(Protocol):
    """The whole provider surface this system uses. Deliberately one method.

    A narrow interface is the point: the less an adapter can ask an LLM to do, the
    less there is to review when someone changes providers. There is no
    free-form `complete()` here, because nothing in this codebase is allowed to
    act on free-form model output.
    """

    @property
    def model_id(self) -> str:
        """Configured model identifier, for logging and audit."""
        ...

    def extract_json(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict[str, Any],
        schema_name: str,
        schema_description: str = "",
        max_tokens: int | None = None,
    ) -> LlmExtraction:
        """Return a JSON object conforming to `json_schema`.

        Implementations must constrain the model to the schema rather than
        parsing prose, and must raise an `LlmError` subclass — never return a
        partial or guessed result — when they cannot.
        """
        ...
