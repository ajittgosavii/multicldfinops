"""Brand identity.

Kept separate from `theme.py` because `theme` owns *data* colour -- the
CVD-validated categorical slots that encode meaning in charts -- while this
module owns *identity* colour, which encodes nothing and must never be used to
paint a data mark. Mixing the two is how a brand blue ends up meaning "AWS".
"""

from __future__ import annotations

BRAND = "Infosys"
PRODUCT = "Multi-Cloud FinOps Command Center"
TAGLINE = "AWS · Azure · GCP · OCI — allocation, forecast, and optimization in one plane"
PAGE_TITLE = f"{BRAND} | {PRODUCT}"

# Identity palette. Chrome only: mastheads, the login page, the brand mark.
# Never a series colour.
INK = "#0A1930"
DEEP = "#04070F"
AZURE = "#4FB3F5"
TEAL = "#2FD8C4"
VIOLET = "#8B7BF0"
AMBER = "#F2B45A"
GLOW = "#7FE3FF"

# Phrases the login page cycles through. Each is a claim the product actually
# makes good on somewhere in the app -- no marketing that the code cannot back.
ROTATING_CLAIMS = [
    "Every source normalised to FOCUS 1.2",
    "Showback, chargeback, and the shared-cost split",
    "Two-year forecasts that see the commitment cliff",
    "59 optimization levers, detected from the bill",
    "An agent team that only quotes numbers it measured",
]

# Rendered as the login page's proof strip. These are counted from the code, so
# if the numbers drift the strip is wrong -- see `ui._proof_points`.
PROOF = [
    ("4", "clouds"),
    ("18", "connectors"),
    ("59", "levers"),
    ("12", "dashboards"),
]


def hero_svg() -> str:
    """The login page's hero: the platform's architecture, animated.

    It draws the one idea -- four clouds and any procured FinOps tool collapse
    into a single FOCUS frame, and everything downstream reads only that. Flow
    dashes travel left to right along the paths, nodes breathe, and the FOCUS
    box's outline draws itself once on load.

    Single line, like `mark_svg`, so markdown cannot mistake it for a code block.
    Pure CSS animation on classed elements, so `prefers-reduced-motion` stops it
    from one place.
    """
    ink = "rgba(230,238,250,.72)"
    faint = "rgba(230,238,250,.34)"

    def node(x, y, w, h, label, colour, delay):
        return (
            f'<g class="mf-h-node" style="animation-delay:{delay}s">'
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="rgba(255,255,255,.04)" '
            f'stroke="{colour}" stroke-width="1.4"/>'
            f'<text x="{x + w / 2}" y="{y + h / 2 + 4}" text-anchor="middle" font-size="12.5" '
            f'font-weight="600" fill="{ink}" font-family="system-ui,sans-serif">{label}</text></g>'
        )

    def flow(d, delay):
        return (
            f'<path d="{d}" fill="none" stroke="{faint}" stroke-width="1.2"/>'
            f'<path d="{d}" fill="none" stroke="{AZURE}" stroke-width="1.6" '
            f'class="mf-h-flow" style="animation-delay:{delay}s"/>'
        )

    parts = [
        '<svg viewBox="0 0 560 330" class="mf-hero-svg" xmlns="http://www.w3.org/2000/svg" '
        'role="img" aria-label="Four clouds and any FinOps tool normalise to one FOCUS frame">',
        "<defs>",
        f'<linearGradient id="mfhg" x1="0" y1="0" x2="560" y2="0">'
        f'<stop offset="0%" stop-color="{AZURE}"/><stop offset="55%" stop-color="{TEAL}"/>'
        f'<stop offset="100%" stop-color="{VIOLET}"/></linearGradient>',
        f'<filter id="mfglow"><feGaussianBlur stdDeviation="6" result="b"/>'
        f'<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>',
        "</defs>",
    ]

    # Sources -> connector. Five nodes of height 40 on a 56px pitch, centred on
    # the connector's own centre line (y=143), so the fan stays symmetric:
    # centres land at 31, 87, 143, 199, 255 and the column spans 11..275.
    sources = [("AWS", AZURE), ("Azure", TEAL), ("GCP", VIOLET), ("OCI", AMBER), ("Any tool", faint)]
    for i, (label, colour) in enumerate(sources):
        cy = 31 + i * 56
        parts.append(node(14, cy - 20, 92, 40, label, colour, i * 0.32))
        parts.append(flow(f"M106 {cy} C 160 {cy}, 175 143, 218 143", i * 0.42))

    # Connector -> FOCUS
    parts.append(node(218, 121, 96, 44, "Connector", GLOW, 0.2))
    parts.append(flow("M314 143 L 358 143", 0.7))

    # The FOCUS frame -- the centre of gravity, so it gets the glow and the draw-on
    parts.append(
        '<g filter="url(#mfglow)">'
        f'<rect x="358" y="112" width="112" height="62" rx="12" fill="rgba(79,179,245,.10)" '
        f'stroke="url(#mfhg)" stroke-width="2" class="mf-h-draw"/></g>'
        f'<text x="414" y="138" text-anchor="middle" font-size="13" font-weight="700" '
        f'fill="#EAF2FF" font-family="system-ui,sans-serif">FOCUS</text>'
        f'<text x="414" y="156" text-anchor="middle" font-size="10.5" letter-spacing="1.6" '
        f'fill="{faint}" font-family="system-ui,sans-serif">1.2 FRAME</text>'
    )

    # FOCUS -> outputs
    for i, (label, y) in enumerate([("KPIs", 34), ("Forecast", 118), ("Optimize", 202), ("Agents", 268)]):
        parts.append(flow(f"M470 143 C 500 143, 500 {y + 20}, 520 {y + 20}", 0.9 + i * 0.18))
        parts.append(
            f'<g class="mf-h-node" style="animation-delay:{1.2 + i * 0.2}s">'
            f'<circle cx="528" cy="{y + 20}" r="5" fill="{GLOW}"/>'
            f'<text x="520" y="{y + 8}" text-anchor="end" font-size="11.5" fill="{ink}" '
            f'font-family="system-ui,sans-serif">{label}</text></g>'
        )

    parts.append("</svg>")
    return "".join(parts)


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
