"""End-to-end render test.

A 200 from the Streamlit server proves only that the web server started -- the
HTML shell is served before the script runs. `AppTest` actually executes
`app.py`, and because Streamlit evaluates the body of every `st.tabs` child on
each script run, one pass exercises all twelve tabs against the demo estate.

`app.py` deliberately catches per-tab exceptions and renders them with
`st.error` so one broken panel cannot take down the whole dashboard. That means
a crash is invisible to a naive "did it raise?" check. So we assert on the
absence of error elements, which is the thing that actually matters.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("streamlit.testing.v1")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")

# Generating the estate plus a 24-month forecast, the lever detectors and the
# anomaly pass is genuinely slow the first time; the cache is cold in a test.
TIMEOUT = 300


@pytest.fixture(scope="module")
def app() -> AppTest:
    os.environ.pop("OPENAI_API_KEY", None)  # prove the Copilot tab degrades gracefully
    os.environ.pop("APP_PASSWORD", None)  # no login gate
    os.environ["FINOPS_MODE"] = "demo"
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    at.run()
    return at


def test_app_runs_without_uncaught_exception(app: AppTest) -> None:
    assert not app.exception, [str(e.value) for e in app.exception]


def test_no_tab_rendered_an_error(app: AppTest) -> None:
    """Every tab renders. `app.py` converts a tab crash into `st.error`."""
    messages = [e.value for e in app.error]
    assert not messages, "tab(s) failed to render:\n" + "\n".join(messages)


def test_all_tabs_present(app: AppTest) -> None:
    import app as app_module

    assert len(app_module.TABS) == 12
    # Every tab module must import and expose render().
    import importlib

    for _name, path, _desc in app_module.TABS:
        mod = importlib.import_module(path)
        assert callable(getattr(mod, "render", None)), f"{path} has no render()"


def test_executive_kpis_rendered(app: AppTest) -> None:
    """The masthead and hero tile made it into the DOM."""
    body = " ".join(m.value for m in app.markdown)
    assert "Multi-Cloud FinOps Command Center" in body
    assert "Total amortised spend" in body


def test_demo_mode_is_the_default(app: AppTest) -> None:
    body = " ".join(m.value for m in app.markdown)
    assert "DEMO" in body
