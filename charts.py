"""Plotly chart vocabulary.

Rules encoded here so no caller has to remember them:

* one y-axis, always -- never a dual-scale plot
* colour follows the entity, not its rank (see `theme.colour_map`)
* a legend whenever >= 2 series; none for one (the title names it)
* selective direct labels -- endpoint / extreme only, never every point
* thin marks, hairline solid gridlines, area washes at ~10% opacity
* a 2px surface gap between touching fills, a 2px surface ring on markers
* hover on by default; tooltips enhance, they never gate (`ui.table_view` is the twin)
* sequential = one hue light->dark; diverging = two opposite hues + neutral grey

Every function returns a `plotly.graph_objects.Figure`; the caller renders it.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence

import pandas as pd
import plotly.graph_objects as go

import theme
from theme import STATUS

# Mark specs
BAR_MAX_PX = 24
LINE_WIDTH = 2
MARKER_SIZE = 9  # >= 8px
RING_WIDTH = 2
GAP_WIDTH = 2
AREA_OPACITY = 0.10


def _hex_to_rgba(hex_colour: str, alpha: float) -> str:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def base_layout(mode: Optional[str] = None, height: int = 340, showlegend: bool = True) -> dict:
    """Chart chrome. Gridlines and axes are solid hairlines one step off surface."""
    s = theme.surface(mode or theme.DEFAULT_MODE)
    return dict(
        height=height,
        margin=dict(l=8, r=8, t=28, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=theme.FONT_STACK, size=12, color=s.text_secondary),
        showlegend=showlegend,
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="left",
            x=0,
            font=dict(color=s.text_secondary, size=11),
            bgcolor="rgba(0,0,0,0)",
        ),
        xaxis=dict(
            showgrid=False,
            zeroline=False,
            linecolor=s.axis,
            linewidth=1,
            tickfont=dict(color=s.text_muted, size=11),
        ),
        yaxis=dict(
            showgrid=True,
            gridcolor=s.grid,
            gridwidth=1,
            zeroline=False,
            linecolor="rgba(0,0,0,0)",
            tickfont=dict(color=s.text_muted, size=11),
        ),
        hovermode="x unified",
        hoverlabel=dict(
            bgcolor=s.surface_raised,
            bordercolor=s.border,
            font=dict(family=theme.FONT_STACK, color=s.text_primary, size=12),
        ),
    )


def _money_hover(prefix: str = "") -> str:
    return prefix + "$%{y:,.0f}<extra></extra>"


# --------------------------------------------------------------------------
# Trend + forecast fan chart
# --------------------------------------------------------------------------


def forecast_fan(
    history: pd.DataFrame,
    forecast: pd.DataFrame,
    budget: Optional[pd.DataFrame] = None,
    mode: Optional[str] = None,
    height: int = 400,
    value_col: str = "cost",
    date_col: str = "period",
) -> go.Figure:
    """Actuals + point forecast + 80/95% prediction bands, with the budget line.

    The executive reads three things: where we are, where we are heading, and
    where the band crosses budget. The bands are area washes, not saturated
    blocks, and are ordered widest-first so the 80% band sits on top of the 95%.

    `forecast` must carry `lo80/hi80/lo95/hi95` alongside the point estimate.
    """
    s = theme.surface(mode or theme.DEFAULT_MODE)
    accent = s.categorical[0]
    fig = go.Figure()

    # 95% band (outer, faintest)
    if {"lo95", "hi95"}.issubset(forecast.columns):
        fig.add_trace(
            go.Scatter(
                x=list(forecast[date_col]) + list(forecast[date_col][::-1]),
                y=list(forecast["hi95"]) + list(forecast["lo95"][::-1]),
                fill="toself",
                fillcolor=_hex_to_rgba(accent, AREA_OPACITY * 0.6),
                line=dict(width=0),
                hoverinfo="skip",
                name="95% likely range",
                showlegend=True,
            )
        )
    # 80% band (inner)
    if {"lo80", "hi80"}.issubset(forecast.columns):
        fig.add_trace(
            go.Scatter(
                x=list(forecast[date_col]) + list(forecast[date_col][::-1]),
                y=list(forecast["hi80"]) + list(forecast["lo80"][::-1]),
                fill="toself",
                fillcolor=_hex_to_rgba(accent, AREA_OPACITY * 1.4),
                line=dict(width=0),
                hoverinfo="skip",
                name="80% likely range",
                showlegend=True,
            )
        )

    # Actuals
    fig.add_trace(
        go.Scatter(
            x=history[date_col],
            y=history[value_col],
            mode="lines",
            name="Actual",
            line=dict(color=accent, width=LINE_WIDTH, shape="linear"),
            hovertemplate=_money_hover("Actual  "),
        )
    )

    # Point forecast -- dashed, because it IS a projection (dashing a gridline
    # would be noise; dashing a projection is the meaning).
    fig.add_trace(
        go.Scatter(
            x=forecast[date_col],
            y=forecast[value_col],
            mode="lines",
            name="Forecast",
            line=dict(color=accent, width=LINE_WIDTH, dash="dash"),
            hovertemplate=_money_hover("Forecast  "),
        )
    )

    # Budget -- a threshold, drawn in muted ink so it never competes with data
    if budget is not None and len(budget):
        fig.add_trace(
            go.Scatter(
                x=budget[date_col],
                y=budget[value_col],
                mode="lines",
                name="Budget",
                line=dict(color=s.text_muted, width=LINE_WIDTH, dash="dot"),
                hovertemplate=_money_hover("Budget  "),
            )
        )

    # One selective direct label: the terminal forecast value.
    if len(forecast):
        last = forecast.iloc[-1]
        fig.add_trace(
            go.Scatter(
                x=[last[date_col]],
                y=[last[value_col]],
                mode="markers+text",
                marker=dict(
                    size=MARKER_SIZE,
                    color=accent,
                    line=dict(width=RING_WIDTH, color=s.surface),
                ),
                text=[f"  ${last[value_col]:,.0f}"],
                textposition="middle right",
                textfont=dict(color=s.text_primary, size=11),
                showlegend=False,
                hoverinfo="skip",
            )
        )

    lay = base_layout(mode, height=height)
    lay["yaxis"]["tickprefix"] = "$"
    lay["yaxis"]["tickformat"] = ",.0f"
    fig.update_layout(**lay)
    return fig


# --------------------------------------------------------------------------
# Stacked spend over time
# --------------------------------------------------------------------------


def stacked_area(
    df: pd.DataFrame,
    date_col: str,
    series_col: str,
    value_col: str,
    mode: Optional[str] = None,
    height: int = 340,
) -> go.Figure:
    s = theme.surface(mode or theme.DEFAULT_MODE)
    entities = list(df[series_col].unique())
    cmap = theme.colour_map(entities, mode or theme.DEFAULT_MODE)
    fig = go.Figure()
    for e in entities:
        sub = df[df[series_col] == e].sort_values(date_col)
        c = cmap[e]
        fig.add_trace(
            go.Scatter(
                x=sub[date_col],
                y=sub[value_col],
                name=str(e),
                mode="lines",
                stackgroup="one",
                line=dict(width=LINE_WIDTH, color=c),
                fillcolor=_hex_to_rgba(c, 0.35),
                hovertemplate=f"{e}  $%{{y:,.0f}}<extra></extra>",
            )
        )
    lay = base_layout(mode, height=height, showlegend=len(entities) >= 2)
    lay["yaxis"]["tickprefix"] = "$"
    fig.update_layout(**lay)
    return fig


def stacked_bar(
    df: pd.DataFrame,
    x_col: str,
    series_col: str,
    value_col: str,
    mode: Optional[str] = None,
    height: int = 340,
) -> go.Figure:
    """Stacked columns. Segments are separated by a 2px surface gap, not a stroke."""
    s = theme.surface(mode or theme.DEFAULT_MODE)
    entities = list(df[series_col].unique())
    cmap = theme.colour_map(entities, mode or theme.DEFAULT_MODE)
    fig = go.Figure()
    for e in entities:
        sub = df[df[series_col] == e]
        fig.add_trace(
            go.Bar(
                x=sub[x_col],
                y=sub[value_col],
                name=str(e),
                marker=dict(
                    color=cmap[e],
                    line=dict(width=GAP_WIDTH, color=s.surface),  # the gap
                ),
                hovertemplate=f"{e}  $%{{y:,.0f}}<extra></extra>",
            )
        )
    lay = base_layout(mode, height=height, showlegend=len(entities) >= 2)
    lay["barmode"] = "stack"
    lay["bargap"] = 0.35
    lay["yaxis"]["tickprefix"] = "$"
    fig.update_layout(**lay)
    return fig


def ranked_bar(
    labels: Sequence[str],
    values: Sequence[float],
    mode: Optional[str] = None,
    height: int = 360,
    highlight: Optional[str] = None,
    value_prefix: str = "$",
) -> go.Figure:
    """One series -> one colour for every bar.

    A value-ramp across nominal categories would double-encode length as hue and
    burn the only free channel. When `highlight` is given we use emphasis: the
    named bar takes the accent, the rest recede to muted.
    """
    s = theme.surface(mode or theme.DEFAULT_MODE)
    accent = s.categorical[0]
    if highlight:
        colours = [accent if l == highlight else s.grid for l in labels]
    else:
        colours = [accent] * len(labels)

    fig = go.Figure(
        go.Bar(
            x=list(values),
            y=list(labels),
            orientation="h",
            marker=dict(color=colours, line=dict(width=GAP_WIDTH, color=s.surface)),
            text=[f"{value_prefix}{v:,.0f}" for v in values],
            textposition="outside",  # never clipped inside a short bar
            textfont=dict(color=s.text_secondary, size=11),
            hovertemplate="%{y}  " + value_prefix + "%{x:,.0f}<extra></extra>",
            cliponaxis=False,
        )
    )
    lay = base_layout(mode, height=height, showlegend=False)
    lay["yaxis"]["autorange"] = "reversed"
    lay["yaxis"]["showgrid"] = False
    lay["xaxis"]["showgrid"] = True
    lay["xaxis"]["gridcolor"] = s.grid
    lay["bargap"] = 0.45
    fig.update_layout(**lay)
    fig.update_traces(marker_line_width=GAP_WIDTH)
    return fig


# --------------------------------------------------------------------------
# Budget variance -- polarity, so: diverging
# --------------------------------------------------------------------------


def variance_waterfall(
    labels: Sequence[str],
    deltas: Sequence[float],
    mode: Optional[str] = None,
    height: int = 360,
) -> go.Figure:
    """Bridge from budget to forecast. Overrun warm, underrun cool, zero neutral."""
    s = theme.surface(mode or theme.DEFAULT_MODE)
    over = s.diverging[-2]
    under = s.diverging[1]
    fig = go.Figure(
        go.Waterfall(
            orientation="v",
            measure=["relative"] * (len(labels) - 1) + ["total"],
            x=list(labels),
            y=list(deltas),
            connector=dict(line=dict(color=s.grid, width=1)),
            increasing=dict(marker=dict(color=over, line=dict(width=GAP_WIDTH, color=s.surface))),
            decreasing=dict(marker=dict(color=under, line=dict(width=GAP_WIDTH, color=s.surface))),
            totals=dict(marker=dict(color=s.text_muted, line=dict(width=GAP_WIDTH, color=s.surface))),
            hovertemplate="%{x}  $%{y:,.0f}<extra></extra>",
        )
    )
    lay = base_layout(mode, height=height, showlegend=False)
    lay["yaxis"]["tickprefix"] = "$"
    lay["hovermode"] = "closest"
    fig.update_layout(**lay)
    return fig


def variance_bars(
    labels: Sequence[str],
    variance: Sequence[float],
    mode: Optional[str] = None,
    height: int = 340,
) -> go.Figure:
    """Signed variance per entity, diverging around a neutral zero."""
    s = theme.surface(mode or theme.DEFAULT_MODE)
    over, under = s.diverging[-2], s.diverging[1]
    colours = [over if v > 0 else under for v in variance]
    fig = go.Figure(
        go.Bar(
            x=list(labels),
            y=list(variance),
            marker=dict(color=colours, line=dict(width=GAP_WIDTH, color=s.surface)),
            hovertemplate="%{x}  $%{y:,.0f}<extra></extra>",
        )
    )
    lay = base_layout(mode, height=height, showlegend=False)
    lay["yaxis"]["tickprefix"] = "$"
    lay["yaxis"]["zeroline"] = True
    lay["yaxis"]["zerolinecolor"] = s.axis
    lay["yaxis"]["zerolinewidth"] = 1
    lay["bargap"] = 0.4
    lay["hovermode"] = "closest"
    fig.update_layout(**lay)
    return fig


# --------------------------------------------------------------------------
# Allocation treemap -- identity, coloured by cloud (a pinned entity)
# --------------------------------------------------------------------------


def allocation_treemap(
    df: pd.DataFrame,
    path_cols: Sequence[str],
    value_col: str,
    colour_by: str,
    mode: Optional[str] = None,
    height: int = 440,
) -> go.Figure:
    s = theme.surface(mode or theme.DEFAULT_MODE)
    labels: List[str] = []
    parents: List[str] = []
    values: List[float] = []
    colours: List[str] = []

    root = "All spend"
    labels.append(root)
    parents.append("")
    values.append(float(df[value_col].sum()))
    colours.append(s.surface_raised)

    cmap = theme.colour_map(sorted(df[colour_by].unique()), mode or theme.DEFAULT_MODE)

    level1 = df.groupby(path_cols[0], as_index=False, observed=True)[value_col].sum()
    for _, r in level1.iterrows():
        labels.append(str(r[path_cols[0]]))
        parents.append(root)
        values.append(float(r[value_col]))
        colours.append(cmap.get(str(r[path_cols[0]]), s.categorical[0]))

    if len(path_cols) > 1:
        level2 = df.groupby([path_cols[0], path_cols[1]], as_index=False, observed=True)[value_col].sum()
        for _, r in level2.iterrows():
            node = f"{r[path_cols[1]]}"
            labels.append(node)
            parents.append(str(r[path_cols[0]]))
            values.append(float(r[value_col]))
            colours.append(_hex_to_rgba(cmap.get(str(r[path_cols[0]]), s.categorical[0]), 0.55))

    fig = go.Figure(
        go.Treemap(
            labels=labels,
            parents=parents,
            values=values,
            branchvalues="total",
            marker=dict(colors=colours, line=dict(width=GAP_WIDTH, color=s.surface)),
            textinfo="label+value+percent parent",
            texttemplate="<b>%{label}</b><br>$%{value:,.0f}",
            hovertemplate="%{label}<br>$%{value:,.0f}<br>%{percentParent} of parent<extra></extra>",
            tiling=dict(pad=2),
            pathbar=dict(visible=True),
        )
    )
    lay = base_layout(mode, height=height, showlegend=False)
    lay.pop("xaxis")
    lay.pop("yaxis")
    lay["hovermode"] = "closest"
    fig.update_layout(**lay)
    fig.update_traces(insidetextfont=dict(color="#FFFFFF", size=12))
    return fig


# --------------------------------------------------------------------------
# Heatmap -- continuous magnitude, so: one hue, light -> dark
# --------------------------------------------------------------------------


def heatmap(
    matrix: pd.DataFrame,
    mode: Optional[str] = None,
    height: int = 380,
    value_prefix: str = "$",
    colourbar_title: str = "",
) -> go.Figure:
    s = theme.surface(mode or theme.DEFAULT_MODE)
    ramp = theme.SEQUENTIAL_BLUE
    if (mode or theme.DEFAULT_MODE) == "dark":
        ramp = list(reversed(theme.SEQUENTIAL_BLUE))  # dark surface: light = high
    stops = [[i / (len(ramp) - 1), c] for i, c in enumerate(ramp)]

    fig = go.Figure(
        go.Heatmap(
            z=matrix.values,
            x=[str(c) for c in matrix.columns],
            y=[str(i) for i in matrix.index],
            colorscale=stops,
            xgap=GAP_WIDTH,
            ygap=GAP_WIDTH,
            hovertemplate="%{y} · %{x}<br>" + value_prefix + "%{z:,.0f}<extra></extra>",
            colorbar=dict(
                title=dict(text=colourbar_title, font=dict(color=s.text_muted, size=11)),
                tickfont=dict(color=s.text_muted, size=10),
                outlinewidth=0,
                thickness=10,
            ),
        )
    )
    lay = base_layout(mode, height=height, showlegend=False)
    lay["yaxis"]["showgrid"] = False
    lay["hovermode"] = "closest"
    fig.update_layout(**lay)
    return fig


# --------------------------------------------------------------------------
# Small pieces
# --------------------------------------------------------------------------


def sparkline(values: Sequence[float], mode: Optional[str] = None, height: int = 46) -> go.Figure:
    s = theme.surface(mode or theme.DEFAULT_MODE)
    fig = go.Figure(
        go.Scatter(
            y=list(values),
            mode="lines",
            line=dict(color=s.categorical[0], width=LINE_WIDTH),
            hoverinfo="skip",
        )
    )
    fig.update_layout(
        height=height,
        margin=dict(l=0, r=0, t=0, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
    )
    return fig


def meter(
    value_pct: float,
    mode: Optional[str] = None,
    height: int = 120,
    title: str = "",
    thresholds: Sequence[float] = (60, 85),
    higher_is_better: bool = True,
) -> go.Figure:
    """Fill carries severity; the unfilled track is a lighter step of the same ramp."""
    s = theme.surface(mode or theme.DEFAULT_MODE)
    lo, hi = thresholds
    if higher_is_better:
        colour = STATUS["critical"] if value_pct < lo else STATUS["warning"] if value_pct < hi else STATUS["good"]
    else:
        colour = STATUS["good"] if value_pct < lo else STATUS["warning"] if value_pct < hi else STATUS["critical"]

    fig = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=value_pct,
            number=dict(suffix="%", font=dict(size=22, color=s.text_primary, family=theme.FONT_STACK)),
            title=dict(text=title, font=dict(size=11, color=s.text_muted, family=theme.FONT_STACK)),
            gauge=dict(
                axis=dict(range=[0, 100], tickcolor=s.grid, tickfont=dict(size=9, color=s.text_muted)),
                bar=dict(color=colour, thickness=0.7),
                bgcolor=s.grid,
                borderwidth=0,
            ),
        )
    )
    fig.update_layout(
        height=height,
        margin=dict(l=12, r=12, t=28, b=4),
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(family=theme.FONT_STACK),
    )
    return fig


def anomaly_scatter(
    df: pd.DataFrame,
    date_col: str,
    value_col: str,
    flag_col: str,
    mode: Optional[str] = None,
    height: int = 320,
) -> go.Figure:
    """Daily spend with flagged points. Status colour ships with a distinct marker
    symbol, so the anomaly never reads by colour alone."""
    s = theme.surface(mode or theme.DEFAULT_MODE)
    normal = df[~df[flag_col]]
    flagged = df[df[flag_col]]

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=df[date_col],
            y=df[value_col],
            mode="lines",
            name="Daily spend",
            line=dict(color=s.categorical[0], width=LINE_WIDTH),
            hovertemplate=_money_hover(),
        )
    )
    if len(flagged):
        fig.add_trace(
            go.Scatter(
                x=flagged[date_col],
                y=flagged[value_col],
                mode="markers",
                name="Anomaly",
                marker=dict(
                    size=MARKER_SIZE + 3,
                    symbol="diamond",  # secondary channel, not colour alone
                    color=STATUS["critical"],
                    line=dict(width=RING_WIDTH, color=s.surface),
                ),
                hovertemplate="Anomaly  $%{y:,.0f}<extra></extra>",
            )
        )
    lay = base_layout(mode, height=height, showlegend=True)
    lay["yaxis"]["tickprefix"] = "$"
    fig.update_layout(**lay)
    return fig
