"""Web app smoke tests with Streamlit's headless AppTest (no browser, no network, no AI calls)."""

from pathlib import Path

import pytest

st_testing = pytest.importorskip("streamlit.testing.v1")
APP = str(Path(__file__).resolve().parents[1] / "streamlit_app.py")
HAS_DATA = (Path(APP).parent / "data" / "app" / "data.json.gz").exists()
pytestmark = pytest.mark.skipif(not HAS_DATA, reason="no exported app data yet")


def app(monkeypatch, password="test-pass"):
    monkeypatch.setenv("APP_PASSWORD", password)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return st_testing.AppTest.from_file(APP, default_timeout=60)


def login(at):
    at.run()
    at.text_input[0].input("test-pass")
    at.button[0].click().run()
    return at


def test_locked_without_password_secret(monkeypatch):
    at = st_testing.AppTest.from_file(APP, default_timeout=60)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    at.run()
    assert "locked" in at.error[0].value


def test_wrong_password_is_rejected(monkeypatch):
    at = app(monkeypatch)
    at.run()
    at.text_input[0].input("nope")
    at.button[0].click().run()
    assert at.error[0].value == "Wrong password."
    assert not at.tabs


def test_dashboard_renders_after_login(monkeypatch):
    at = login(app(monkeypatch))
    assert not at.exception
    assert at.title[0].value == "AGL Release Health"
    assert [t.label for t in at.tabs] == ["Weekly status", "Bug review", "Escalations", "Trends", "Download"]
    assert len(at.metric) == 6


def test_default_settings_reuse_weekly_ai_draft(monkeypatch):
    at = login(app(monkeypatch))
    assert any("weekly run" in i.value for i in at.info)


def test_changed_settings_recompute_and_offer_ai(monkeypatch):
    at = login(app(monkeypatch))
    at.select_slider(key="s_window").set_value(7).run()
    assert not at.exception
    assert at.metric[1].label == "New (7d)"
    assert any("differ from the weekly run" in w.value for w in at.warning)


SHARE = "s3cret-share-key-0123456789"


def share_app(monkeypatch, with_password=False):
    monkeypatch.setenv("SHARE_KEY", SHARE)
    if with_password:
        monkeypatch.setenv("APP_PASSWORD", "test-pass")
    else:
        monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    return st_testing.AppTest.from_file(APP, default_timeout=60)


def test_share_link_logs_in_and_hides_key(monkeypatch):
    at = share_app(monkeypatch)
    at.query_params["key"] = SHARE
    at.run()
    assert not at.exception
    assert at.title[0].value == "AGL Release Health"
    assert "key" not in at.query_params


def test_wrong_share_key_is_rejected(monkeypatch):
    at = share_app(monkeypatch)
    at.query_params["key"] = "not-the-right-key-at-all"
    at.run()
    assert "isn't valid" in at.error[0].value
    assert not at.tabs


def test_share_only_mode_has_no_password_form(monkeypatch):
    at = share_app(monkeypatch)
    at.run()
    assert not at.text_input and "link you were sent" in at.info[0].value


def test_password_still_works_alongside_share_link(monkeypatch):
    at = share_app(monkeypatch, with_password=True)
    login(at)
    assert at.title[0].value == "AGL Release Health"


def test_short_share_key_is_ignored(monkeypatch):
    monkeypatch.setenv("SHARE_KEY", "short")
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    at = st_testing.AppTest.from_file(APP, default_timeout=60)
    at.query_params["key"] = "short"
    at.run()
    assert "locked" in at.error[0].value
