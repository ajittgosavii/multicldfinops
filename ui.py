"""Streamlit chrome: theme, motion, the auth gate, and the component vocabulary.

Every visual primitive the app uses lives here so the tabs stay declarative.
Data colour comes from `theme`; identity colour from `brand`. This module never
invents a hex.

On motion
---------
Animation here is functional, not decorative. It does three jobs:

* **Orientation.** A staggered entrance tells you the KPI row is one group and
  the charts below are another. Order communicates structure.
* **Change.** A figure that just moved is worth re-reading. Tiles lift on hover,
  a critical status pill breathes, the live-data dot blinks.
* **Continuity.** Streamlit reruns the whole script on every widget change; a
  short fade-in keeps a re-render from reading as a page flash.

What it never does is animate a data mark's position or length, which would
misstate a value mid-flight.

Streamlit's `unsafe_allow_html` strips `<script>`, so everything below is pure
CSS keyframes and inline SVG. That is also why it degrades cleanly: a browser
that ignores an animation still gets the final state, because every keyframe
set ends at the resting style and uses `both` fill mode.

All of it collapses under `prefers-reduced-motion: reduce` -- one media query at
the bottom of the stylesheet, no per-component opt-in to forget.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from string import Template
from typing import Iterable, List, Optional, Sequence, Tuple

import streamlit as st

import brand
import theme
from theme import STATUS, STATUS_ICON

APP_NAME = brand.PRODUCT
APP_TAGLINE = brand.TAGLINE

# Any run of whitespace spanning a newline. Used to flatten HTML blocks.
_WS = re.compile(r"\s*\n\s*")


# --------------------------------------------------------------------------
# Page config + mode
# --------------------------------------------------------------------------


def page_config() -> None:
    st.set_page_config(
        page_title=brand.PAGE_TITLE,
        page_icon="◈",
        layout="wide",
        initial_sidebar_state="expanded",
    )


def mode() -> str:
    return st.session_state.get("colour_mode", theme.DEFAULT_MODE)


def _html(markup: str) -> str:
    """Collapse an HTML block to a single line before handing it to markdown.

    `st.markdown` dedents by the block's *common* leading whitespace. Interpolate
    anything with its own indentation -- an SVG, a nested component -- and the
    common prefix collapses, leaving lines indented four or more spaces. Markdown
    then renders them as an indented code block, and the page prints its own
    source. One line has no indentation to misread.
    """
    return _WS.sub(" ", markup).strip()


def _md(markup: str) -> None:
    st.markdown(_html(markup), unsafe_allow_html=True)


def authenticated() -> bool:
    return bool(st.session_state.get("authenticated")) or not _expected_password()


# --------------------------------------------------------------------------
# Stylesheet
# --------------------------------------------------------------------------

_CSS = Template(
    """
<style>
:root {
  --page: $page;
  --surface: $surface;
  --surface-raised: $surface_raised;
  --text-primary: $text_primary;
  --text-secondary: $text_secondary;
  --text-muted: $text_muted;
  --grid: $grid;
  --axis: $axis;
  --border: $border;
  --accent: $accent;
  --good: $good;
  --warning: $warning;
  --serious: $serious;
  --critical: $critical;
  --brand-azure: $b_azure;
  --brand-teal: $b_teal;
  --brand-violet: $b_violet;
  --brand-glow: $b_glow;
  --ease: cubic-bezier(.22,.61,.36,1);
}

html, body, [class*="css"] { font-family: $font; }
.stApp { background: var(--page); color: var(--text-primary); }

.block-container { padding-top: 1.4rem; padding-bottom: 3rem; max-width: 1640px; }
header[data-testid="stHeader"] { background: transparent; }
#MainMenu, footer { visibility: hidden; }

