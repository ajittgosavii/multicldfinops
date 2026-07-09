"""Architecture diagrams -- High Level, End User View, Low Level.

Authored once, exported twice: an `.svg` (the deliverable -- scalable, and with
`svg.fonttype='none'` its text stays real text, so it can be searched, restyled
or edited) and a `.png` at 220 dpi, because `python-pptx` cannot embed SVG.

    python tools/diagrams.py            # -> docs/diagrams/*.svg + *.png

The boxes carry the module names the code actually uses, so a rename makes the
diagram wrong. `tests/test_diagrams.py` asserts every module named on the
low-level diagram still imports.
"""

from __future__ import annotations

import os
import sys
from typing import Dict, List, Tuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.patches as mpatches  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "diagrams")

# Light theme, matching tools/build_deck.py
INK = "#0B142A"
BODY = "#445066"
MUTED = "#7A8599"
RULE = "#E2E7EF"
PAPER = "#FFFFFF"
WASH = "#F5F8FC"

AZURE = "#1E6FD9"
TEAL = "#119B8A"
VIOLET = "#5B4BC4"
AMBER = "#C98500"
CRIMSON = "#C23333"
GREEN = "#0C7A3E"

matplotlib.rcParams["svg.fonttype"] = "none"  # keep text as text in the SVG
matplotlib.rcParams["font.family"] = "DejaVu Sans"


# ==========================================================================
# Primitives
# ==========================================================================


def canvas(w: float, h: float):
    """A 100-wide canvas whose y axis grows DOWNWARD.

    Architecture diagrams are read top to bottom and matplotlib's y grows up.
    Inverting the axis once, here, means every coordinate below is "distance
    from the top" -- which is how the layout is actually reasoned about, and is
    why the first cut of this file drew its source layer at the bottom.
    """
    fig, ax = plt.subplots(figsize=(w, h))
    ax.set_xlim(0, 100)
    ax.set_ylim(100 * h / w, 0)  # inverted
    ax.axis("off")
    fig.patch.set_facecolor(PAPER)
    return fig, ax


def height(ax) -> float:
    return ax.get_ylim()[0]


def band(ax, x, y, w, h, label: str, colour: str) -> None:
    ax.add_patch(
        FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=1.2",
                       linewidth=1, edgecolor=RULE, facecolor=WASH, zorder=1)
    )
    ax.add_patch(mpatches.Rectangle((x, y), 0.55, h, facecolor=colour, edgecolor="none", zorder=2))
    ax.text(x + 1.7, y + h / 2, label, fontsize=7.0, color=colour, weight="bold",
            rotation=90, ha="center", va="center", zorder=3)


def node(ax, x, y, w, h, title_text: str, sub: str = "", colour: str = AZURE,
         fill: str = PAPER, fs: float = 8.0, sub_fs: float = 6.2) -> Tuple[float, float]:
    """`y` is the TOP edge; the box extends downward. Returns its centre."""
    ax.add_patch(
        FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.9",
                       linewidth=1.2, edgecolor=colour, facecolor=fill, zorder=4)
    )
    if sub:
        ax.text(x + w / 2, y + h * 0.34, title_text, fontsize=fs, color=INK, weight="bold",
                ha="center", va="center", zorder=5)
        ax.text(x + w / 2, y + h * 0.70, sub, fontsize=sub_fs, color=MUTED,
                ha="center", va="center", zorder=5, linespacing=1.4)
    else:
        ax.text(x + w / 2, y + h / 2, title_text, fontsize=fs, color=INK, weight="bold",
                ha="center", va="center", zorder=5)
    return x + w / 2, y + h / 2


def arrow(ax, p1, p2, colour: str = MUTED, lw: float = 1.1, rad: float = 0.0,
          dashed: bool = False) -> None:
    ax.add_patch(
        FancyArrowPatch(p1, p2, arrowstyle="-|>", mutation_scale=9, linewidth=lw,
                        color=colour, zorder=3, linestyle="--" if dashed else "-",
                        connectionstyle=f"arc3,rad={rad}", shrinkA=1, shrinkB=3)
    )


def title(ax, eyebrow: str, text: str, sub: str = "") -> None:
    ax.text(2.0, 2.4, eyebrow.upper(), fontsize=6.5, color=MUTED, weight="bold", va="center")
    ax.text(2.0, 5.2, text, fontsize=13, color=INK, weight="bold", va="center")
    if sub:
        ax.text(2.0, 8.2, sub, fontsize=7.4, color=BODY, va="center")


def caption(ax, text: str) -> None:
    ax.text(2.0, height(ax) - 1.6, text, fontsize=6.0, color=MUTED, va="center")


