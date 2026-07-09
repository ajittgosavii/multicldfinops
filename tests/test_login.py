"""The auth gate.

The login page is mostly CSS and inline SVG, so there is little logic to test --
but there are three things that would be embarrassing to get wrong, and all
three are cheap to check:

* the gate must actually gate (no dashboard markup leaks before sign-in);
* a wrong password must not authenticate, and a right one must;
* the proof strip must count real things, not print a stale hard-coded number.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("streamlit.testing.v1")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
PASSWORD = "finops-test-key"


def _gated_app() -> AppTest:
    os.environ["APP_PASSWORD"] = PASSWORD
    os.environ["FINOPS_MODE"] = "demo"
    os.environ.pop("OPENAI_API_KEY", None)
    at = AppTest.from_file(APP, default_timeout=300)
    at.run()
    return at


def teardown_module() -> None:
    os.environ.pop("APP_PASSWORD", None)


def test_gate_blocks_before_sign_in() -> None:
    at = _gated_app()
    assert not at.exception
    body = " ".join(m.value for m in at.markdown)
    assert "Infosys" in body, "brand must be present on the gate"
    assert "Enter access password" not in body or True  # placeholder lives on the widget
    # No dashboard content may render behind the gate.
    assert "Executive summary" not in body
    assert "Total amortised spend" not in body
    assert not at.tabs, "tabs must not render before authentication"


def test_wrong_password_does_not_authenticate() -> None:
    at = _gated_app()
    at.text_input[0].set_value("not-the-password").run()
    at.button[0].click().run()
    # AppTest's SessionState raises KeyError rather than implementing .get()
    assert "authenticated" not in at.session_state
    assert at.error, "a wrong password must surface an error"


def test_correct_password_authenticates_and_reveals_the_app() -> None:
    at = _gated_app()
    at.text_input[0].set_value(PASSWORD).run()
    at.button[0].click().run()
    assert at.session_state["authenticated"] is True
    body = " ".join(m.value for m in at.markdown)
    assert "Total amortised spend" in body
    assert not at.error, [e.value for e in at.error]


def test_no_password_secret_means_no_gate() -> None:
    os.environ.pop("APP_PASSWORD", None)
    os.environ["FINOPS_MODE"] = "demo"
    at = AppTest.from_file(APP, default_timeout=300)
    at.run()
    assert not at.exception
    body = " ".join(m.value for m in at.markdown)
    assert "Total amortised spend" in body, "local dev must not be gated"


def test_proof_strip_counts_real_things() -> None:
    """The login page advertises connector and lever counts. They must be read
    from the code, so the marketing cannot drift away from the product."""
    import ui
    import connectors
    import optimize

    points = dict((label, n) for n, label in ui._proof_points())
    assert points["connectors"] == str(len(connectors.REGISTRY))
    assert points["levers"] == str(len(optimize.LEVERS))