/* ===================== Motion primitives ===================== */
@keyframes mfFadeUp   { from { opacity:0; transform: translateY(14px); } to { opacity:1; transform:none; } }
@keyframes mfFadeIn   { from { opacity:0; } to { opacity:1; } }
@keyframes mfShimmer  { 0% { background-position: -220% 0; } 100% { background-position: 220% 0; } }
@keyframes mfAurora   { 0%,100% { transform: translate3d(0,0,0) scale(1); }
                        33%     { transform: translate3d(4%,-6%,0) scale(1.12); }
                        66%     { transform: translate3d(-5%,4%,0) scale(.94); } }
@keyframes mfFloat    { 0%,100% { transform: translateY(0) } 50% { transform: translateY(-16px) } }
@keyframes mfSpin     { to { transform: rotate(360deg); } }
@keyframes mfBreath   { 0%,100% { opacity:.55; r:3.2 } 50% { opacity:1; r:4.4 } }
@keyframes mfPulse    { 0%,100% { box-shadow: 0 0 0 0 currentColor; opacity:1 }
                        50%     { box-shadow: 0 0 0 4px transparent; opacity:.72 } }
@keyframes mfDot      { 0%,100% { opacity:1; transform: scale(1) } 50% { opacity:.35; transform: scale(.82) } }
@keyframes mfRail     { from { transform: scaleY(0) } to { transform: scaleY(1) } }
@keyframes mfSweep    { to { background-position: 200% center; } }
@keyframes mfDrift    { from { background-position: 0 0 } to { background-position: 64px 64px } }

/* Entrance stagger: columns are the app's grouping primitive, so they carry it. */
div[data-testid="stColumn"] { animation: mfFadeUp .46s var(--ease) both; }
div[data-testid="stColumn"]:nth-child(1) { animation-delay: .02s }
div[data-testid="stColumn"]:nth-child(2) { animation-delay: .07s }
div[data-testid="stColumn"]:nth-child(3) { animation-delay: .12s }
div[data-testid="stColumn"]:nth-child(4) { animation-delay: .17s }
div[data-testid="stColumn"]:nth-child(5) { animation-delay: .22s }
div[data-testid="stColumn"]:nth-child(6) { animation-delay: .27s }
div[data-testid="stColumn"]:nth-child(7) { animation-delay: .32s }
div[data-testid="stColumn"]:nth-child(8) { animation-delay: .37s }

div[data-testid="stPlotlyChart"] { animation: mfFadeUp .55s .08s var(--ease) both; }
div[data-testid="stDataFrame"]   { animation: mfFadeIn .5s .12s var(--ease) both; }

/* ===================== Sidebar ===================== */
section[data-testid="stSidebar"] {
  background: linear-gradient(180deg, var(--surface) 0%, var(--page) 140%);
  border-right: 1px solid var(--border);
}
section[data-testid="stSidebar"] .block-container { padding-top: 1.1rem; }
.mf-side-brand { display:flex; align-items:center; gap:.6rem; margin-bottom:.15rem; }
.mf-side-brand .mf-wordmark { font-size:1.02rem; font-weight:680; letter-spacing:-.01em; }
.mf-side-sub { font-size:.7rem; letter-spacing:.14em; text-transform:uppercase; color:var(--text-muted); margin-bottom:1rem; }

/* ===================== Brand mark ===================== */
.mf-mark-svg { display:block; overflow:visible; }
.mf-mark-spin { animation: mfSpin 26s linear infinite; }
.mf-mark-core { animation: mfBreath 3.4s ease-in-out infinite; }

.mf-wordmark {
  background: linear-gradient(100deg, var(--brand-glow), var(--brand-azure) 40%, var(--brand-violet) 70%, var(--brand-glow));
  background-size: 200% auto;
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent; color: transparent;
  animation: mfSweep 7s linear infinite;
}