def save(fig, name: str) -> List[str]:
    os.makedirs(OUT_DIR, exist_ok=True)
    paths = []
    for ext, kw in (("svg", {}), ("png", {"dpi": 220})):
        p = os.path.join(OUT_DIR, f"{name}.{ext}")
        fig.savefig(p, bbox_inches="tight", facecolor=PAPER, **kw)
        paths.append(p)
    plt.close(fig)
    return paths


# ==========================================================================
# 1. High level design
# ==========================================================================


def hld() -> List[str]:
    # 16x11 rather than 16x10: the side rails and the caption both need to sit
    # inside the axis, and at 10 the rails ran off the bottom edge.
    fig, ax = canvas(16, 11)
    title(ax, "High level design",
          "Every source normalises to one FOCUS 1.2 frame",
          "Nothing downstream of a connector has ever seen a vendor-specific field")

    Y_SRC, Y_ING, Y_CAN, Y_ENG, Y_EXP, Y_RAIL = 12.5, 25.0, 34.0, 43.0, 53.5, 61.8

    band(ax, 2, Y_SRC - 1.3, 96, 9.6, "SOURCES", AZURE)
    band(ax, 2, Y_ING - 1.3, 96, 7.4, "INGEST", TEAL)
    band(ax, 2, Y_CAN - 1.3, 96, 7.2, "CANONICAL", VIOLET)
    band(ax, 2, Y_ENG - 1.3, 96, 8.6, "ANALYTICS", AMBER)
    band(ax, 2, Y_EXP - 1.3, 96, 7.2, "EXPERIENCE", CRIMSON)

    srcs = [
        ("AWS", "Data Exports (FOCUS 1.2)\nCost Explorer · Cost Opt Hub"),
        ("Azure", "Cost Management\nFocusCost export · Advisor"),
        ("GCP", "BigQuery billing export\nRecommender · Budgets"),
        ("Procured tool", "Cloudability · CloudHealth\nFlexera · Finout · Vantage"),
        ("FOCUS file", "CSV / Parquet\nlocal · s3 · az · gs"),
    ]
    src_cx = []
    x = 6.5
    for name, sub in srcs:
        node(ax, x, Y_SRC, 16.6, 7.0, name, sub, AZURE, PAPER, 8.0, 5.2)
        src_cx.append(x + 8.3)
        x += 18.1

    ing_h = 4.8
    node(ax, 26, Y_ING, 48, ing_h, "connectors/  ·  Connector.fetch_costs()",
         "17 connectors · lazy SDK imports · a failing binding never fails the page",
         TEAL, PAPER, 8.4, 5.6)
    for i, sx in enumerate(src_cx):
        arrow(ax, (sx, Y_SRC + 7.0), (32 + i * 9.0, Y_ING), AZURE, lw=0.9)

    can_h = 4.6
    node(ax, 31, Y_CAN, 38, can_h, "FOCUS 1.2 DataFrame",
         "focus.normalize · validate · explode_tags · serialize_tags",
         VIOLET, "#F3F1FC", 9.0, 5.8)
    arrow(ax, (50.0, Y_ING + ing_h), (50.0, Y_CAN), TEAL, lw=1.8)

    eng = [("kpi.py", "ESR, coverage,\nwaste, variance"),
           ("forecast.py", "Holt-Winters, SARIMA,\ncommitment cliffs"),
           ("budget.py", "variance, bridge,\nyear-end"),
           ("anomaly.py", "STL + MAD\non the residual"),
           ("allocation.py", "showback, chargeback,\nshared-cost split"),
           ("optimize.py", "53 levers,\nrule detectors")]
    x = 4.5
    eng_cx = []
    for name, sub in eng:
        node(ax, x, Y_ENG, 14.6, 6.0, name, sub, AMBER, PAPER, 7.6, 5.0)
        eng_cx.append(x + 7.3)
        x += 15.4
    for cx in eng_cx:
        arrow(ax, (50.0, Y_CAN + can_h), (cx, Y_ENG), VIOLET, rad=0.04, lw=0.8)

    exp = [("12 dashboards", "tabs/ · render(ctx)"),
           ("AI Copilot", "agents/ · LangGraph supervisor"),
           ("Exports", "CSV · a table-view twin per chart")]
    x = 11.0
    for name, sub in exp:
        node(ax, x, Y_EXP, 25.0, 4.6, name, sub, CRIMSON, PAPER, 8.4, 5.6)
        # The whole analytics layer feeds each surface. Pairing one engine to one
        # surface would assert a coupling that does not exist.
        arrow(ax, (x + 12.5, Y_ENG + 6.0), (x + 12.5, Y_EXP), AMBER, lw=0.9)
        x += 26.5

    rails = [("Two modes", "Demo (synthetic) · Live (connectors)"),
             ("Secrets", "Streamlit secrets / env · never logged"),
             ("Caching", "st.cache_data · CE bills ~$0.01/request"),
             ("Durable state", "SQLite / Postgres · policies, scenarios")]
    x = 2.5
    for name, sub in rails:
        node(ax, x, Y_RAIL, 23.0, 4.2, name, sub, MUTED, WASH, 7.4, 5.2)
        x += 24.0

    caption(ax, "Adopting a new FinOps tool is one Connector subclass and one registry line. Dropping one is deleting it.")
    return save(fig, "hld")


