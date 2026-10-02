"""Tests for config.py: driver self-registration and URL-to-site lookup."""

from __future__ import annotations

import importlib

import pytest

import config


def test_all_expected_sites_are_registered():
    assert set(config.SUPPORTED_SITES) == {"mgread", "nelomanga", "wfwf504", "mangago", "madara", "mangadex", "generic"}


def test_all_drivers_matches_registry_size():
    assert len(config.all_drivers()) == len(config.SUPPORTED_SITES)


def test_site_for_url_matches_by_domain():
    site = config.site_for_url("https://www.nelomanga.net/manga/death-note")
    assert site is not None
    assert site.key == "nelomanga"


def test_site_for_url_returns_none_for_unknown_domain():
    assert config.site_for_url("https://example.com/whatever") is None


def test_classify_url_delegates_to_driver():
    site = config.site_for_url("https://mgread.io/manga/one-piece")
    assert site is not None
    assert config.classify_url("https://mgread.io/manga/one-piece", site) == "list"


def test_registry_problems_go_to_stderr_not_stdout(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """Discovery runs at import time, before `--json` routes the console: stdout must stay clean."""
    real_import = importlib.import_module

    def broken_wfwf(name: str, package: str | None = None):
        if name == "src.sites.wfwf":
            raise ImportError("no module named 'something'")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", broken_wfwf)
    sites = config._discover_drivers()
    assert "wfwf504" not in sites and "mgread" in sites
    out = capsys.readouterr()
    assert out.out == ""
    assert "[registry] failed to load site module src.sites.wfwf" in out.err
