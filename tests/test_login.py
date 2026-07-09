"""The front door.

The sign-in page is the app's entry point, not a conditional guard: there is
always a password, so the page always renders. What matters, and is cheap to
check:

* the door must actually be shut (no dashboard markup leaks before sign-in);
* a wrong key must not open it, a right one must;
* an `APP_PASSWORD` secret must override the demo key;
* the page must never let an operator believe the demo key is a security
  boundary -- it ships in the repository;
* the proof strip must count real things, not print a stale hard-coded number.
"""

from __future__ import annotations

import os
from typing import Optional

import pytest

pytest.importorskip("streamlit.testing.v1")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
DEMO_KEY = "cldfinops"


def _app(password: Optional[str] = None) -> AppTest:
    os.environ.pop("APP_PASSWORD", None)
    if password:
        os.environ["APP_PASSWORD"] = password
    os.environ["FINOPS_MODE"] = "demo"
    os.environ.pop("OPENAI_API_KEY", None)
    at = AppTest.from_file(APP, default_timeout=300)
    at.run()
    return at


def teardown_function() -> None:
    os.environ.pop("APP_PASSWORD", None)


def _body(at: AppTest) -> str:
    return " ".join(m.value for m in at.markdown)


def test_the_gate_always_renders_and_hides_the_dashboard() -> None:
    at = _app()
    assert not at.exception
    body = _body(at)
    assert "Infosys" in body
    assert "mf-hero-svg" in body, "the animated hero must render"
    assert "Total amortised spend" not in body
    assert not at.tabs, "tabs must not render before sign-in"


def test_wrong_key_does_not_open_the_door() -> None:
    at = _app()
    at.text_input[0].set_value("not-the-key").run()
    at.button[0].click().run()
    # AppTest's SessionState raises KeyError rather than implementing .get()
    assert "authenticated" not in at.session_state
    assert at.error, "a wrong key must surface an error"
    assert "Total amortised spend" not in _body(at)


def test_demo_key_opens_the_door() -> None:
    at = _app()
    at.text_input[0].set_value(DEMO_KEY).run()
    at.button[0].click().run()
    assert at.session_state["authenticated"] is True
    assert "Total amortised spend" in _body(at)
    assert not at.error, [e.value for e in at.error]


def test_app_password_secret_overrides_the_demo_key() -> None:
    # The shipped demo key must stop working the moment a real one is configured.
    at = _app(password="a-real-secret")
    at.text_input[0].set_value(DEMO_KEY).run()
    at.button[0].click().run()
    assert "authenticated" not in at.session_state

    at = _app(password="a-real-secret")
    at.text_input[0].set_value("a-real-secret").run()
    at.button[0].click().run()
    assert at.session_state["authenticated"] is True


def test_demo_key_is_never_presented_as_security() -> None:
    """It ships in a public repository. The page says so on its face."""
    at = _app()
    captions = " ".join(c.value for c in at.caption)
    assert DEMO_KEY in captions
    assert "gates nothing real" in captions
    assert "APP_PASSWORD" in captions

    # With a real secret configured, that disclaimer must disappear.
    at = _app(password="a-real-secret")
    captions = " ".join(c.value for c in at.caption)
    assert DEMO_KEY not in captions


def test_password_is_default_tracks_the_secret() -> None:
    import ui

    os.environ.pop("APP_PASSWORD", None)
    assert ui.password_is_default() is True
    os.environ["APP_PASSWORD"] = "x"
    assert ui.password_is_default() is False
    os.environ.pop("APP_PASSWORD", None)


def test_proof_strip_counts_real_things() -> None:
    """The page advertises connector and lever counts. They are read from the
    code, so the marketing cannot drift away from the product."""
    import connectors
    import optimize
    import ui

    points = dict((label, n) for n, label in ui._proof_points())
    assert points["connectors"] == str(len(connectors.REGISTRY))
    assert points["levers"] == str(len(optimize.LEVERS))


def test_hero_and_mark_are_single_line() -> None:
    """Both are interpolated into indented HTML blocks. A newline would let
    markdown render them as a code fence."""
    import brand

    assert "\n" not in brand.hero_svg()
    assert "\n" not in brand.mark_svg(42)