# ==========================================================================
# 2. End user view
# ==========================================================================


def end_user_view() -> List[str]:
    fig, ax = canvas(16, 10)
    title(ax, "End user view",
          "Who asks what, and where the answer lives",
          "One filter row scopes every panel on a page, so no two charts ever disagree")

    steps = [("Sign in", "access key"), ("Choose mode", "Demo or Live"),
             ("Scope", "cloud · app · BU · env · period"),
             ("Read", "12 dashboards"), ("Ask", "AI Copilot")]
    x, y = 3.5, 12.5
    for i, (name, sub) in enumerate(steps):
        node(ax, x, y, 16.8, 5.2, name, sub, AZURE, PAPER, 8.2, 5.4)
        if i:
            arrow(ax, (x - 1.5, y + 2.6), (x, y + 2.6), AZURE, lw=1.2)
        x += 18.4

    personas = [
        ("Leadership", "Spend, forecast vs budget,\nESR, unit cost",
         ["Executive", "Forecast & Budget", "Unit Economics"], CRIMSON),
        ("Finance", "Variance, chargeback,\ninvoice reconciliation",
         ["Showback & Chargeback", "Forecast & Budget", "Baseline"], VIOLET),
        ("FinOps practitioner", "Coverage, anomalies,\nsavings realised",
         ["Optimize", "Anomalies", "Governance"], TEAL),
        ("Engineering", "Cost per service,\nrightsizing signals",
         ["Applications", "Optimize", "Anomalies"], AMBER),
        ("Procurement", "Commitment coverage,\nutilisation, renewals",
         ["Executive", "Optimize"], GREEN),
    ]
    y_p, y_t = 24.5, 33.5
    x = 3.0
    for name, wants, tabs, colour in personas:
        node(ax, x, y_p, 17.8, 6.8, name, wants, colour, PAPER, 8.0, 5.2)
        arrow(ax, (x + 8.9, y_p + 6.8), (x + 8.9, y_t), colour, lw=0.9)
        ty = y_t
        for t in tabs:
            ax.add_patch(
                FancyBboxPatch((x + 1.3, ty), 15.2, 2.8, boxstyle="round,pad=0,rounding_size=0.7",
                               linewidth=0.9, edgecolor=RULE, facecolor=WASH, zorder=4)
            )
            ax.text(x + 8.9, ty + 1.4, t, fontsize=6.3, color=BODY, ha="center", va="center", zorder=5)
            ty += 3.4
        x += 18.8

    notes = [
        "Every chart has a table-view twin with a CSV download, so no value is reachable only through a tooltip.",
        "Status is carried by an icon and a label as well as colour. Colour follows the entity, never its rank.",
        "No dual-axis charts: two measures of different scale are indexed to 100 at t0 on one axis.",
        "The AI Copilot answers in outcome terms and cites the tool each figure came from.",
    ]
    y = 47.5
    for n in notes:
        ax.text(3.0, y, "—  " + n, fontsize=6.8, color=BODY, va="center")
        y += 2.9

    caption(ax, "Personas and their expectations are the FinOps Foundation's, rendered from the same constants the code uses.")
    return save(fig, "end_user_view")


# ==========================================================================
# 3. Low level design
# ==========================================================================


LLD_MODULES = [
    "app", "ui", "data", "finops_core", "focus", "kpi",
    "forecast", "budget", "anomaly", "allocation", "optimize", "store",
]


