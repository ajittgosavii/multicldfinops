"""Data loading -- the one place Demo Mode and Live Mode diverge.

Below this module nothing knows which mode it is in. `load_context()` returns a
`DataContext` carrying a FOCUS 1.2 frame, and every tab, engine and agent tool
reads that frame identically. Swapping a cloud connector for a procured FinOps
tool is a change to a dict entry in `AppConfig.connector_for`, not a change to
any dashboard.

Caching: Streamlit reruns the whole script on every widget interaction, so both
paths are cached. Demo generation takes ~7 seconds and yields a ~40 MB frame;
live fetches cost real money (AWS Cost Explorer bills roughly $0.01 per
request, CloudZero allows 60 cost requests *per day*). Neither is something to
repeat on a slider drag.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

import focus
from finops_core import AppConfig, DataContext, Mode, SourceInfo

DEFAULT_MONTHS = 24

# Cache TTLs. Demo data is deterministic so it never needs to expire within a
# session; live billing data lands late and is restated, so an hour is right.
_DEMO_TTL = None
_LIVE_TTL = 3600


def _cache_data(func=None, **kwargs):
    """`st.cache_data` when Streamlit is present, a passthrough otherwise.

    The engines and tests import this module without a Streamlit runtime.
    """
    try:
        import streamlit as st

        return st.cache_data(func, **kwargs) if func else st.cache_data(**kwargs)
    except Exception:
        if func:
            return func
        return lambda f: f


# ==========================================================================
# Demo Mode
# ==========================================================================


@_cache_data(ttl=_DEMO_TTL, show_spinner="Generating the demo estate...")
def _load_demo(months: int, seed: int) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    from connectors.demo import build_demo_dataset

    return build_demo_dataset(months=months, seed=seed)


def load_demo_context(cfg: Optional[AppConfig] = None, months: int = DEFAULT_MONTHS, seed: int = 20260708) -> DataContext:
    cfg = cfg or AppConfig(mode=Mode.DEMO)
    df, budgets, drivers = _load_demo(months, seed)
    return DataContext(
        focus_df=df,
        budgets=budgets,
        drivers=drivers,
        mode=Mode.DEMO,
        config=cfg,
        sources=[
            SourceInfo(
                connector="demo",
                cloud=cloud,
                rows=int((df["ProviderName"] == cloud).sum()),
                live=False,
                note="Synthetic FOCUS 1.2 estate",
            )
            for cloud in ("AWS", "Azure", "GCP")
        ],
        validation=focus.validate(df),
    )


# ==========================================================================
# Live Mode
# ==========================================================================


def _secrets_for(prefix: str) -> Dict[str, str]:
    """Collect every secret, then hand the whole map to the connector.

    Connectors read what they need by name. We do not try to guess which
    secrets belong to which connector -- that coupling belongs in the
    connector's `ConnectorSpec.required_secrets`.
    """
    out: Dict[str, str] = {}
    try:
        import streamlit as st

        out.update({k: str(v) for k, v in st.secrets.items() if isinstance(v, (str, int, float))})
    except Exception:
        pass
    import os

    out.update({k: v for k, v in os.environ.items()})
    return out


@_cache_data(ttl=_LIVE_TTL, show_spinner="Fetching live billing data...")
def _fetch_live(
    connector_keys: Tuple[Tuple[str, str], ...],
    start: date,
    end: date,
    _secrets: Dict[str, str],
) -> Tuple[pd.DataFrame, List[dict]]:
    """Pull each configured cloud and concatenate.

    A cloud whose connector is unconfigured or failing does NOT fail the page:
    it contributes zero rows and a source note saying why. A half-wired estate
    should still show the clouds that are wired.
    """
    import connectors as reg

    frames: List[pd.DataFrame] = []
    notes: List[dict] = []

    for cloud, key in connector_keys:
        try:
            conn = reg.get_connector(key, secrets=_secrets)
        except Exception as exc:
            notes.append({"connector": key, "cloud": cloud, "rows": 0, "live": False, "note": f"Load failed: {exc}"})
            continue

        if not conn.configured:
            missing = ", ".join(conn.missing_secrets())
            notes.append(
                {"connector": key, "cloud": cloud, "rows": 0, "live": False, "note": f"Missing secrets: {missing}"}
            )
            continue

        probe = conn.test_connection()
        if not probe.ok:
            notes.append({"connector": key, "cloud": cloud, "rows": 0, "live": False, "note": probe.message})
            continue

        try:
            df = conn.fetch_costs(start, end)
        except Exception as exc:  # a live API failing is expected, not exceptional
            notes.append({"connector": key, "cloud": cloud, "rows": 0, "live": False, "note": f"Fetch failed: {exc}"})
            continue

        if len(df):
            frames.append(df)
        notes.append(
            {
                "connector": key,
                "cloud": cloud,
                "rows": len(df),
                "live": True,
                "note": probe.message,
            }
        )

    if not frames:
        return focus.empty_frame(), notes
    return focus.normalize(pd.concat(frames, ignore_index=True)), notes


def load_live_context(cfg: AppConfig, months: int = DEFAULT_MONTHS) -> DataContext:
    end = date.today()
    start = (end.replace(day=1) - timedelta(days=30 * months)).replace(day=1)

    keys = tuple(sorted(cfg.connector_for.items()))
    df, notes = _fetch_live(keys, start, end, _secrets_for("")) if keys else (focus.empty_frame(), [])

    if len(df):
        df = focus.explode_tags(df)
    else:
        df = focus.explode_tags(focus.empty_frame())

    budgets = _live_budgets(cfg, df)
    drivers = _live_drivers(df)

    return DataContext(
        focus_df=df,
        budgets=budgets,
        drivers=drivers,
        mode=Mode.LIVE,
        config=cfg,
        sources=[SourceInfo(**n) for n in notes],
        validation=focus.validate(df) if len(df) else None,
    )


def _live_budgets(cfg: AppConfig, df: pd.DataFrame) -> pd.DataFrame:
    """Budgets from each cloud's native Budgets API, where it exposes one.

    An empty frame is a legitimate answer -- plenty of enterprises keep budgets
    in a planning system rather than in the cloud console. The Budget tab says
    so instead of inventing a number.
    """
    import connectors as reg

    secrets = _secrets_for("")
    frames: List[pd.DataFrame] = []
    for cloud, key in cfg.connector_for.items():
        try:
            conn = reg.get_connector(key, secrets=secrets)
            if conn.configured and conn.supports(reg.Capability.BUDGETS):
                b = conn.fetch_budgets()
                if len(b):
                    frames.append(b)
        except Exception:
            continue
    if not frames:
        return pd.DataFrame(columns=["period", "cloud", "application", "budget"])
    return pd.concat(frames, ignore_index=True)


def _live_drivers(df: pd.DataFrame) -> pd.DataFrame:
    """Business drivers cannot be read from a cloud bill -- by definition.

    Unit economics needs a denominator from the business (customers served,
    kWh delivered, work orders closed). In Live Mode that must be uploaded or
    wired to a system of record. We return an empty frame and the Unit
    Economics tab explains what to provide.
    """
    return pd.DataFrame(columns=["period", "metric", "value"])


# ==========================================================================
# Entry point
# ==========================================================================


def load_context(cfg: AppConfig, months: int = DEFAULT_MONTHS) -> DataContext:
    if cfg.mode is Mode.LIVE:
        return load_live_context(cfg, months=months)
    return load_demo_context(cfg, months=months)


def upload_focus_context(file, cfg: Optional[AppConfig] = None) -> DataContext:
    """Ingest any FOCUS-conformant export the customer's procured tool emits.

    This is the escape hatch that makes the platform tool-agnostic: CloudZero,
    Vantage and Finout emit FOCUS natively; Cloudability, CloudHealth and
    Flexera ingest it and can round-trip it. Drop the file here and every
    dashboard works.
    """
    import connectors as reg

    cfg = cfg or AppConfig()
    conn = reg.get_connector("focus_file", secrets={}, file=file)
    probe = conn.test_connection()
    df = conn.fetch_costs(date(1970, 1, 1), date.today())
    if len(df):
        df = focus.explode_tags(df)

    return DataContext(
        focus_df=df,
        budgets=pd.DataFrame(columns=["period", "cloud", "application", "budget"]),
        drivers=pd.DataFrame(columns=["period", "metric", "value"]),
        mode=cfg.mode,
        config=cfg,
        sources=[
            SourceInfo(
                connector="focus_file",
                cloud="Uploaded",
                rows=len(df),
                live=False,
                note=probe.message,
            )
        ],
        validation=focus.validate(df) if len(df) else None,
    )
