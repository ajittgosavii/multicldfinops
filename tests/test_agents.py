"""Offline tests for the agent layer.

No network, no OPENAI_API_KEY. Everything here exercises the parts that must
work before an API key is ever involved: the package imports, the tools query
the real demo frame, the query_spend whitelist actually rejects, and the
key-absent path degrades to a message instead of raising. The live graph itself
needs OpenAI and is not exercised here by design.
"""

from __future__ import annotations

import os

import pytest

# Guarantee the key-absent path regardless of the developer's environment.
os.environ.pop("OPENAI_API_KEY", None)

from connectors.demo import build_demo_dataset
from finops_core import AppConfig, DataContext, Mode


@pytest.fixture(scope="module")
def ctx() -> DataContext:
    df, budgets, drivers = build_demo_dataset(months=12)
    return DataContext(focus_df=df, budgets=budgets, drivers=drivers, mode=Mode.DEMO, config=AppConfig())


@pytest.fixture(scope="module")
def tool_map(ctx):
    import agents

    tools = agents.make_tools(ctx)
    return {t.name: t for t in tools}


def test_import_without_key():
    # Import must not need a key or a Streamlit runtime.
    import agents

    assert hasattr(agents, "run")
    assert hasattr(agents, "make_tools")
    cfg = AppConfig()  # no key
    assert agents.openai_available(cfg) is False


def test_make_tools_count_and_docstrings(ctx):
    import agents

    tools = agents.make_tools(ctx)
    assert len(tools) >= 10, f"expected >=10 tools, got {len(tools)}: {[t.name for t in tools]}"
    for t in tools:
        assert (t.description or "").strip(), f"tool {t.name} has no docstring/description"


def test_missing_tools_is_list():
    from agents.tools import missing_tools

    assert isinstance(missing_tools(), list)


def test_get_spend_summary(tool_map):
    out = tool_map["get_spend_summary"].invoke({"group_by": "ProviderName", "months": 12})
    assert isinstance(out, dict)
    assert out["total_spend"] and out["total_spend"] > 0
    assert out["breakdown"], "expected a per-provider breakdown"
    # Demo estate spans AWS/Azure/GCP.
    assert any(k in out["breakdown"] for k in ("AWS", "Azure", "GCP"))


def test_get_executive_kpis(tool_map):
    out = tool_map["get_executive_kpis"].invoke({})
    assert isinstance(out, dict)
    assert out["total_spend"] and out["total_spend"] > 0
    assert "chargeback_readiness" in out
    assert "esr_pct" in out


def test_find_optimization_opportunities_or_absent(tool_map):
    from agents.tools import missing_tools

    if "optimize" in missing_tools():
        # Engine not shipped yet: the tool must not be registered at all.
        assert "find_optimization_opportunities" not in tool_map
        pytest.skip("optimize engine not present; tool correctly unregistered")
    out = tool_map["find_optimization_opportunities"].invoke({"min_annual_savings": 0.0, "category": ""})
    assert isinstance(out, dict)
    assert "opportunities" in out
    assert out["total_annual_savings"] is None or out["total_annual_savings"] >= 0


def test_query_spend_rejects_unknown_column(tool_map):
    out = tool_map["query_spend"].invoke(
        {"filters": [{"column": "DROP TABLE", "op": "==", "value": "x"}], "group_by": [], "metric": "EffectiveCost"}
    )
    assert isinstance(out, dict)
    assert "error" in out
    assert "not permitted" in out["error"]


def test_query_spend_rejects_bad_operator(tool_map):
    out = tool_map["query_spend"].invoke(
        {"filters": [{"column": "ProviderName", "op": "__import__", "value": "os"}], "group_by": [], "metric": "EffectiveCost"}
    )
    assert isinstance(out, dict)
    assert "error" in out
    assert "operator" in out["error"]


def test_query_spend_happy_path(tool_map):
    out = tool_map["query_spend"].invoke(
        {"filters": [{"column": "ChargeCategory", "op": "==", "value": "Usage"}],
         "group_by": ["ProviderName"], "metric": "EffectiveCost"}
    )
    assert isinstance(out, dict)
    assert out["total"] and out["total"] > 0
    assert out["groups"], "expected grouped results"


def test_openai_available_false_and_run_message(ctx):
    import agents

    cfg = AppConfig()  # no key
    assert agents.openai_available(cfg) is False

    chunks = list(agents.run("What is our total spend?", cfg, ctx, thread_id="t-test", persona="Leadership"))
    joined = "".join(chunks)
    assert "OPENAI_API_KEY" in joined
    # It must explain, not raise.
    assert len(joined) > 50


def test_graph_diagram_nonempty():
    import agents

    d = agents.graph_diagram()
    assert isinstance(d, str)
    assert "graph TD" in d
    assert "Supervisor" in d


def test_agent_cards_shape():
    import agents

    assert isinstance(agents.AGENT_CARDS, list) and len(agents.AGENT_CARDS) >= 5
    for card in agents.AGENT_CARDS:
        assert {"name", "domain", "capabilities", "tools", "model"} <= set(card)


# ==========================================================================
# Provider failures are a normal state, not a crash
# ==========================================================================


def test_model_access_error_names_the_secret_to_change():
    from agents.graph import explain_failure
    from finops_core import AppConfig

    cfg = AppConfig(openai_model="gpt-5")
    msg = explain_failure(Exception("The model `gpt-5` does not exist or you do not have access"), cfg)
    assert "gpt-5" in msg
    assert "OPENAI_MODEL" in msg
    assert "reboot" in msg.lower()


def test_quota_and_auth_errors_are_distinguished():
    from agents.graph import explain_failure
    from finops_core import AppConfig

    cfg = AppConfig()
    quota = explain_failure(Exception("Error code: 429 - insufficient_quota"), cfg)
    assert "quota" in quota.lower()
    assert "dashboard still works" in quota.lower()

    auth = explain_failure(Exception("Error code: 401 - Invalid API key provided"), cfg)
    assert "OPENAI_API_KEY" in auth


def test_run_yields_the_explanation_instead_of_raising(monkeypatch):
    """A provider outage must degrade to a readable message in the chat, never a
    traceback in the user's face."""
    import agents.graph as G
    from connectors.demo import build_demo_dataset
    from finops_core import AppConfig, DataContext, Mode

    df, b, d = build_demo_dataset(months=6)
    ctx = DataContext(focus_df=df, budgets=b, drivers=d, mode=Mode.DEMO, config=AppConfig())
    cfg = AppConfig(openai_api_key="sk-test", openai_model="gpt-5")

    def boom(*a, **k):
        raise RuntimeError("Error code: 404 - The model `gpt-5` does not exist or you do not have access")

    monkeypatch.setattr(G, "get_graph", boom)
    out = "".join(G.run("why did spend rise?", cfg, ctx, thread_id="t1"))
    assert "OPENAI_MODEL" in out
    assert "gpt-5" in out