def lld() -> List[str]:
    fig, ax = canvas(16, 10)
    title(ax, "Low level design",
          "One request through the system",
          "Module boundaries, the secret boundary, and where results are cached")

    col = [3.0, 26.0, 51.0, 76.0]

    node(ax, col[0], 13.0, 19, 4.6, "app.py", "page config · auth gate · filter row", AZURE, PAPER, 8.2, 5.2)
    node(ax, col[0], 20.0, 19, 4.6, "ui.require_login()", "hmac.compare_digest", AZURE, PAPER, 7.8, 5.2)
    node(ax, col[0], 27.0, 19, 4.6, "data.load_context(cfg)", "the ONLY Demo/Live fork", AZURE, "#EAF2FD", 7.6, 5.2)
    arrow(ax, (12.5, 17.6), (12.5, 20.0), AZURE)
    arrow(ax, (12.5, 24.6), (12.5, 27.0), AZURE)

    node(ax, col[1], 13.0, 21, 4.6, "cfg.bindings()", "one per payer / tenant / billing acct", TEAL, PAPER, 7.8, 5.0)
    node(ax, col[1], 20.0, 21, 4.6, "connectors.get_connector()", "lazy import · secrets never logged", TEAL, PAPER, 7.4, 5.0)
    node(ax, col[1], 27.0, 21, 4.6, "Connector.fetch_costs()", "@st.cache_data(ttl=3600)", TEAL, "#E9F7F5", 7.6, 5.0)
    arrow(ax, (col[0] + 19, 29.3), (col[1], 29.3), AZURE)
    arrow(ax, (36.5, 17.6), (36.5, 20.0), TEAL)
    arrow(ax, (36.5, 24.6), (36.5, 27.0), TEAL)

    ax.add_patch(mpatches.Rectangle((col[1] - 1.5, 11.4), 24.0, 21.8, linewidth=1.1,
                                    edgecolor=CRIMSON, facecolor="none", linestyle="--", zorder=2))
    ax.text(col[1] + 10.5, 34.8, "secret boundary — credentials enter here, and are never echoed",
            fontsize=5.6, color=CRIMSON, ha="center")

    node(ax, col[2], 20.0, 21, 5.8, "focus.py", "normalize → validate →\nexplode_tags → serialize_tags",
         VIOLET, "#F3F1FC", 8.2, 5.2)
    node(ax, col[2], 28.5, 21, 4.6, "DataContext", "focus_df · budgets · drivers", VIOLET, PAPER, 7.8, 5.2)
    arrow(ax, (col[1] + 21, 29.3), (col[2] + 10.5, 25.8), TEAL, rad=0.10)
    arrow(ax, (61.5, 25.8), (61.5, 28.5), VIOLET)

    y = 13.0
    for name, sub in [("kpi.executive_kpis()", "amortised EffectiveCost"),
                      ("forecast.forecast_spend()", "auto-selected by WAPE backtest"),
                      ("optimize.detect_all()", "12 detectors, spec columns only"),
                      ("allocation.allocate()", "shared-cost policy")]:
        node(ax, col[3], y, 21, 4.2, name, sub, AMBER, PAPER, 7.2, 4.9)
        arrow(ax, (col[2] + 21, 30.0), (col[3], y + 2.1), VIOLET, rad=0.16, lw=0.7)
        y += 5.0

    node(ax, col[3], 34.5, 21, 4.4, "tabs/*.render(ctx)", "render only, never compute", CRIMSON, PAPER, 7.4, 5.0)
    arrow(ax, (col[2] + 21, 31.5), (col[3], 36.7), VIOLET, rad=-0.12, lw=0.7)

    node(ax, col[2], 42.0, 21, 5.6, "agents/graph.py", "supervisor (gpt-5-mini)\n4 specialists (gpt-5)",
         GREEN, "#EDF7F1", 7.8, 5.2)
    node(ax, col[3], 42.5, 21, 4.6, "agents/tools.py", "12 tools · column whitelist, no eval", GREEN, PAPER, 7.2, 4.9)
    arrow(ax, (61.5, 33.1), (61.5, 42.0), VIOLET, dashed=True)
    arrow(ax, (col[2] + 21, 44.8), (col[3], 44.8), GREEN)
    arrow(ax, (86.5, 42.5), (86.5, 38.9), GREEN, dashed=True)

    node(ax, col[0], 42.0, 19, 5.0, "store.py", "SQLite / Postgres\npolicies · scenarios", MUTED, WASH, 7.6, 5.0)

    for i, t in enumerate([
        "Shaded boxes are cached. Demo generation is cached for the session; live fetches for an hour.",
        "Tabs render and never compute. Every executive number is defined exactly once, in kpi.py.",
        "A binding that fails contributes zero rows and a reason, and the page still renders.",
    ]):
        ax.text(3.0, 53.0 + i * 2.7, "—  " + t, fontsize=6.6, color=BODY, va="center")

    caption(ax, "Agent tools read the same DataContext the dashboards read, so the Copilot cannot quote a number the UI disagrees with.")
    return save(fig, "lld")


# ==========================================================================


def build_all() -> Dict[str, List[str]]:
    return {"hld": hld(), "end_user_view": end_user_view(), "lld": lld()}


if __name__ == "__main__":
    for name, paths in build_all().items():
        print(f"{name}: " + ", ".join(os.path.relpath(p) for p in paths))
