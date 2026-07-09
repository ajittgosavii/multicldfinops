"""Brand identity.

Kept separate from `theme.py` because `theme` owns *data* colour -- the
CVD-validated categorical slots that encode meaning in charts -- while this
module owns *identity* colour, which encodes nothing and must never be used to
paint a data mark. Mixing the two is how a brand blue ends up meaning "AWS".
"""

from __future__ import annotations

BRAND = "Infosys"
PRODUCT = "Multi-Cloud FinOps Command Center"
TAGLINE = "AWS · Azure · GCP — allocation, forecast, and optimization in one plane"
PAGE_TITLE = f"{BRAND} | {PRODUCT}"

# Identity palette. Chrome only: mastheads, the login page, the brand mark.
# Never a series colour.
INK = "#0A1930"
DEEP = "#04070F"
AZURE = "#4FB3F5"
TEAL = "#2FD8C4"
VIOLET = "#8B7BF0"
GLOW = "#7FE3FF"

# Phrases the login page cycles through. Each is a claim the product actually
# makes good on somewhere in the app -- no marketing that the code cannot back.
ROTATING_CLAIMS = [
    "Every source normalised to FOCUS 1.2",
    "Showback, chargeback, and the shared-cost split",
    "Two-year forecasts that see the commitment cliff",
    "53 optimization levers, detected from the bill",
    "An agent team that only quotes numbers it measured",
]

# Rendered as the login page's proof strip. These are counted from the code, so
# if the numbers drift the strip is wrong -- see `ui._proof_points`.
PROOF = [
    ("3", "clouds"),
    ("17", "connectors"),
    ("53", "levers"),
    ("12", "dashboards"),
]


def mark_svg(size: int = 44, spin: bool = True, uid: str = "") -> str:
    """The brand mark: a stacked diamond that slowly rotates and breathes.

    Returned as a SINGLE LINE, deliberately. `st.markdown` dedents a block by its
    *common* leading whitespace, so interpolating a multi-line, differently
    indented SVG into an HTML block leaves the surviving lines indented four or
    more spaces -- and markdown renders those as a code fence. The masthead
    printed its own source once. Never again: no newlines, no indentation.

    `uid` namespaces the gradient ids. Two marks on one page sharing an id would
    make the second reuse the first's gradient.

    SMIL is avoided -- CSS keyframes on the group let `prefers-reduced-motion`
    switch the whole thing off from one place.
    """
    cls = "mf-mark-spin" if spin else ""
    g1, g2 = f"mfg1{uid}", f"mfg2{uid}"
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 64 64" fill="none" '
        f'xmlns="http://www.w3.org/2000/svg" class="mf-mark-svg" aria-hidden="true">'
        f'<defs><linearGradient id="{g1}" x1="0" y1="0" x2="64" y2="64">'
        f'<stop offset="0%" stop-color="{GLOW}"/>'
        f'<stop offset="55%" stop-color="{AZURE}"/>'
        f'<stop offset="100%" stop-color="{VIOLET}"/></linearGradient>'
        f'<linearGradient id="{g2}" x1="64" y1="0" x2="0" y2="64">'
        f'<stop offset="0%" stop-color="{TEAL}"/>'
        f'<stop offset="100%" stop-color="{AZURE}"/></linearGradient></defs>'
        f'<g class="{cls}" style="transform-origin:32px 32px">'
        f'<rect x="32" y="4" width="28" height="28" rx="4" transform="rotate(45 32 4)" '
        f'stroke="url(#{g1})" stroke-width="2.5"/>'
        f'<rect x="32" y="18" width="18" height="18" rx="3" transform="rotate(45 32 18)" '
        f'stroke="url(#{g2})" stroke-width="2" opacity="0.85"/></g>'
        f'<circle cx="32" cy="32" r="3.2" fill="{GLOW}" class="mf-mark-core"/></svg>'
    )
