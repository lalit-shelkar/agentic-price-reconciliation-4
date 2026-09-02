"""Agent 2 — Respond, Resolve, Close (spec 06).

OWNERSHIP: this package is owned by the Agent 2 branch (`feat/agent-2`). Do not
implement it from `feat/agent-1`; see OWNERSHIP.md.

`intent.py` and `auto_close.py` are pure, deterministic logic modules; `graph.py`
wires them (plus the tool contracts and the orchestrator) into the four LangGraph
`StateGraph`s spec 06's steps require, one per external trigger — see that
module's docstring. `agent.py`'s `Agent2` class is the thin public entry point the
rest of the system calls.
"""

from .agent import Agent2, Agent2Result
from .auto_close import AutoCloseCheck
from .intent import IntentClassification, IntentClassifier

__all__ = [
    "Agent2",
    "Agent2Result",
    "AutoCloseCheck",
    "IntentClassification",
    "IntentClassifier",
]
