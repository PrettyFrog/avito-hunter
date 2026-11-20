"""Unit tests for the Camoufox launch options (no browser launch involved)."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from hunter.transport import camoufox_setup


def _install_fake_camoufox(monkeypatch, *, pin, installed, found):
    root = types.ModuleType("camoufox")
    root.__path__ = []
    pin_mod = types.ModuleType("camoufox.browser_pin")
    pin_mod.effective_pin = lambda config: pin
    version_mod = types.ModuleType("camoufox.multiversion")
    version_mod.load_config = lambda: {}
    version_mod.find_installed_version = lambda spec: found
    version_mod.list_installed = lambda: installed
    monkeypatch.setitem(sys.modules, "camoufox", root)
    monkeypatch.setitem(sys.modules, "camoufox.browser_pin", pin_mod)
    monkeypatch.setitem(sys.modules, "camoufox.multiversion", version_mod)


def test_options_carry_ru_windows_identity_and_proxy(monkeypatch):
    _install_fake_camoufox(monkeypatch, pin=None, installed=[], found=None)
    proxy = {"server": "http://127.0.0.1:18080"}

    options = camoufox_setup.camoufox_options(headless=False, proxy=proxy)

    assert options["headless"] is False
    assert options["os"] in ("windows", "linux")
    assert options["locale"] == "ru-RU"
    assert options["humanize"] is True
    assert options["geoip"] is False
    assert options["proxy"] == proxy
    assert options["i_know_what_im_doing"] is True
    assert "browser" not in options


def test_paired_build_is_left_to_the_library(monkeypatch):
    pin = SimpleNamespace(spec="official/158.0.1-beta.41")
    _install_fake_camoufox(monkeypatch, pin=pin, installed=[], found="/browsers/158")

    options = camoufox_setup.camoufox_options(headless=True, proxy=None)

    assert "browser" not in options


def test_missing_paired_build_falls_back_to_installed_one(monkeypatch):
    pin = SimpleNamespace(spec="official/158.0.1-beta.41")
    installed = [SimpleNamespace(version=SimpleNamespace(build="beta.36"))]
    _install_fake_camoufox(monkeypatch, pin=pin, installed=installed, found=None)

    options = camoufox_setup.camoufox_options(headless=False, proxy=None)

    assert options["browser"] == "beta.36"


def test_import_failure_leaves_options_untouched(monkeypatch):
    monkeypatch.setitem(sys.modules, "camoufox.browser_pin", None)

    options = camoufox_setup.camoufox_options(headless=False, proxy=None)

    assert "browser" not in options
    assert options["geoip"] is False


@pytest.mark.parametrize("headless", [True, False])
def test_headless_flag_is_passed_through(monkeypatch, headless):
    _install_fake_camoufox(monkeypatch, pin=None, installed=[], found=None)

    options = camoufox_setup.camoufox_options(headless=headless, proxy=None)

    assert options["headless"] is headless
