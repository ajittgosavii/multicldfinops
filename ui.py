"""Streamlit chrome: theme injection, auth gate, and the component vocabulary.

Every visual primitive the app uses lives here so the tabs stay declarative.
Colours come from `theme`; this module never invents a hex.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Iterable, Optional, Sequence

import streamlit as st

import theme
from theme import STATUS, STATUS_ICON

APP_NAME = "Multi-Cloud FinOps Command Center"
APP_TAGLINE = "AWS · Azure · GCP — allocation, forecast, and optimization in one plane"


# --------------------------------------------------------------------------
# Page config + CSS
# --------------------------------------------------------------------------


def page_config() -> None:
    st.set_page_config(
        page_title=APP_NAME,
        page_icon="◇",
        layout="wide",
        initial_sidebar_state="expanded",
    )


def mode() -> str:
    return st.session_state.get("colour_mode", theme.DEFAULT_MODE)


def inject_css() -> None:
    s = theme.surface(mode())
    st.markdown(
        f"""
        <style>
        :root {{
            --page: {s.page};
            --surface: {s.surface};
            --surface-raised: {s.surface_raised};
            --text-primary: {s.text_primary};
            --text-secondary: {s.text_secondary};
            --text-muted: {s.text_muted};
            --grid: {s.grid};
            --axis: {s.axis};
            --border: {s.border};
            --accent: {s.categorical[0]};
            --good: {STATUS['good']};
            --warning: {STATUS['warning']};
            --serious: {STATUS['serious']};
            --critical: {STATUS['critical']};
        }}

        html, body, [class*="css"] {{ font-family: {theme.FONT_STACK}; }}
        .stApp {{ background: var(--page); color: var(--text-primary); }}

        /* Tighten Streamlit's default vertical rhythm -- an exec dashboard
           should not require scrolling to see the KPI row. */
        .block-container {{ padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1600px; }}
        header[data-testid="stHeader"] {{ background: transparent; }}
        #MainMenu, footer {{ visibility: hidden; }}

        section[data-testid="stSidebar"] {{
            background: var(--surface);
            border-right: 1px solid var(--border);
        }}
        section[data-testid="stSidebar"] .block-container {{ padding-top: 1.2rem; }}

        /* ---- Masthead ---- */
        .mf-masthead {{
            display: flex; align-items: center; justify-content: space-between;
            gap: 1.5rem; padding: 1.1rem 1.4rem; margin-bottom: 1.2rem;
            background: linear-gradient(135deg, var(--surface) 0%, var(--surface-raised) 100%);
            border: 1px solid var(--border); border-radius: 14px;
        }}
        .mf-masthead h1 {{
            font-size: 1.5rem; font-weight: 650; letter-spacing: -0.02em;
            margin: 0; color: var(--text-primary);
        }}
        .mf-masthead p {{ margin: .25rem 0 0; color: var(--text-secondary); font-size: .86rem; }}
        .mf-masthead .mf-mark {{
            font-size: 1.6rem; color: var(--accent); margin-right: .6rem;
        }}

        /* ---- Stat tiles ----
           Values use proportional figures (tabular-nums is reserved for
           vertically aligned columns: table rows and axis ticks). */
        .mf-tile {{
            background: var(--surface); border: 1px solid var(--border);
            border-radius: 12px; padding: .95rem 1.05rem; height: 100%;
            display: flex; flex-direction: column; gap: .35rem;
        }}
        .mf-tile .mf-label {{
            font-size: .70rem; font-weight: 600; letter-spacing: .07em;
            text-transform: uppercase; color: var(--text-muted);
        }}
        .mf-tile .mf-value {{
            font-size: 1.85rem; font-weight: 620; line-height: 1.05;
            letter-spacing: -0.025em; color: var(--text-primary);
        }}
        .mf-tile .mf-sub {{ font-size: .76rem; color: var(--text-secondary); }}
        .mf-tile .mf-delta {{ font-size: .8rem; font-weight: 560; display: flex; align-items: center; gap: .3rem; }}
        .mf-accent-rail {{ border-left: 3px solid var(--accent); }}

        /* ---- Section headers ---- */
        .mf-section {{ margin: 1.6rem 0 .7rem; }}
        .mf-section h3 {{
            font-size: 1.02rem; font-weight: 620; margin: 0; color: var(--text-primary);
            letter-spacing: -0.01em;
        }}
        .mf-section p {{ margin: .2rem 0 0; font-size: .8rem; color: var(--text-muted); }}

        /* ---- Pills / chips ---- */
        .mf-pill {{
            display: inline-flex; align-items: center; gap: .35rem;
            padding: .16rem .55rem; border-radius: 999px;
            font-size: .72rem; font-weight: 560; border: 1px solid var(--border);
            color: var(--text-secondary); background: var(--surface-raised);
        }}
        .mf-pill .mf-dot {{ width: .5rem; height: .5rem; border-radius: 50%; display: inline-block; }}

        /* ---- Callout ---- */
        .mf-callout {{
            border-left: 3px solid var(--accent); background: var(--surface);
            border-radius: 0 10px 10px 0; padding: .75rem 1rem; margin: .5rem 0;
            font-size: .85rem; color: var(--text-secondary);
        }}

        /* ---- Tabs ---- */
        .stTabs [data-baseweb="tab-list"] {{
            gap: .15rem; border-bottom: 1px solid var(--border);
            overflow-x: auto; scrollbar-width: thin;
        }}
        .stTabs [data-baseweb="tab"] {{
            height: 2.6rem; padding: 0 .95rem; background: transparent;
            border-radius: 8px 8px 0 0; color: var(--text-muted);
            font-size: .84rem; font-weight: 560; white-space: nowrap;
        }}
        .stTabs [aria-selected="true"] {{
            background: var(--surface); color: var(--text-primary);
            border-bottom: 2px solid var(--accent);
        }}

        /* ---- Dataframes: tabular figures belong here ---- */
        [data-testid="stDataFrame"] {{ font-variant-numeric: tabular-nums; }}
        [data-testid="stDataFrame"] div {{ border-radius: 10px; }}

        /* ---- Buttons ---- */
        .stButton > button {{
            border-radius: 9px; border: 1px solid var(--border);
            font-weight: 560; font-size: .84rem;
        }}
        .stButton > button[kind="primary"] {{
            background: var(--accent); border-color: var(--accent); color: #06101F;
        }}

        /* ---- Hold previous render at reduced opacity; no skeleton flash ---- */
        [data-testid="stStatusWidget"] {{ display: none; }}
        .stApp [data-stale="true"] {{ opacity: .55; transition: opacity .18s ease; }}

        /* ---- Login ---- */
        .mf-login {{
            max-width: 420px; margin: 12vh auto 0; padding: 2rem;
            background: var(--surface); border: 1px solid var(--border); border-radius: 16px;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------
# Auth gate
# --------------------------------------------------------------------------


def _expected_password() -> Optional[str]:
    try:
        return st.secrets.get("APP_PASSWORD")  # type: ignore[no-any-return]
    except Exception:
        return None


def require_login() -> bool:
    """Password gate. Returns True when the app may render.

    If no `APP_PASSWORD` secret is configured the gate is open -- that is the
    local-development path. On Streamlit Cloud, set the secret.
    """
    expected = _expected_password()
    if not expected:
        return True
    if st.session_state.get("authenticated"):
        return True

    st.markdown('<div class="mf-login">', unsafe_allow_html=True)
    st.markdown(f"### ◇ {APP_NAME}")
    st.caption("Enter the access password to continue.")
    pw = st.text_input("Password", type="password", label_visibility="collapsed", placeholder="Password")
    if st.button("Sign in", type="primary", use_container_width=True):
        if hmac.compare_digest(
            hashlib.sha256(pw.encode()).hexdigest(),
            hashlib.sha256(str(expected).encode()).hexdigest(),
        ):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    st.markdown("</div>", unsafe_allow_html=True)
    return False


# --------------------------------------------------------------------------
# Components
# --------------------------------------------------------------------------


def masthead(subtitle: str = APP_TAGLINE, right_html: str = "") -> None:
    st.markdown(
        f"""
        <div class="mf-masthead">
          <div style="display:flex;align-items:center;">
            <span class="mf-mark">◇</span>
            <div><h1>{APP_NAME}</h1><p>{subtitle}</p></div>
          </div>
          <div style="text-align:right;">{right_html}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def section(title: str, note: str = "") -> None:
    st.markdown(
        f'<div class="mf-section"><h3>{title}</h3>'
        + (f"<p>{note}</p>" if note else "")
        + "</div>",
        unsafe_allow_html=True,
    )


def _delta_html(delta_pct: Optional[float], good_when: str = "down") -> str:
    """Render a signed delta.

    `good_when` decides which direction earns the success ink. For spend, down
    is good; for coverage, up is good. The arrow carries the direction so the
    colour is never the only channel.
    """
    if delta_pct is None:
        return ""
    up = delta_pct >= 0
    is_good = (up and good_when == "up") or ((not up) and good_when == "down")
    colour = STATUS["good"] if is_good else STATUS["critical"]
    arrow = "▲" if up else "▼"
    return (
        f'<div class="mf-delta" style="color:{colour}">'
        f"<span>{arrow}</span><span>{abs(delta_pct):.1f}%</span></div>"
    )


def tile(
    label: str,
    value: str,
    sub: str = "",
    delta_pct: Optional[float] = None,
    good_when: str = "down",
    status: Optional[str] = None,
    accent: bool = False,
) -> None:
    """A stat tile. When the story is one number, this *is* the chart."""
    rail = " mf-accent-rail" if accent else ""
    badge = ""
    if status:
        badge = (
            f'<span class="mf-pill" style="color:{STATUS[status]}">'
            f'{STATUS_ICON[status]} {status.title()}</span>'
        )
    st.markdown(
        f"""
        <div class="mf-tile{rail}">
          <div class="mf-label">{label}</div>
          <div class="mf-value">{value}</div>
          {_delta_html(delta_pct, good_when)}
          <div class="mf-sub">{sub} {badge}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def tile_row(tiles: Sequence[dict]) -> None:
    cols = st.columns(len(tiles), gap="small")
    for col, spec in zip(cols, tiles):
        with col:
            tile(**spec)


def pill(text: str, colour: Optional[str] = None) -> str:
    dot = f'<span class="mf-dot" style="background:{colour}"></span>' if colour else ""
    return f'<span class="mf-pill">{dot}{text}</span>'


def status_pill(status: str, text: str = "") -> str:
    """Status colour is never alone -- it always ships with icon + label."""
    label = text or status.title()
    return (
        f'<span class="mf-pill" style="color:{STATUS[status]};border-color:{STATUS[status]}33">'
        f"{STATUS_ICON[status]} {label}</span>"
    )


def callout(markdown_text: str) -> None:
    st.markdown(f'<div class="mf-callout">{markdown_text}</div>', unsafe_allow_html=True)


def legend(entries: Iterable[tuple]) -> None:
    """An explicit legend row. Present whenever >= 2 series are drawn."""
    html = " ".join(pill(label, colour) for label, colour in entries)
    st.markdown(html, unsafe_allow_html=True)


def table_view(df, key: str, label: str = "Table view") -> None:
    """The WCAG-clean twin every chart must have.

    Tooltips enhance; they never gate. Any value visible in a chart is also
    reachable here, and downloadable.
    """
    with st.expander(label, expanded=False):
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.download_button(
            "Download CSV",
            df.to_csv(index=False).encode("utf-8"),
            file_name=f"{key}.csv",
            mime="text/csv",
            key=f"dl_{key}",
        )


def money(x: float, decimals: int = 0) -> str:
    """Compact currency for tiles; full precision for tables."""
    if x is None:
        return "—"
    a = abs(x)
    sign = "-" if x < 0 else ""
    if a >= 1_000_000_000:
        return f"{sign}${a / 1_000_000_000:.2f}B"
    if a >= 1_000_000:
        return f"{sign}${a / 1_000_000:.2f}M"
    if a >= 1_000:
        return f"{sign}${a / 1_000:.1f}K"
    return f"{sign}${a:,.{decimals}f}"


def pct(x: Optional[float], decimals: int = 1) -> str:
    return "—" if x is None else f"{x:.{decimals}f}%"
