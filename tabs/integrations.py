"""The Integrations tab -- the connector control plane.

This tab is what proves the platform's central claim: that it plugs into
whatever FinOps tool the client has already procured. The architecture that
makes the claim true is boring on purpose -- every connector normalises to
FOCUS on the way in, so adopting a new tool is one `Connector` subclass and
nothing downstream changes. This page makes that visible: the live sources, the
full catalog of what could be wired, a tester that probes a connector without
ever echoing a secret, and the zero-code FOCUS upload path.

The rate-limit table is not decoration. Every figure in it is a real operational
constraint pulled from vendor API research -- the things that turn a naive
integration into a 429 storm or a $-per-request surprise. Rendered verbatim so
whoever wires the connector sees the trap before they hit it.

This tab renders provenance and metadata; it computes no KPI.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import connectors as reg
import data
import focus
import ui
from finops_core import DataContext

# native sorts first: a tool that emits FOCUS natively is the least work to adopt.
_FOCUS_ORDER = {"native": 0, "ingest": 1, "map": 2, "none": 3}


def render(ctx: DataContext) -> None:
    ui.section(
        "Connector control plane",
        f"{ctx.config.organisation} · everything normalises to FOCUS "
        f"{focus.FOCUS_CANONICAL_VERSION} on ingest.",
    )
    ui.callout(
        "Every source -- a hyperscaler billing export, a commercial FinOps "
        "platform, or a dropped CSV -- returns a **FOCUS "
        f"{focus.FOCUS_CANONICAL_VERSION} conformant frame**. Adopting a new tool "
        "is **one `Connector` subclass**; every dashboard, KPI, forecast and agent "
        "tool keeps working, because none of them has ever seen a vendor field."
    )

    # ---------------------------------------------------------------
    # Current sources
    # ---------------------------------------------------------------
    st.divider()
    ui.section("Current data sources", "What is feeding the dashboards right now.")
    _current_sources(ctx)

    # ---------------------------------------------------------------
    # Connector catalog
    # ---------------------------------------------------------------
    st.divider()
    ui.section(
        "Connector catalog",
        "Everything the platform can wire, grouped so FOCUS-native tools sort first.",
    )
    specs = _load_specs()
    _catalog(specs)

    # ---------------------------------------------------------------
    # Connection tester
    # ---------------------------------------------------------------
    st.divider()
    ui.section(
        "Connection tester",
        "Probe a connector without ever displaying a secret value.",
    )
    _tester(ctx, specs)

    # ---------------------------------------------------------------
    # FOCUS upload
    # ---------------------------------------------------------------
    st.divider()
    ui.section(
        "FOCUS upload -- the zero-code path",
        "Any tool that exports FOCUS drops a file here and every dashboard works. "
        "No connector required.",
    )
    _uploader(ctx)

    # ---------------------------------------------------------------
    # Rate limits & gotchas
    # ---------------------------------------------------------------
    st.divider()
    ui.section(
        "Rate limits and gotchas",
        "Real operational constraints from vendor API research. Read before wiring.",
    )
    _rate_limits()


# ==========================================================================
# Panels
# ==========================================================================


@st.cache_data(show_spinner=False)
def _load_specs_records():
    """Static connector metadata as plain records -- cache-safe (no live objects)."""
    rows = []
    for s in reg.specs():
        rows.append(
            {
                "key": s.key,
                "display_name": s.display_name,
                "vendor": s.vendor,
                "clouds": list(s.clouds),
                "auth": s.auth.value,
                "capabilities": [c.value for c in s.capabilities],
                "focus_support": s.focus_support,
                "required_secrets": list(s.required_secrets),
                "base_url": s.base_url,
                "docs_url": s.docs_url,
                "notes": s.notes,
            }
        )
    return rows


def _load_specs():
    try:
        return _load_specs_records()
    except Exception as exc:
        st.error(f"Could not load connector specs: `{exc}`")
        return []


def _current_sources(ctx: DataContext) -> None:
    if not ctx.sources:
        ui.callout("No sources registered for this context.")
        return

    cols = st.columns(min(len(ctx.sources), 3))
    for i, src in enumerate(ctx.sources):
        with cols[i % len(cols)]:
            pill = ui.status_pill("good", "LIVE") if src.live else ui.pill("DEMO")
            st.markdown(
                f'<div class="mf-tile">'
                f'<div class="mf-label">{src.cloud}</div>'
                f'<div class="mf-value" style="font-size:1.1rem">{src.connector}</div>'
                f'<div class="mf-sub">{src.rows:,} rows · FOCUS {src.focus_version} {pill}</div>'
                f'<div class="mf-sub">{src.note}</div>'
                f"</div>",
                unsafe_allow_html=True,
            )

    frame = pd.DataFrame(
        [
            {
                "Connector": s.connector,
                "Cloud": s.cloud,
                "Rows": s.rows,
                "Mode": "Live" if s.live else "Demo",
                "FOCUS": s.focus_version,
                "Note": s.note,
            }
            for s in ctx.sources
        ]
    )
    ui.table_view(frame, key="int_sources", label="Sources table view")


def _catalog(specs) -> None:
    if not specs:
        return

    clouds = sorted({c for s in specs for c in s["clouds"]})
    caps = sorted({c for s in specs for c in s["capabilities"]})
    focus_levels = ["native", "ingest", "map", "none"]

    f1, f2, f3 = st.columns(3)
    with f1:
        sel_cloud = st.multiselect("Cloud", clouds, default=[], key="int_f_cloud",
                                   placeholder="All clouds")
    with f2:
        sel_cap = st.multiselect("Capability", caps, default=[], key="int_f_cap",
                                 placeholder="All capabilities")
    with f3:
        sel_focus = st.multiselect("FOCUS support", focus_levels, default=[], key="int_f_focus",
                                   placeholder="All levels")

    rows = []
    for s in specs:
        if sel_cloud and not (set(sel_cloud) & set(s["clouds"])):
            continue
        if sel_cap and not (set(sel_cap) & set(s["capabilities"])):
            continue
        if sel_focus and s["focus_support"] not in sel_focus:
            continue
        rows.append(s)

    rows.sort(key=lambda s: (_FOCUS_ORDER.get(s["focus_support"], 9), s["display_name"]))

    if not rows:
        ui.callout("No connectors match those filters.")
        return

    frame = pd.DataFrame(
        [
            {
                "Connector": s["display_name"],
                "Key": s["key"],
                "Vendor": s["vendor"],
                "Clouds": ", ".join(s["clouds"]),
                "Auth": s["auth"],
                "Capabilities": ", ".join(s["capabilities"]),
                "FOCUS": s["focus_support"],
                "Required secrets": ", ".join(s["required_secrets"]) or "—",
                "Base URL": s["base_url"],
                "Docs": s["docs_url"],
                "Notes": s["notes"],
            }
            for s in rows
        ]
    )
    st.dataframe(
        frame,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Docs": st.column_config.LinkColumn("Docs", display_text="docs"),
            "Base URL": st.column_config.LinkColumn("Base URL", display_text="endpoint"),
        },
    )
    st.caption(
        f"{len(rows)} connector(s). FOCUS support: **native** emits FOCUS directly, "
        "**ingest** round-trips it, **map** requires a field mapping, **none** has no FOCUS path."
    )


def _tester(ctx: DataContext, specs) -> None:
    if not specs:
        return
    by_key = {s["key"]: s for s in specs}
    keys = sorted(by_key)

    chosen = st.selectbox(
        "Connector",
        keys,
        format_func=lambda k: by_key[k]["display_name"],
        key="int_test_key",
    )
    spec = by_key[chosen]

    try:
        secrets = _secrets_map()
        conn = reg.get_connector(chosen, secrets=secrets)
        missing = conn.missing_secrets()
    except Exception as exc:
        st.error(f"Could not instantiate connector: `{exc}`")
        return

    c1, c2 = st.columns([1, 1])
    with c1:
        if missing:
            st.markdown(
                f"{ui.status_pill('warning', 'Missing secrets')} &nbsp; "
                + ", ".join(f"`{m}`" for m in missing),
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                ui.status_pill("good", "Credentials present"), unsafe_allow_html=True
            )
        st.caption("Secret *values* are never displayed -- only whether they are set.")
    with c2:
        run = st.button("Test connection", type="primary", key="int_test_run")

    if run:
        try:
            res = conn.test_connection()
        except Exception as exc:  # a connector must not raise, but never trust it
            st.error(f"Tester raised (a connector should return ok=False instead): `{exc}`")
            return

        status = "good" if res.ok else "critical"
        lat = f"{res.latency_ms:.0f} ms" if res.latency_ms is not None else "—"
        st.markdown(
            f"{ui.status_pill(status, 'OK' if res.ok else 'Failed')} &nbsp; "
            f"{res.message} &nbsp; · &nbsp; latency {lat}",
            unsafe_allow_html=True,
        )
        detail = {k: v for k, v in (res.detail or {}).items()}
        if detail:
            with st.expander("Detail", expanded=False):
                st.json(detail)


def _uploader(ctx: DataContext) -> None:
    up = st.file_uploader(
        "FOCUS export (CSV or Parquet)",
        type=["csv", "parquet"],
        key="int_focus_upload",
    )
    if up is None:
        st.caption(
            "CloudZero, Vantage and Finout emit FOCUS natively; Cloudability, "
            "CloudHealth and Flexera can round-trip it. Any of those exports lands here."
        )
        return

    try:
        uploaded = data.upload_focus_context(up, cfg=ctx.config)
    except Exception as exc:
        st.error(f"Could not ingest the file: `{exc}`")
        return

    v = uploaded.validation
    if v is None:
        ui.callout("The file produced no rows -- nothing to validate.")
        return

    status = "good" if v.ok and not v.warnings else "warning" if v.ok else "critical"
    st.markdown(
        f"{ui.status_pill(status, 'Conformant' if v.ok else 'Non-conformant')} &nbsp; "
        f"{v.summary()}",
        unsafe_allow_html=True,
    )
    if v.errors:
        with st.expander(f"{len(v.errors)} error(s)", expanded=True):
            for e in v.errors:
                st.markdown(f"- {e}")
    if v.warnings:
        with st.expander(f"{len(v.warnings)} warning(s)", expanded=False):
            for w in v.warnings:
                st.markdown(f"- {w}")

    if not uploaded.focus_df.empty:
        st.caption(f"Preview -- first rows of {len(uploaded.focus_df):,} ingested.")
        st.dataframe(_arrow_safe(uploaded.focus_df.head(15)), use_container_width=True, hide_index=True)


def _arrow_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Stringify dict/list cells before Streamlit hands the frame to Arrow.

    FOCUS models `Tags` (and 1.3's `AllocatedTags` / `SkuPriceDetails`) as a
    map, so those land in pandas as a dict per cell. Arrow copes with a dict
    column when the value types are consistent, but this preview renders a file
    an arbitrary vendor exported: one row with a list-valued tag and the next
    with a scalar is enough for `ArrowInvalid: cannot mix list and non-list`.
    A preview only needs the value readable, so we stringify rather than gamble.
    """
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == "object" and out[col].map(lambda v: isinstance(v, (dict, list))).any():
            out[col] = out[col].astype(str)
    return out


