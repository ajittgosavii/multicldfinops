"""The multi-agent graph: a hand-rolled supervisor over four react specialists.

Why hand-rolled. `langgraph-supervisor` exists and would do this in three lines,
but making it a hard dependency buys a version-coupling headache for a pattern
that is a dozen lines of `Command` routing. So we import it only to note its
presence and otherwise build the graph ourselves -- one fewer thing that can
pin us to a langgraph point release.

The routing itself is the small-model-first lever (G3 in the optimizer's own
playbook) applied to us: the supervisor's routing decision runs on the cheap
model (`cfg.openai_model_fast`), and only the specialists -- who actually reason
over tool output -- run on the workhorse (`cfg.openai_model`). The platform
practises the economics it preaches.

Streaming is synchronous on purpose. Streamlit reruns the whole script on every
interaction; calling `asyncio.run(...)` per rerun is a well-known way to end up
with "event loop is closed" and half-consumed generators. `graph.stream(...)` is
a plain generator, so `st.write_stream` consumes it directly.
"""

from __future__ import annotations

from typing import Dict, List, Literal, Optional

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import BaseModel, Field

from finops_core import AppConfig, DataContext
from agents import prompts
from agents.state import FinOpsState
from agents.tools import make_tools, missing_tools

# langgraph-supervisor is optional; we prefer the hand-rolled graph. Importing
# it here is only so the UI can report whether the managed variant is available.
try:
    import langgraph_supervisor as _supervisor_pkg  # noqa: F401

    SUPERVISOR_PKG_AVAILABLE = True
except Exception:
    SUPERVISOR_PKG_AVAILABLE = False


SPECIALISTS = ("analyst", "forecaster", "optimizer", "governor")

# How many tools each specialist may see. Narrow toolsets keep each agent's
# decisions inside its Framework domain and cut token cost per call.
_TOOL_SUBSETS: Dict[str, List[str]] = {
    "analyst": [
        "get_spend_summary", "get_executive_kpis", "query_spend",
        "list_focus_columns", "get_anomalies", "get_allocation", "get_allocation_coverage",
    ],
    "forecaster": [
        "get_forecast", "get_budget_variance", "get_spend_summary", "get_executive_kpis",
    ],
    "optimizer": [
        "find_optimization_opportunities", "explain_lever",
        "get_commitment_position", "get_executive_kpis",
    ],
    "governor": [
        "get_allocation_coverage", "get_allocation", "get_executive_kpis",
        "query_spend", "list_focus_columns",
    ],
}

_PROMPT_FOR = {
    "analyst": prompts.analyst_prompt,
    "forecaster": prompts.forecaster_prompt,
    "optimizer": prompts.optimizer_prompt,
    "governor": prompts.governor_prompt,
}

# The supervisor forces FINISH once iterations crosses this. A mis-routing loop
# would otherwise burn tokens until it hit the recursion limit.
_MAX_ITERATIONS = 8


class Route(BaseModel):
    """The supervisor's structured routing decision."""

    next: Literal["analyst", "forecaster", "optimizer", "governor", "FINISH"] = Field(
        description="Which specialist handles the next unanswered part, or FINISH."
    )
    reason: str = Field(description="One line explaining the choice.")


# ------------------------------------------------------------------------
# Availability
# ------------------------------------------------------------------------


def openai_available(cfg: AppConfig) -> bool:
    """True only if we could actually talk to OpenAI: a key AND the client lib.

    Cheap to call every rerun; the UI uses it to decide whether to render the
    team or the "set OPENAI_API_KEY" placeholder.
    """
    if not getattr(cfg, "openai_api_key", None):
        return False
    try:
        import langchain_openai  # noqa: F401
    except Exception:
        return False
    return True


def _llm(model: str, cfg: AppConfig, **kwargs):
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=model, api_key=cfg.openai_api_key, **kwargs)


# ------------------------------------------------------------------------
# Graph construction
# ------------------------------------------------------------------------


