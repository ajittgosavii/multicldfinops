"""A provider the code has never met must not break the platform.

FOCUS leaves `ProviderName` a free string on purpose: the specification has to
survive a cloud its authors have not met. `focus_file` therefore accepts a
conformant export from anyone -- OCI, IBM, Alibaba, a private cloud, a procured
tool that re-exports somebody else's bill.

`optimize.py` used to index five dicts keyed by ProviderName directly, so the
first such export raised `KeyError('OCI')` from deep inside a detector. The
whole Optimize tab died on a file that was perfectly valid.

The contract now: a cloud we have a profile for gets its rate levers; a cloud we
do not still gets every provider-agnostic finding (idle resources, unused
commitment, untagged spend), because those need no profile. Nothing raises.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

import focus
import optimize
from connectors import REGISTRY
from connectors.demo import COMMITMENT_DISCOUNT, SPOT_DISCOUNT, _share
from finops_core import CLOUDS, AppConfig


def _theme():
    """`theme` is the Streamlit app's; the extracted `finops-core` package has no
    UI layer. This file is copied into both, so the colour assertions skip there
    rather than fail."""
    return pytest.importorskip("theme")


@pytest.fixture(scope="module")
def estate() -> pd.DataFrame:
    from connectors.demo import DemoConnector

    return focus.explode_tags(DemoConnector().fetch_costs(date(2024, 8, 1), date(2026, 7, 1)))


# ---- OCI is a first-class provider ---------------------------------------


def test_oci_is_in_every_canonical_list() -> None:
    assert "OCI" in CLOUDS
    assert "oci_native" in REGISTRY
    assert AppConfig().connector_for["OCI"] == "oci_native"


def test_oci_is_in_the_ui_palette() -> None:
    theme = _theme()
    assert "OCI" in theme.PROVIDERS
    assert "OCI" in theme.PROVIDER_SLOT


def test_oci_gets_its_own_colour_not_the_muted_fallback() -> None:
    theme = _theme()
    for mode in ("dark", "light"):
        colour = theme.provider_colour("OCI", mode)
        assert colour != theme.surface(mode).text_muted
        # ...and it must not collide with another provider.
        others = {theme.provider_colour(p, mode) for p in ("AWS", "Azure", "GCP")}
        assert colour not in others


def test_the_demo_estate_bills_oci(estate: pd.DataFrame) -> None:
    assert "OCI" in set(estate["ProviderName"])
    oci = estate[estate["ProviderName"] == "OCI"]
    assert oci["EffectiveCost"].sum() > 0
    # Compartments, not projects: OCI's account model is its own. Only usage
    # rows carry a sub-account -- tax, credits and commitment purchases are
    # levied on the payer and have none, in every cloud.
    usage = oci[oci["ChargeCategory"] == "Usage"]
    assert set(usage["SubAccountType"].dropna()) == {"Compartment"}


def test_oci_levers_fire_on_the_demo_estate(estate: pd.DataFrame) -> None:
    got = {o.lever_id for o in optimize.detect_all(estate) if o.cloud == "OCI"}
    # R17 Universal Credits, R18 Ampere, R20 BYOL to OCI, U21 storage tiering.
    assert {"R17", "R18", "R20", "U21"} <= got, f"missing OCI levers: {got}"


def test_every_lever_id_an_opportunity_cites_actually_exists(estate: pd.DataFrame) -> None:
    catalog = {lever.id for lever in optimize.LEVERS}
    for opp in optimize.detect_all(estate):
        assert opp.lever_id in catalog, f"{opp.lever_id} is not in the catalog"


# ---- the regression: an unknown provider ---------------------------------


def _relabel(df: pd.DataFrame, provider: str) -> pd.DataFrame:
    """A valid FOCUS frame whose ProviderName we have never seen."""
    alien = df[df["ProviderName"] == "GCP"].copy()
    alien["ProviderName"] = provider
    return pd.concat([df, alien], ignore_index=True)


@pytest.mark.parametrize("provider", ["IBM Cloud", "Alibaba Cloud", "OVHcloud", "Nutanix"])
def test_an_unknown_provider_does_not_crash_the_optimizer(estate: pd.DataFrame, provider: str) -> None:
    mixed = _relabel(estate, provider)
    assert focus.validate(mixed).ok, "the alien frame must itself be valid FOCUS"
    optimize.detect_all(mixed)  # must not raise -- this is the whole test


def test_an_unknown_provider_still_yields_provider_agnostic_findings(estate: pd.DataFrame) -> None:
    opps = optimize.detect_all(_relabel(estate, "IBM Cloud"))
    alien = [o for o in opps if o.cloud == "IBM Cloud"]
    assert alien, "an unknown cloud should still surface waste and idle resources"
    # ...but only from levers that claim every cloud. A lever scoped to specific
    # providers (an AWS Savings Plan, BYOL to OCI) must never be recommended for
    # a cloud whose commitment instrument we cannot even name.
    universal = {lever.id for lever in optimize.LEVERS if lever.clouds == optimize.ALL}
    cited = {o.lever_id for o in alien}
    assert cited <= universal, f"cloud-specific levers recommended for an unknown cloud: {cited - universal}"


def test_unknown_provider_gets_muted_ink_rather_than_a_wrong_colour() -> None:
    theme = _theme()
    assert theme.provider_colour("IBM Cloud") == theme.surface().text_muted


# ---- the demo and the optimizer must agree -------------------------------


def test_demo_discounts_match_the_optimizer_profiles() -> None:
    """If these drift, the demo's savings estimates stop reconciling with the
    demo's own bill, and every number on the Optimize tab is quietly wrong."""
    for cloud, profile in optimize._PROFILES.items():
        assert profile.commitment_rate == COMMITMENT_DISCOUNT[cloud], cloud
        assert profile.spot_discount == SPOT_DISCOUNT[cloud], cloud


def test_every_profiled_cloud_names_levers_that_exist() -> None:
    catalog = {lever.id for lever in optimize.LEVERS}
    for cloud, profile in optimize._PROFILES.items():
        assert profile.commitment_lever in catalog, cloud
        for lever_id in (profile.arm_lever, profile.tier_lever):
            assert lever_id is None or lever_id in catalog, cloud


def test_app_shares_sum_to_one() -> None:
    """`App.monthly_base` must mean the app's base, not 1.32x it."""
    for clouds in [("AWS",), ("AWS", "OCI"), ("AWS", "Azure", "GCP"), ("AWS", "Azure", "GCP", "OCI")]:
        total = sum(_share(clouds, c) for c in clouds)
        assert total == pytest.approx(1.0), clouds