/* ===================== Masthead ===================== */
.mf-masthead {
  position: relative; overflow: hidden;
  display:flex; align-items:center; justify-content:space-between; gap:1.5rem;
  padding: 1.05rem 1.4rem; margin-bottom: 1.15rem;
  background: linear-gradient(120deg, var(--surface) 0%, var(--surface-raised) 55%, var(--surface) 100%);
  border: 1px solid var(--border); border-radius: 16px;
  animation: mfFadeUp .5s var(--ease) both;
}
.mf-masthead::before {   /* slow aurora wash behind the title */
  content:""; position:absolute; inset:-60% -20%;
  background:
    radial-gradient(38% 55% at 18% 40%, color-mix(in srgb, var(--brand-azure) 22%, transparent), transparent 70%),
    radial-gradient(32% 50% at 72% 60%, color-mix(in srgb, var(--brand-violet) 18%, transparent), transparent 70%);
  filter: blur(14px); opacity:.55; pointer-events:none;
  animation: mfAurora 22s ease-in-out infinite;
}
.mf-masthead > * { position: relative; z-index: 1; }
.mf-masthead h1 { font-size:1.42rem; font-weight:660; letter-spacing:-.022em; margin:0; color:var(--text-primary); }
.mf-masthead p  { margin:.22rem 0 0; color:var(--text-secondary); font-size:.85rem; }
.mf-eyebrow { font-size:.68rem; letter-spacing:.2em; text-transform:uppercase; color:var(--text-muted); margin:0 0 .1rem; }

/* ===================== Stat tiles ===================== */
.mf-tile {
  position:relative; overflow:hidden;
  background: var(--surface); border:1px solid var(--border);
  border-radius: 13px; padding:.95rem 1.05rem; height:100%;
  display:flex; flex-direction:column; gap:.32rem;
  transition: transform .22s var(--ease), border-color .22s var(--ease), box-shadow .22s var(--ease);
}
.mf-tile::after {  /* one-shot shimmer on entrance */
  content:""; position:absolute; inset:0; pointer-events:none;
  background: linear-gradient(100deg, transparent 40%,
              color-mix(in srgb, var(--text-primary) 6%, transparent) 50%, transparent 60%);
  background-size: 220% 100%;
  animation: mfShimmer 1.5s .25s var(--ease) 1 both;
}
.mf-tile:hover {
  transform: translateY(-3px);
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
  box-shadow: 0 10px 30px -12px color-mix(in srgb, var(--accent) 55%, transparent);
}
.mf-tile .mf-label { font-size:.69rem; font-weight:620; letter-spacing:.08em; text-transform:uppercase; color:var(--text-muted); }
.mf-tile .mf-value {
  font-size:1.86rem; font-weight:620; line-height:1.05; letter-spacing:-.026em;
  color:var(--text-primary);
  animation: mfFadeUp .5s .1s var(--ease) both;
}
.mf-tile .mf-sub   { font-size:.75rem; color:var(--text-secondary); }
.mf-tile .mf-delta { font-size:.79rem; font-weight:560; display:flex; align-items:center; gap:.3rem; }
.mf-accent-rail::before {
  content:""; position:absolute; left:0; top:0; bottom:0; width:3px;
  background: linear-gradient(180deg, var(--brand-glow), var(--accent), var(--brand-violet));
  transform-origin: top; animation: mfRail .6s .15s var(--ease) both;
}
.mf-accent-rail .mf-value {
  background: linear-gradient(100deg, var(--text-primary), var(--brand-glow) 50%, var(--text-primary));
  background-size: 200% auto; -webkit-background-clip:text; background-clip:text;
  -webkit-text-fill-color:transparent; color:transparent;
  animation: mfFadeUp .5s .1s var(--ease) both, mfSweep 6s 1s linear infinite;
}

/* ===================== Section headers ===================== */
.mf-section { margin:1.6rem 0 .7rem; animation: mfFadeIn .5s var(--ease) both; }
.mf-section h3 { font-size:1.01rem; font-weight:620; margin:0; color:var(--text-primary); letter-spacing:-.01em; }
.mf-section p  { margin:.2rem 0 0; font-size:.79rem; color:var(--text-muted); }