def build_graph(cfg: AppConfig, ctx: DataContext):
    """Compile the supervisor graph for one config + data context.

    Not cached here -- see `get_graph`, which is the entry point callers use so
    the compile happens once per Streamlit session rather than once per rerun.
    """
    from langgraph.prebuilt import create_react_agent

    tools = make_tools(ctx)
    by_name = {t.name: t for t in tools}

    fast = _llm(cfg.openai_model_fast, cfg)
    strong = _llm(cfg.openai_model, cfg)
    router = fast.with_structured_output(Route)

    # -- supervisor node ------------------------------------------------
    def supervisor(state: FinOpsState) -> Command:
        iterations = int(state.get("iterations", 0)) + 1
        persona = state.get("persona", "Leadership")

        if iterations > _MAX_ITERATIONS:
            return Command(goto=END, update={"iterations": iterations, "next_agent": "FINISH"})

        sys = SystemMessage(content=prompts.supervisor_prompt(persona))
        decision: Route = router.invoke([sys] + list(state["messages"]))

        if decision.next == "FINISH":
            return Command(goto=END, update={"iterations": iterations, "next_agent": "FINISH"})

        return Command(goto=decision.next, update={"iterations": iterations, "next_agent": decision.next})

    # -- one react specialist per domain --------------------------------
    def _make_specialist(name: str):
        subset = [by_name[n] for n in _TOOL_SUBSETS[name] if n in by_name]
        prompt_text = _PROMPT_FOR[name]("Leadership")  # persona re-injected per run below
        agent = create_react_agent(strong, tools=subset, prompt=prompt_text, name=name)

        # Node functions take (state, config) so the react subgraph inherits the
        # parent run's streaming callbacks -- without the config, token streaming
        # from the nested agent would be invisible to graph.stream(stream_mode=...).
        def node(state: FinOpsState, config=None) -> Command:
            persona = state.get("persona", "Leadership")
            sys = SystemMessage(content=_PROMPT_FOR[name](persona))
            inp = {"messages": [sys] + list(state["messages"])}
            result = agent.invoke(inp, config)
            last = result["messages"][-1]
            answer = AIMessage(content=last.content, name=name)
            finding = {"agent": name, "content": _first_line(last.content)}
            return Command(
                goto="supervisor",
                update={"messages": [answer], "findings": state.get("findings", []) + [finding]},
            )

        return node

    g = StateGraph(FinOpsState)
    g.add_node("supervisor", supervisor)
    for name in SPECIALISTS:
        g.add_node(name, _make_specialist(name))
    g.add_edge(START, "supervisor")
    # Every specialist routes back to the supervisor via Command(goto=...), so no
    # static edges out of them are needed; the supervisor owns all routing.

    return g.compile(checkpointer=MemorySaver())


def _first_line(text) -> str:
    if isinstance(text, list):
        text = " ".join(str(t) for t in text)
    s = str(text).strip().splitlines()
    return (s[0] if s else "")[:280]


# ------------------------------------------------------------------------
# Cached accessor -- safe to call on every Streamlit rerun.
# ------------------------------------------------------------------------

try:
    import streamlit as st

    _HAS_ST = True
except Exception:
    _HAS_ST = False


if _HAS_ST:

    @st.cache_resource(show_spinner=False)
    def _cached_graph(_cfg: AppConfig, _ctx: DataContext, cache_key: tuple):
        # Leading-underscore args are excluded from Streamlit's hashing; the
        # explicit hashable cache_key is what identifies the cache entry.
        return build_graph(_cfg, _ctx)

    def get_graph(cfg: AppConfig, ctx: DataContext):
        key = (cfg.openai_model, cfg.openai_model_fast, bool(cfg.openai_api_key), id(ctx))
        return _cached_graph(cfg, ctx, key)

else:
    _GRAPH_CACHE: Dict[tuple, object] = {}

    def get_graph(cfg: AppConfig, ctx: DataContext):
        key = (cfg.openai_model, cfg.openai_model_fast, bool(cfg.openai_api_key), id(ctx))
        if key not in _GRAPH_CACHE:
            _GRAPH_CACHE[key] = build_graph(cfg, ctx)
        return _GRAPH_CACHE[key]


# ------------------------------------------------------------------------
# Running
# ------------------------------------------------------------------------

_NO_KEY_MESSAGE = (
    "The AI team is offline because OPENAI_API_KEY is not set.\n\n"
    "With a key, a supervisor would route your question to one of four "
    "specialists -- Analyst (spend, allocation, anomalies), Forecaster "
    "(forecast, budget variance, unit economics), Optimizer (savings levers, "
    "commitment position) or Governor (tagging, chargeback readiness) -- each "
    "of which answers from the live FOCUS data using the same KPI engine the "
    "dashboards use, and cites the tool behind every figure.\n\n"
    "Set OPENAI_API_KEY in the environment or Streamlit secrets to enable it."
)