def _rate_limits() -> None:
    rows = [
        ("CloudZero", "60 cost requests per DAY; 10,000-record pages; 30s timeout."),
        ("AWS Cost Explorer", "~$0.01 per request; max 2 GroupBy per query; global endpoint, sign us-east-1."),
        ("CloudHealth", "REST GET URI capped at 4,000 chars; GraphQL accessToken lives 15 minutes, refreshToken 9 hours."),
        ("Finout", "Query window <=60 days (v2) / 90 days (v1); v2 field `unixTimeMillisecondsStart` vs v1 `unixTimeMillSecondsStart`; generate-query 50/min, 10,000/day."),
        ("Cloudability", "Enqueue endpoint limited to 20 requests per user -> 429; cost-report pagination via `token` above 10,000 rows."),
        ("Flexera", "1000 RPM per org, 500 RPM per principal; 429 carries `retry-after`."),
        ("Azure", "429 + `Retry-After`; follow `nextLink`."),
        ("Turbonomic", "Not a billing source -- recommendations only."),
        ("ServiceNow", "CCM cost table names are not publicly documented; enumerate `sys_db_object` in the customer instance."),
    ]
    frame = pd.DataFrame(rows, columns=["Vendor", "Limit / gotcha"])
    st.dataframe(frame, use_container_width=True, hide_index=True)
    ui.table_view(frame, key="int_rate_limits", label="Rate limits table view")


def _secrets_map() -> dict:
    """Best-effort read of secrets + environment, handed to the connector to
    resolve by name. This tab never renders any value it collects here."""
    out: dict = {}
    try:
        import streamlit as _st

        out.update({k: str(v) for k, v in _st.secrets.items() if isinstance(v, (str, int, float))})
    except Exception:
        pass
    import os

    out.update(dict(os.environ))
    return out
