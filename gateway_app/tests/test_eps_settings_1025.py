"""#1025 — EPS_* is read first, IPS_* is the deprecated fallback, no default."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services import ips_client as ips_mod
from app.services.ips_client import IpsClient, eps_env, eps_setting

_NAMES = ("EPS_BASE_URL", "IPS_BASE_URL", "EPS_API_KEY", "IPS_API_KEY")


@pytest.fixture
def env(monkeypatch):
    for n in _NAMES:
        monkeypatch.delenv(n, raising=False)
    return monkeypatch


@pytest.fixture
def no_config(app):
    """The app's own IPS_* config emptied, so only the environment can answer."""
    prev = {k: app.config.get(k) for k in ("IPS_BASE_URL", "IPS_API_KEY")}
    app.config["IPS_BASE_URL"] = None
    app.config["IPS_API_KEY"] = None
    yield app
    app.config.update(prev)


@pytest.mark.parametrize("suffix", ["BASE_URL", "API_KEY"])
def test_new_name_wins(env, suffix):
    env.setenv(f"EPS_{suffix}", "new")
    env.setenv(f"IPS_{suffix}", "old")
    assert eps_env(suffix) == "new"


@pytest.mark.parametrize("suffix", ["BASE_URL", "API_KEY"])
def test_old_name_still_works(env, suffix):
    env.setenv(f"IPS_{suffix}", "old")
    assert eps_env(suffix) == "old"


@pytest.mark.parametrize("suffix", ["BASE_URL", "API_KEY"])
def test_an_empty_new_name_falls_back_to_the_old(env, suffix):
    env.setenv(f"EPS_{suffix}", "")
    env.setenv(f"IPS_{suffix}", "old")
    assert eps_env(suffix) == "old"


@pytest.mark.parametrize("suffix", ["BASE_URL", "API_KEY"])
def test_both_unset_is_unset_with_no_invented_default(env, suffix):
    assert eps_env(suffix) == ""


def test_constructor_takes_the_new_names(env):
    env.setenv("EPS_BASE_URL", "https://eps.example/")
    env.setenv("IPS_BASE_URL", "https://ips.example")
    env.setenv("EPS_API_KEY", "k-new")
    env.setenv("IPS_API_KEY", "k-old")
    c = IpsClient()
    assert c.base_url == "https://eps.example"
    assert c.api_key == "k-new"


def test_constructor_still_takes_the_old_names_alone(env):
    env.setenv("IPS_BASE_URL", "https://ips.example")
    env.setenv("IPS_API_KEY", "k-old")
    c = IpsClient()
    assert c.base_url == "https://ips.example"
    assert c.api_key == "k-old"


def test_constructor_with_nothing_set_fetches_nothing(env):
    c = IpsClient()
    assert c.base_url == "" and c.api_key is None
    with patch.object(ips_mod.requests, "get", side_effect=AssertionError("called")):
        assert c.fetch_active_blocks("p1") == []


def test_default_client_uses_the_new_names(env, no_config):
    env.setenv("EPS_BASE_URL", "https://eps.example")
    env.setenv("IPS_BASE_URL", "https://ips.example")
    env.setenv("EPS_API_KEY", "k-new")
    env.setenv("IPS_API_KEY", "k-old")
    with no_config.app_context():
        c = ips_mod._default_client()
    assert c.base_url == "https://eps.example"
    assert c._headers()["Authorization"] == "ApiKey k-new"


def test_default_client_still_uses_the_old_names_alone(env, no_config):
    env.setenv("IPS_BASE_URL", "https://ips.example")
    env.setenv("IPS_API_KEY", "k-old")
    with no_config.app_context():
        c = ips_mod._default_client()
    assert c.base_url == "https://ips.example"
    assert c._headers()["Authorization"] == "ApiKey k-old"


def test_default_client_with_nothing_set_is_unconfigured(env, no_config):
    with no_config.app_context():
        c = ips_mod._default_client()
    assert c.base_url == ""
    assert "Authorization" not in c._headers()


def test_eps_setting_prefers_the_app_config_then_the_environment(env):
    env.setenv("EPS_API_KEY", "k-env")
    assert eps_setting("API_KEY", {"IPS_API_KEY": "k-cfg"}) == "k-cfg"
    assert eps_setting("API_KEY", {}) == "k-env"
