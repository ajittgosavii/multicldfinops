"""The AI Copilot tab -- the LangGraph agent team.

Why this tab is built to degrade, not to demand:

* **No key is a first-class state.** A fresh clone has no `OPENAI_API_KEY`, and
  the tab must still explain what the team *would* do, list the specialists and
  the exact tool inventory they reason over, and draw the topology -- without
  raising. A blank "set a key" screen teaches nothing; this one is a spec.

* **Every figure is a tool call, never model memory.** The specialists answer
  only through the same KPI engine the dashboards use, bound to the live FOCUS
  frame. The supervisor routes on the cheap model and the specialists reason on
  the strong one -- the small-model-first lever the optimizer preaches, applied
  to ourselves.

* **A stable thread is mandatory.** The graph carries a checkpointer, so the
  conversation keys on `st.session_state['thread_id']` (set once by app.py). Mint
  a new one per turn and the team forgets the previous message.

This tab orchestrates; it computes no KPI of its own.
"""

from __future__ import annotations

import uuid

import streamlit as st

import ui
from finops_core import CORE_PERSONAS, DataContext

_SUGGESTIONS = [
    "Why did spend rise last month?",
    "Where is our commitment coverage weakest?",
    "What is our projected year-end variance?",
    "Which applications are not chargeback-ready?",
    "Give me the top 5 optimization levers with effort and risk.",
]


def render(ctx: DataContext) -> None:
    cfg = ctx.config

    # Guard: the whole agent layer is an optional import.
    try:
        import agents
        from agents import graph as agent_graph
    except Exception as exc:  # missing langchain/langgraph, etc.
        missing = getattr(exc, "name", None) or str(exc)
        ui.section("AI Copilot", "The agent team could not be imported.")
        ui.callout(
            f"The agent layer failed to import: `{exc}`. "
            f"Missing package: **{missing}**. Install the AI extras "
            "(`langchain-openai`, `langgraph`) to enable the Copilot."
        )
        return

    ui.section(
        "AI Copilot -- the FinOps agent team",
        f"{ctx.config.organisation} · a supervisor over four Framework specialists.",
    )

    persona = st.selectbox(
        "Ask as",
        list(CORE_PERSONAS.keys()),
        key="copilot_persona",
        help="Every prompt is tuned to the persona; the supervisor routes on it.",
    )
    st.caption(CORE_PERSONAS.get(persona, ""))

    # ---------------------------------------------------------------
    # Team panel + topology (always rendered)
    # ---------------------------------------------------------------
    _team_panel(agent_graph)
    _topology(agent_graph)

    st.divider()

    # ---------------------------------------------------------------
    # No-key path -- render a spec, not a wall.
    # ---------------------------------------------------------------
    if not cfg.ai_enabled:
        _offline_panel(ctx, agents)
        return

    # ---------------------------------------------------------------
    # Chat
    # ---------------------------------------------------------------
    _chat(ctx, agent_graph, persona)


# ==========================================================================
# Panels
# ==========================================================================


def _team_panel(agent_graph) -> None:
    ui.section("The team", "Each specialist owns one domain of the FinOps Framework.")
    cards = agent_graph.AGENT_CARDS
    cols = st.columns(len(cards)) if len(cards) <= 5 else st.columns(3)
    for i, card in enumerate(cards):
        with cols[i % len(cols)]:
            caps = "".join(f'<div class="mf-sub">· {c}</div>' for c in card.get("capabilities", []))
            tools = card.get("tools", [])
            tools_html = (
                f'<div class="mf-sub" style="margin-top:.3rem">{len(tools)} tool(s)</div>'
                if tools else '<div class="mf-sub" style="margin-top:.3rem">routing only</div>'
            )
            st.markdown(
                f'<div class="mf-tile">'
                f'<div class="mf-label">{card.get("name", "")}</div>'
                f'<div class="mf-sub" style="font-weight:560">{card.get("domain", "")}</div>'
                f"{caps}"
                f"{tools_html}"
                f'<div class="mf-sub" style="margin-top:.3rem">Model: {card.get("model", "")}</div>'
                f"</div>",
                unsafe_allow_html=True,
            )