/* ===================== Pills ===================== */
.mf-pill {
  display:inline-flex; align-items:center; gap:.35rem;
  padding:.17rem .58rem; border-radius:999px;
  font-size:.71rem; font-weight:560; border:1px solid var(--border);
  color:var(--text-secondary); background:var(--surface-raised);
  transition: transform .18s var(--ease);
}
.mf-pill:hover { transform: translateY(-1px); }
.mf-pill .mf-dot { width:.5rem; height:.5rem; border-radius:50%; display:inline-block; }
.mf-pill.mf-live .mf-dot { animation: mfDot 1.7s ease-in-out infinite; }
.mf-pill.mf-critical { animation: mfPulse 2.2s ease-in-out infinite; }

/* ===================== Callout ===================== */
.mf-callout {
  position:relative; border-left:3px solid var(--accent); background:var(--surface);
  border-radius:0 10px 10px 0; padding:.75rem 1rem; margin:.55rem 0;
  font-size:.845rem; color:var(--text-secondary); line-height:1.55;
  animation: mfFadeUp .5s var(--ease) both;
}
.mf-callout strong { color: var(--text-primary); font-weight:620; }
.mf-callout code {
  background: var(--surface-raised); border:1px solid var(--border);
  padding:.05rem .3rem; border-radius:5px; font-family:$mono; font-size:.9em;
}
.mf-callout a { color: var(--accent); text-decoration:none; border-bottom:1px solid transparent; }
.mf-callout a:hover { border-bottom-color: var(--accent); }

/* ===================== Tabs ===================== */
.stTabs [data-baseweb="tab-list"] { gap:.15rem; border-bottom:1px solid var(--border); overflow-x:auto; scrollbar-width:thin; }
.stTabs [data-baseweb="tab"] {
  height:2.6rem; padding:0 .95rem; background:transparent; border-radius:9px 9px 0 0;
  color:var(--text-muted); font-size:.84rem; font-weight:560; white-space:nowrap;
  transition: color .18s var(--ease), background .18s var(--ease);
}
.stTabs [data-baseweb="tab"]:hover { color:var(--text-secondary); background:color-mix(in srgb, var(--surface) 60%, transparent); }
.stTabs [aria-selected="true"] { background:var(--surface); color:var(--text-primary); position:relative; }
.stTabs [aria-selected="true"]::after {
  content:""; position:absolute; left:0; right:0; bottom:-1px; height:2px;
  background: linear-gradient(90deg, var(--brand-glow), var(--accent), var(--brand-violet));
  animation: mfFadeIn .3s var(--ease) both;
}

/* ===================== Data + inputs ===================== */
[data-testid="stDataFrame"] { font-variant-numeric: tabular-nums; }
[data-testid="stDataFrame"] div { border-radius:10px; }

.stButton > button {
  border-radius:10px; border:1px solid var(--border); font-weight:560; font-size:.84rem;
  transition: transform .18s var(--ease), box-shadow .18s var(--ease), filter .18s var(--ease);
}
.stButton > button:hover { transform: translateY(-1px); }
.stButton > button[kind="primary"] {
  background: linear-gradient(100deg, var(--brand-azure), var(--brand-violet));
  border-color: transparent; color:#04070F;
}
.stButton > button[kind="primary"]:hover { filter: brightness(1.08); box-shadow: 0 8px 26px -10px var(--brand-azure); }

[data-testid="stStatusWidget"] { display:none; }
.stApp [data-stale="true"] { opacity:.55; transition: opacity .18s var(--ease); }

/* ===================== Reduced motion ===================== */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: .001ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: .001ms !important;
  }
}
</style>
"""
)


def inject_css() -> None:
    s = theme.surface(mode())
    st.markdown(
        _CSS.substitute(
            page=s.page,
            surface=s.surface,
            surface_raised=s.surface_raised,
            text_primary=s.text_primary,
            text_secondary=s.text_secondary,
            text_muted=s.text_muted,
            grid=s.grid,
            axis=s.axis,
            border=s.border,
            accent=s.categorical[0],
            good=STATUS["good"],
            warning=STATUS["warning"],
            serious=STATUS["serious"],
            critical=STATUS["critical"],
            b_azure=brand.AZURE,
            b_teal=brand.TEAL,
            b_violet=brand.VIOLET,
            b_glow=brand.GLOW,
            font=theme.FONT_STACK,
            mono=theme.FONT_MONO,
        ),
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------

_LOGIN_CSS = Template(
    """
