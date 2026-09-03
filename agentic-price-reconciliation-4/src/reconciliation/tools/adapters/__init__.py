"""Real tool adapters implementing the `tools/contracts.py` protocols.

`tools/fakes.py` covers every contract for tests; this package holds
implementations meant for a real deployment. Currently the two LLM-backed
text-extraction tools, which are the contracts where an LLM legitimately belongs
(see `llm/client.py`). The remaining adapters — pricing system, the three market
data feeds, document repository, notifications, dashboard, booking system — are
still to be written against whatever systems the firm actually runs; several are
blocked on open questions in `requirements.md` §6.
"""

from .llm_email_parser import PROMPT_VERSION as EMAIL_PROMPT_VERSION
from .llm_email_parser import LlmEmailParser
from .llm_term_sheet import PROMPT_VERSION as TERM_SHEET_PROMPT_VERSION
from .llm_term_sheet import LlmTermSheetExtraction
from .raw_store import (
    FileRawMessageStore,
    InMemoryRawMessageStore,
    RawMessageNotFound,
    RawMessageStore,
    RawRecord,
)

__all__ = [
    "EMAIL_PROMPT_VERSION",
    "FileRawMessageStore",
    "InMemoryRawMessageStore",
    "LlmEmailParser",
    "LlmTermSheetExtraction",
    "RawMessageNotFound",
    "RawMessageStore",
    "RawRecord",
    "TERM_SHEET_PROMPT_VERSION",
]
