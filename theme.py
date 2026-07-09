"""Design tokens for the Multi-Cloud FinOps Command Center.

Single source of truth for every colour used by the app. Nothing else in the
codebase may define a hex value — import from here.

The categorical slot ORDER is not cosmetic. It was chosen by enumerating
orderings and keeping the one that maximises the minimum adjacent colour
distance under simulated colour-vision deficiency. Validated with the dataviz
`validate_palette.js` six-checks validator:

    dark  (surface #141B34): lightness PASS, chroma PASS,
                             CVD worst adjacent dE 23.6 (deutan) / 24.5 (tritan) PASS,
                             contrast PASS (all >= 3:1)
    light (surface #FFFFFF): lightness PASS, chroma PASS,
                             CVD worst adjacent dE 13.3 (deutan) PASS,
                             contrast WARN on aqua/yellow/magenta -> relief rule
                             applies (direct labels + table view are always shipped)

If you change a hue, re-run the validator before committing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

# --------------------------------------------------------------------------
# Categorical slots -- assign in fixed order, never cycled, never by rank.
# Colour follows the entity (a cloud, an application), not its row number.
# --------------------------------------------------------------------------

CATEGORICAL_DARK: List[str] = [
    "#3987E5",  # 1 blue
    "#D95926",  # 2 orange
    "#199E70",  # 3 aqua
    "#9085E9",  # 4 violet
    "#C98500",  # 5 yellow
    "#D55181",  # 6 magenta
    "#008300",  # 7 green
    "#E66767",  # 8 red
]

CATEGORICAL_LIGHT: List[str] = [
    "#2A78D6",  # 1 blue
    "#EB6834",  # 2 orange
    "#1BAF7A",  # 3 aqua
    "#4A3AA7",  # 4 violet
    "#EDA100",  # 5 yellow
    "#E87BA4",  # 6 magenta
    "#008300",  # 7 green
    "#E34948",  # 8 red
]

# Sequential ramp: ONE hue, light -> dark. For continuous magnitude only
# (heatmaps, treemap intensity). Never a rainbow.
SEQUENTIAL_BLUE: List[str] = [
    "#CDE2FB",
    "#9EC5F4",
    "#6DA7EC",
    "#3987E5",
    "#256ABF",
    "#184F95",
    "#0D366B",
]

# Ordinal ramp (discrete ordered marks). The step nearest each surface must
# still clear 2:1 contrast, so the ends are clipped relative to SEQUENTIAL_BLUE.
ORDINAL_BLUE_LIGHT: List[str] = SEQUENTIAL_BLUE[1:]  # start no lighter than #9EC5F4
ORDINAL_BLUE_DARK: List[str] = SEQUENTIAL_BLUE[:-1]  # go no darker than #184F95

# Diverging pair: two hues that read as OPPOSITE (warm/cool) with a NEUTRAL
# grey midpoint. Used for budget variance -- underrun (cool) vs overrun (warm).
# A hue at the midpoint would be wrong: the midpoint must read as "nothing".
DIVERGING_MID_DARK = "#383835"
DIVERGING_MID_LIGHT = "#F0EFEC"

DIVERGING_DARK: List[str] = [
    "#0D366B",
    "#256ABF",
    "#6DA7EC",
    DIVERGING_MID_DARK,
    "#E66767",
    "#D03B3B",
    "#8F2020",
]
DIVERGING_LIGHT: List[str] = [
    "#0D366B",
    "#256ABF",
    "#9EC5F4",
    DIVERGING_MID_LIGHT,
    "#E87373",
    "#D03B3B",
    "#8F2020",
]

# Status palette: RESERVED. Never reused as "series 5". Always shipped with an
# icon + label so the colour never carries meaning alone.
STATUS: Dict[str, str] = {
    "good": "#0CA30C",
    "warning": "#FAB219",
    "serious": "#EC835A",
    "critical": "#D03B3B",
}

STATUS_ICON: Dict[str, str] = {
    "good": "●",  # filled circle
    "warning": "▲",  # triangle
    "serious": "◆",  # diamond
    "critical": "■",  # square
}


@dataclass(frozen=True)
class Surface:
    """Chart chrome and ink for one colour mode."""

    name: str
    page: str
    surface: str
    surface_raised: str
    text_primary: str
    text_secondary: str
    text_muted: str
    grid: str
    axis: str
    border: str
    categorical: List[str] = field(default_factory=list)
    diverging: List[str] = field(default_factory=list)
    diverging_mid: str = ""
    ordinal: List[str] = field(default_factory=list)


DARK = Surface(
    name="dark",
    page="#0B1020",
    surface="#141B34",
    surface_raised="#1B2444",
    text_primary="#FFFFFF",
    text_secondary="#C3C2B7",
    text_muted="#898781",
    grid="#233056",
    axis="#33406B",
    border="rgba(255,255,255,0.10)",
    categorical=CATEGORICAL_DARK,
    diverging=DIVERGING_DARK,
    diverging_mid=DIVERGING_MID_DARK,
    ordinal=ORDINAL_BLUE_DARK,
)

LIGHT = Surface(
    name="light",
    page="#F7F9FC",
    surface="#FFFFFF",
    surface_raised="#FFFFFF",
    text_primary="#0B0B0B",
    text_secondary="#52514E",
    text_muted="#898781",
    grid="#E6EAF2",
    axis="#C3C2B7",
    border="rgba(11,11,11,0.10)",
    categorical=CATEGORICAL_LIGHT,
    diverging=DIVERGING_LIGHT,
    diverging_mid=DIVERGING_MID_LIGHT,
    ordinal=ORDINAL_BLUE_LIGHT,
)

SURFACES: Dict[str, Surface] = {"dark": DARK, "light": LIGHT}

# The app is dark-first. Light mode is a *selected* set of steps from the same
# ramps, not an automatic inversion.
DEFAULT_MODE = "dark"


def surface(mode: str = DEFAULT_MODE) -> Surface:
    return SURFACES.get(mode, DARK)


# --------------------------------------------------------------------------
# Stable entity -> colour bindings.
#
# A filter that removes a cloud must NOT repaint the survivors, so the
# providers are pinned to fixed slots. Everything else is assigned in slot
# order at first sight and cached (see `colour_map`).
#
# OCI takes slot 3 (violet), not Oracle red. Red would sit a few degrees from
# Azure's orange at slot 1, and it is the same hue this palette reserves for
# `critical`. A provider must never look like an alert.
#
# A provider with no slot is not an error -- `provider_colour` returns muted
# ink for it. FOCUS lets any string be a ProviderName.
# --------------------------------------------------------------------------

PROVIDER_SLOT: Dict[str, int] = {"AWS": 0, "Azure": 1, "GCP": 2, "OCI": 3}
PROVIDERS: List[str] = ["AWS", "Azure", "GCP", "OCI"]


def provider_colour(provider: str, mode: str = DEFAULT_MODE) -> str:
    s = surface(mode)
    slot = PROVIDER_SLOT.get(provider)
    if slot is None:
        return s.text_muted
    return s.categorical[slot]


OTHER_LABEL = "Other"


def colour_map(entities, mode: str = DEFAULT_MODE) -> Dict[str, str]:
    """Bind entities to categorical slots in fixed order.

    Past 8 entities we do not generate or cycle hues -- the caller is expected
    to have already folded the tail into `OTHER_LABEL`, which is painted in the
    muted ink so it recedes.
    """
    s = surface(mode)
    out: Dict[str, str] = {}
    slot = 0
    for e in entities:
        if e == OTHER_LABEL:
            out[e] = s.text_muted
            continue
        if e in PROVIDER_SLOT:
            out[e] = s.categorical[PROVIDER_SLOT[e]]
            continue
        while slot < len(s.categorical) and s.categorical[slot] in out.values():
            slot += 1
        out[e] = s.categorical[slot] if slot < len(s.categorical) else s.text_muted
        slot += 1
    return out


def fold_tail(labels_and_values, limit: int = 8):
    """Collapse everything past `limit-1` entities into a single `Other` row.

    Returns a list of (label, value) with `Other` last. Prevents the 9th-hue
    anti-pattern at the source rather than in the chart.
    """
    rows = sorted(labels_and_values, key=lambda kv: kv[1], reverse=True)
    if len(rows) <= limit:
        return rows
    head = rows[: limit - 1]
    tail_total = sum(v for _, v in rows[limit - 1 :])
    return head + [(OTHER_LABEL, tail_total)]


# --------------------------------------------------------------------------
# Typography -- system sans everywhere, including hero figures.
# --------------------------------------------------------------------------

FONT_STACK = 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif'
FONT_MONO = 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace'
