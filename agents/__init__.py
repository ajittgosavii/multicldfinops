"""The OpenAI + LangGraph multi-agent layer for the FinOps Command Center.

A supervisor routes a persona's question to one of four specialists, each owning
one domain of the FinOps Framework and each answering strictly from the live
FOCUS data via the same KPI engine the dashboards use. The design goals, in
order: never invent a number, degrade gracefully when an analytics engine or the
OpenAI key is absent, and cost as little as the platform's own advice implies
(cheap model routes, workhorse reasons).

Importing this package must not require an OpenAI key or a Streamlit runtime --
only the heavy graph construction touches those, and only when actually run. So
the public surface is safe to import in a test or a script with nothing set.
"""

from __future__ import annotations

from agents.state import FinOpsState
from agents.tools import make_tools, missing_tools
from agents.graph import (
    AGENT_CARDS,
    Route,
    SPECIALISTS,
    build_graph,
    get_graph,
    graph_diagram,
    openai_available,
    run,
    run_sync,
)

__all__ = [
    "FinOpsState",
    "make_tools",
    "missing_tools",
    "AGENT_CARDS",
    "Route",
    "SPECIALISTS",
    "build_graph",
    "get_graph",
    "graph_diagram",
    "openai_available",
    "run",
    "run_sync",
]