<style>
/* The gate owns the whole viewport: no sidebar, no toolbar, no distraction. */
section[data-testid="stSidebar"], header[data-testid="stHeader"] { display:none !important; }
.block-container { padding-top: 0 !important; max-width: 100% !important; }

.mf-bg { position:fixed; inset:0; z-index:0; overflow:hidden; background:$deep; }
.mf-bg::after {  /* a slowly drifting technical grid */
  content:""; position:absolute; inset:0; opacity:.5;
  background-image:
    linear-gradient(color-mix(in srgb, $azure 8%, transparent) 1px, transparent 1px),
    linear-gradient(90deg, color-mix(in srgb, $azure 8%, transparent) 1px, transparent 1px);
  background-size: 64px 64px;
  mask-image: radial-gradient(70% 60% at 50% 45%, #000 30%, transparent 80%);
  animation: mfDrift 32s linear infinite;
}
.mf-orb { position:absolute; border-radius:50%; filter: blur(90px); opacity:.5; will-change: transform; }
.mf-orb-1 { width:52vw; height:52vw; left:-12vw;  top:-16vw; background:$azure;  animation: mfAurora 26s ease-in-out infinite; }
.mf-orb-2 { width:46vw; height:46vw; right:-10vw; top:6vw;   background:$violet; animation: mfAurora 32s ease-in-out infinite reverse; }
.mf-orb-3 { width:40vw; height:40vw; left:26vw;   bottom:-20vw; background:$teal; animation: mfAurora 38s ease-in-out infinite; opacity:.34; }

.mf-spark { position:absolute; width:3px; height:3px; border-radius:50%; background:$glow; opacity:.55; }

/* The card is a *keyed Streamlit container* (`st-key-mf_login_card`), not a raw
   <div>. A raw div cannot wrap Streamlit widgets -- the browser closes it at the
   end of the markdown block, and the password field lands outside the glass. */
.mf-login-pad { height: 9vh; }
div.st-key-mf_login_card {
  position:relative; z-index:2;
  padding: 2.1rem 2.1rem 1.8rem;
  border-radius: 22px;
  background: linear-gradient(160deg, rgba(255,255,255,.075), rgba(255,255,255,.022));
  border: 1px solid rgba(255,255,255,.12);
  backdrop-filter: blur(22px) saturate(140%);
  -webkit-backdrop-filter: blur(22px) saturate(140%);
  box-shadow: 0 30px 90px -30px rgba(0,0,0,.85), inset 0 1px 0 rgba(255,255,255,.14);
  animation: mfFadeUp .8s var(--ease) both;
}
.mf-card-head { display:flex; align-items:center; gap:.85rem; margin-bottom:1.35rem; }
.mf-card-head .mf-brandline { font-size:1.35rem; font-weight:700; letter-spacing:-.02em; line-height:1.1; }
.mf-card-head .mf-productline { font-size:.7rem; letter-spacing:.17em; text-transform:uppercase; color:rgba(230,238,250,.6); margin-top:.2rem; }

.mf-claims { position:relative; height:1.35rem; margin:-.4rem 0 1.35rem; }
.mf-claims span {
  position:absolute; inset:0; font-size:.83rem; color:rgba(230,238,250,.78);
  opacity:0; transform: translateY(6px);
  animation: mfClaim $cycle_total s var(--ease) infinite;
}
@keyframes mfClaim {
  0%   { opacity:0; transform: translateY(6px) }
  4%   { opacity:1; transform: none }
  16%  { opacity:1; transform: none }
  20%  { opacity:0; transform: translateY(-6px) }
  100% { opacity:0; transform: translateY(-6px) }
}

.mf-login-label { font-size:.72rem; letter-spacing:.12em; text-transform:uppercase; color:rgba(230,238,250,.55); margin-bottom:.35rem; }

.mf-proof { display:flex; gap:1.4rem; margin-top:1.5rem; padding-top:1.2rem; border-top:1px solid rgba(255,255,255,.1); }
.mf-proof div { animation: mfFadeUp .7s var(--ease) both; }
.mf-proof div:nth-child(1){animation-delay:.30s} .mf-proof div:nth-child(2){animation-delay:.38s}
.mf-proof div:nth-child(3){animation-delay:.46s} .mf-proof div:nth-child(4){animation-delay:.54s}
.mf-proof .n { font-size:1.18rem; font-weight:660; color:#fff; letter-spacing:-.02em; }
.mf-proof .l { font-size:.66rem; letter-spacing:.13em; text-transform:uppercase; color:rgba(230,238,250,.5); }

.mf-foot { margin-top:1.25rem; font-size:.7rem; color:rgba(230,238,250,.42); text-align:center; }

/* Streamlit widgets inside the glass card */
.mf-shell + div [data-testid="stTextInput"] input,
[data-testid="stTextInput"] input {
  background: rgba(6,12,24,.55) !important;
  border:1px solid rgba(255,255,255,.14) !important;
  color:#EAF2FF !important; border-radius:11px !important; height:2.85rem;
  transition: border-color .2s var(--ease), box-shadow .2s var(--ease);
}
[data-testid="stTextInput"] input:focus {
  border-color: $azure !important;
  box-shadow: 0 0 0 4px color-mix(in srgb, $azure 22%, transparent) !important;
}
</style>
"""
)


def _expected_password() -> Optional[str]:
    """Streamlit secrets first, then the environment.

    Secrets are the Streamlit Cloud path; the env var makes the gate reachable
    from a test and from a container that injects config rather than files.
    """
    try:
        if "APP_PASSWORD" in st.secrets:
            return str(st.secrets["APP_PASSWORD"])
    except Exception:
        pass
    import os

    return os.environ.get("APP_PASSWORD") or None


def _proof_points() -> List[Tuple[str, str]]:
    """Count the claims from the code, so the login page cannot overstate.

    If a count fails to import we drop that claim rather than print the
    hard-coded fallback and quietly lie about it.
    """
    points: List[Tuple[str, str]] = [("3", "clouds")]
    try:
        import connectors

        points.append((str(len(connectors.REGISTRY)), "connectors"))
    except Exception:
        pass
    try:
        import optimize

        points.append((str(len(optimize.LEVERS)), "levers"))
    except Exception:
        pass
    points.append(("12", "dashboards"))
    return points


def _sparks(n: int = 16) -> str:
    """Deterministic floating motes. Positions are fixed, not random, so the
    login page renders identically on every rerun."""
    out = []
    for i in range(n):
        left = (i * 61) % 97 + 2
        top = (i * 37) % 88 + 5
        dur = 7 + (i % 5) * 2.4
        delay = (i % 7) * 0.9
        size = 2 + (i % 3)
        out.append(
            f'<span class="mf-spark" style="left:{left}%;top:{top}%;width:{size}px;height:{size}px;'
            f"animation: mfFloat {dur}s ease-in-out {delay}s infinite, mfFadeIn 1.2s {delay}s both;"
            f'"></span>'
        )
    return "".join(out)


def gate_enabled() -> bool:
    return bool(_expected_password())


def _preview_requested() -> bool:
    """`?login=preview` renders the gate with no password configured.

    A password gate cannot gate anything when there is no password, so this
    never authenticates -- it exists so the sign-in page can be seen and
    screenshotted on an ungated deployment. The card says so on its face rather
    than implying a security boundary that is not there.
    """
    try:
        return st.query_params.get("login") == "preview"
    except Exception:
        return False


def require_login() -> bool:
    """Password gate. Returns True when the app may render.

    With no `APP_PASSWORD` secret there is nothing to check, so the gate is open
    -- that is the local-development path, and it is why the sign-in page does
    not appear on a Streamlit Cloud app whose secret was never set. Add
    `APP_PASSWORD` to the app's secrets and reboot.
    """
    expected = _expected_password()
    preview = not expected and _preview_requested()

    if not expected and not preview:
        return True
    if st.session_state.get("authenticated"):
        return True

    claims = brand.ROTATING_CLAIMS
    cycle = len(claims) * 4  # each claim visible ~4s of the loop

    st.markdown(
        _LOGIN_CSS.substitute(
            deep=brand.DEEP,
            azure=brand.AZURE,
            teal=brand.TEAL,
            violet=brand.VIOLET,
            glow=brand.GLOW,
            cycle_total=cycle,
        ),
        unsafe_allow_html=True,
    )

    _md(
        '<div class="mf-bg">'
        '<div class="mf-orb mf-orb-1"></div>'
        '<div class="mf-orb mf-orb-2"></div>'
        '<div class="mf-orb mf-orb-3"></div>'
        f"{_sparks()}</div>"
    )

    claim_spans = "".join(
        f'<span style="animation-delay:{i * 4}s">{c}</span>' for i, c in enumerate(claims)
    )
    proof = "".join(f'<div><div class="n">{n}</div><div class="l">{l}</div></div>' for n, l in _proof_points())

    _md('<div class="mf-login-pad"></div>')
    _, mid, _ = st.columns([1, 1.15, 1])
    with mid:
        with st.container(key="mf_login_card"):
            label = "Preview — no access key configured" if preview else "Access key"
            _md(
                f'<div class="mf-card-head">{brand.mark_svg(46, uid="login")}'
                f'<div><div class="mf-brandline mf-wordmark">{brand.BRAND}</div>'
                f'<div class="mf-productline">{brand.PRODUCT}</div></div></div>'
                f'<div class="mf-claims">{claim_spans}</div>'
                f'<div class="mf-login-label">{label}</div>'
            )

            if preview:
                # No password exists, so nothing can be verified. Say so, and let
                # the viewer through -- pretending otherwise would be theatre.
                st.caption(
                    "This deployment has no `APP_PASSWORD` secret, so sign-in is "
                    "disabled and this page is a preview. Set the secret and reboot "
                    "to make it a real gate."
                )
                if st.button("Continue to the Command Center", type="primary", width="stretch"):
                    st.session_state["authenticated"] = True
                    st.rerun()
                _md(
                    f'<div class="mf-proof">{proof}</div>'
                    f'<div class="mf-foot">{brand.BRAND} · FOCUS 1.2 conformant · '
                    f"Demo data contains no customer information</div>"
                )
                return False

            pw = st.text_input(
                "Password", type="password", label_visibility="collapsed", placeholder="Enter access password"
            )
            signed_in = st.button("Enter Command Center", type="primary", width="stretch")

            _md(
                f'<div class="mf-proof">{proof}</div>'
                f'<div class="mf-foot">{brand.BRAND} · FOCUS 1.2 conformant · '
                f"Demo data contains no customer information</div>"
            )

            if signed_in:
                if hmac.compare_digest(
                    hashlib.sha256(pw.encode()).hexdigest(),
                    hashlib.sha256(str(expected).encode()).hexdigest(),
                ):
                    st.session_state["authenticated"] = True
                    st.rerun()
                else:
                    st.error("Incorrect password.")

    return False


# --------------------------------------------------------------------------
# Components
# --------------------------------------------------------------------------


def sidebar_brand() -> None:
    _md(
        f'<div class="mf-side-brand">{brand.mark_svg(26, uid="side")}'
        f'<span class="mf-wordmark">{brand.BRAND}</span></div>'
        f'<div class="mf-side-sub">Command Center</div>'
    )


def masthead(subtitle: str = APP_TAGLINE, right_html: str = "") -> None:
    _md(
        f'<div class="mf-masthead">'
        f'<div style="display:flex;align-items:center;gap:.95rem;">'
        f'{brand.mark_svg(42, uid="head")}'
        f'<div><p class="mf-eyebrow">{brand.BRAND}</p>'
        f"<h1>{APP_NAME}</h1><p>{subtitle}</p></div></div>"
        f'<div style="text-align:right;">{right_html}</div></div>'
    )


def section(title: str, note: str = "") -> None:
    _md(f'<div class="mf-section"><h3>{title}</h3>' + (f"<p>{note}</p>" if note else "") + "</div>")


def _delta_html(delta_pct: Optional[float], good_when: str = "down") -> str:
    """Render a signed delta. The arrow carries direction, so colour is never
    the only channel."""
    if delta_pct is None:
        return ""
    up = delta_pct >= 0
    is_good = (up and good_when == "up") or ((not up) and good_when == "down")
    colour = STATUS["good"] if is_good else STATUS["critical"]
    arrow = "▲" if up else "▼"
    return f'<div class="mf-delta" style="color:{colour}"><span>{arrow}</span><span>{abs(delta_pct):.1f}%</span></div>'


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
        crit = " mf-critical" if status == "critical" else ""
        badge = (
            f'<span class="mf-pill{crit}" style="color:{STATUS[status]}">'
            f"{STATUS_ICON[status]} {status.title()}</span>"
        )
    _md(
        f'<div class="mf-tile{rail}">'
        f'<div class="mf-label">{label}</div>'
        f'<div class="mf-value">{value}</div>'
        f"{_delta_html(delta_pct, good_when)}"
        f'<div class="mf-sub">{sub} {badge}</div></div>'
    )


def tile_row(tiles: Sequence[dict]) -> None:
    cols = st.columns(len(tiles), gap="small")
    for col, spec in zip(cols, tiles):
        with col:
            tile(**spec)


def pill(text: str, colour: Optional[str] = None, live: bool = False) -> str:
    dot = f'<span class="mf-dot" style="background:{colour}"></span>' if colour else ""
    cls = "mf-pill mf-live" if live else "mf-pill"
    return f'<span class="{cls}">{dot}{text}</span>'


def status_pill(status: str, text: str = "") -> str:
    """Status colour is never alone -- it always ships with icon + label."""
    label = text or status.title()
    crit = " mf-critical" if status == "critical" else ""
    return (
        f'<span class="mf-pill{crit}" style="color:{STATUS[status]};border-color:{STATUS[status]}33">'
        f"{STATUS_ICON[status]} {label}</span>"
    )


def _md_inline(text: str) -> str:
    """Minimal inline markdown -> HTML.

    `st.markdown(..., unsafe_allow_html=True)` does not parse markdown *inside*
    a raw HTML element, so a callout wrapped in a `<div>` used to print its
    asterisks literally. Rather than drop the wrapper (and its border and
    animation), we translate the four inline forms the callouts actually use.
    """
    import html
    import re

    out = html.escape(text, quote=False)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", out)
    return out


def callout(markdown_text: str) -> None:
    _md(f'<div class="mf-callout">{_md_inline(markdown_text)}</div>')


def legend(entries: Iterable[tuple]) -> None:
    """An explicit legend row. Present whenever >= 2 series are drawn."""
    _md(" ".join(pill(label, colour) for label, colour in entries))


def table_view(df, key: str, label: str = "Table view") -> None:
    """The WCAG-clean twin every chart must have. Tooltips enhance; they never
    gate. Any value visible in a chart is also reachable here."""
    with st.expander(label, expanded=False):
        st.dataframe(df, width="stretch", hide_index=True)
        st.download_button(
            "Download CSV",
            df.to_csv(index=False).encode("utf-8"),
            file_name=f"{key}.csv",
            mime="text/csv",
            key=f"dl_{key}",
        )


def money(x: Optional[float], decimals: int = 0) -> str:
    """Compact currency for tiles; full precision belongs in tables."""
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