def run(question: str, cfg: AppConfig, ctx: DataContext, thread_id: str, persona: str = "Leadership"):
    """Stream the team's answer token by token, for `st.write_stream`.

    Yields plain strings. Never raises for the common failure -- a missing key
    yields one explanatory message instead, so the UI degrades to a helpful note
    rather than a traceback.

    `thread_id` is mandatory because the graph carries a checkpointer, and
    LangGraph refuses to run a checkpointed graph without a thread to write to.
    """
    if not openai_available(cfg):
        yield _NO_KEY_MESSAGE
        return

    graph = get_graph(cfg, ctx)
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}
    inputs: FinOpsState = {
        "messages": [HumanMessage(content=question)],
        "question": question,
        "findings": [],
        "next_agent": "",
        "iterations": 0,
        "persona": persona,
    }

    for chunk, meta in graph.stream(inputs, config, stream_mode="messages"):
        node = (meta or {}).get("langgraph_node")
        if node == "supervisor":
            continue  # routing decisions are not shown to the user
        if not isinstance(chunk, AIMessageChunk):
            continue  # skip ToolMessages and other non-assistant traffic
        if getattr(chunk, "tool_calls", None) or getattr(chunk, "tool_call_chunks", None):
            continue  # skip the tool-call turns; we want the written answer only
        text = chunk.content
        if isinstance(text, list):
            text = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in text)
        if text:
            yield text


def run_sync(question: str, cfg: AppConfig, ctx: DataContext, thread_id: str, persona: str = "Leadership") -> str:
    """Collect the whole streamed answer into one string."""
    return "".join(run(question, cfg, ctx, thread_id, persona=persona))


# ------------------------------------------------------------------------
# UI metadata
# ------------------------------------------------------------------------


def graph_diagram() -> str:
    """Mermaid text for the team topology, rendered on the AI tab."""
    return (
        "graph TD\n"
        "    START([User question]) --> SUP{{Supervisor<br/>routes on gpt-5-mini}}\n"
        "    SUP -->|Understand Usage & Cost| ANA[Analyst]\n"
        "    SUP -->|Quantify Business Value| FOR[Forecaster]\n"
        "    SUP -->|Optimize Usage & Cost| OPT[Optimizer]\n"
        "    SUP -->|Manage the Practice| GOV[Governor]\n"
        "    ANA -->|findings| SUP\n"
        "    FOR -->|findings| SUP\n"
        "    OPT -->|findings| SUP\n"
        "    GOV -->|findings| SUP\n"
        "    SUP -->|FINISH| DONE([Answer])\n"
    )


def _cards() -> List[dict]:
    return [
        {
            "name": "Supervisor",
            "domain": "Routing",
            "capabilities": ["Persona-aware routing", "Loop guard", "FINISH decision"],
            "tools": [],
            "model": "openai_model_fast (gpt-5-mini)",
        },
        {
            "name": "Analyst",
            "domain": "Understand Usage and Cost",
            "capabilities": ["Reporting and Analytics", "Allocation", "Anomaly Management"],
            "tools": _TOOL_SUBSETS["analyst"],
            "model": "openai_model (gpt-5)",
        },
        {
            "name": "Forecaster",
            "domain": "Quantify Business Value",
            "capabilities": ["Forecasting", "Budgeting", "Unit Economics"],
            "tools": _TOOL_SUBSETS["forecaster"],
            "model": "openai_model (gpt-5)",
        },
        {
            "name": "Optimizer",
            "domain": "Optimize Usage and Cost",
            "capabilities": ["Rate Optimization", "Workload Optimization", "Architecting for the Cloud"],
            "tools": _TOOL_SUBSETS["optimizer"],
            "model": "openai_model (gpt-5)",
        },
        {
            "name": "Governor",
            "domain": "Manage the FinOps Practice",
            "capabilities": ["Policy and Governance", "Invoicing and Chargeback", "Allocation"],
            "tools": _TOOL_SUBSETS["governor"],
            "model": "openai_model (gpt-5)",
        },
    ]


AGENT_CARDS: List[dict] = _cards()
