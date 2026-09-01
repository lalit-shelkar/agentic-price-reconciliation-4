"""Agent 2 — Respond, Resolve, Close (spec 06).

OWNERSHIP: this package is owned by the Agent 2 branch (`feat/agent-2`). Do not
implement it from `feat/agent-1`; see OWNERSHIP.md.

The modules here ship as contract stubs: real signatures, real return types, bodies
raising `NotImplementedError`. That gives the Agent 2 author a surface to fill in,
and lets shared and Agent 1 tests import the package without depending on it being
finished.
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