def _topology(agent_graph) -> None:
    ui.section("Team topology", "Supervisor routes; specialists report findings back.")
    mermaid = agent_graph.graph_diagram()
    # Streamlit does not render mermaid natively. We show the fenced block (which
    # a repo docs viewer renders) and the raw source as a code fallback.
    st.markdown(f"```mermaid\n{mermaid}\n```")
    st.code(mermaid, language="mermaid")
    st.caption(
        "Streamlit renders this as source; a rendered diagram is available in the "
        "repo docs. The supervisor's routing runs on the cheap model, the "
        "specialists on the strong one."
    )


def _offline_panel(ctx: DataContext, agents) -> None:
    ui.section("Copilot is offline", "No OPENAI_API_KEY is set. Here is what it would do.")
    ui.callout(
        "With a key, a **supervisor** routes your question to one of four "
        "specialists -- **Analyst** (spend, allocation, anomalies), **Forecaster** "
        "(forecast, budget variance, unit economics), **Optimizer** (savings "
        "levers, commitment position) and **Governor** (tagging, chargeback "
        "readiness). Each answers from the live FOCUS frame via the same KPI "
        "engine the dashboards use, and cites the tool behind every figure."
    )

    st.markdown("**Set exactly one secret to enable it:** `OPENAI_API_KEY` "
                "(in the environment or Streamlit secrets).")

    ui.section("Tool inventory", "What the specialists would reason over -- bound to the live frame.")
    try:
        tools = agents.make_tools(ctx)
    except Exception as exc:
        ui.callout(f"Could not build the tool inventory: `{exc}`")
        tools = []

    missing = []
    try:
        missing = agents.missing_tools()
    except Exception:
        pass

    if tools:
        import pandas as pd

        rows = [
            {"Tool": t.name, "What it does": _first_line(getattr(t, "description", "") or "")}
            for t in tools
        ]
        frame = pd.DataFrame(rows)
        st.dataframe(frame, width="stretch", hide_index=True)
        st.caption(f"{len(tools)} tool(s) live.")
    if missing:
        ui.callout(
            "Analytics engines not present in this build (their tools are "
            f"withheld rather than faked): {', '.join(missing)}."
        )


def _chat(ctx: DataContext, agent_graph, persona: str) -> None:
    ui.section("Ask the team", "Every figure comes from a tool call against the loaded FOCUS frame.")

    if "copilot_messages" not in st.session_state:
        st.session_state["copilot_messages"] = []
    thread_id = st.session_state.get("thread_id")
    if not thread_id:
        thread_id = str(uuid.uuid4())
        st.session_state["thread_id"] = thread_id

    # Suggested-question chips -- prefill the input on click.
    st.caption("Suggested questions:")
    chip_cols = st.columns(len(_SUGGESTIONS))
    for i, q in enumerate(_SUGGESTIONS):
        with chip_cols[i]:
            if st.button(q, key=f"copilot_chip_{i}", width="stretch"):
                st.session_state["copilot_pending"] = q

    # Replay the transcript.
    for msg in st.session_state["copilot_messages"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    typed = st.chat_input("Ask about spend, forecast, savings or allocation...")
    question = typed or st.session_state.pop("copilot_pending", None)

    if not question:
        st.caption(
            "The supervisor routes on the cheap model; specialists reason on the "
            "strong one -- the small-model-first lever, applied to ourselves."
        )
        return

    st.session_state["copilot_messages"].append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        try:
            answer = st.write_stream(
                agent_graph.run(question, ctx.config, ctx, thread_id, persona)
            )
        except Exception as exc:
            answer = f"The team hit an error: `{exc}`"
            st.error(answer)

    st.session_state["copilot_messages"].append({"role": "assistant", "content": answer})
    st.caption(
        "Every figure the Copilot quoted came from a tool call against the loaded "
        "FOCUS frame, not from model memory."
    )


def _first_line(text: str) -> str:
    for line in str(text).splitlines():
        s = line.strip()
        if s:
            return s
    return ""
