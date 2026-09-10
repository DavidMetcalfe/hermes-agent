"""Dashboard plugin discovery/state follows the selected management profile (#46408).

The dashboard is a machine-level management surface: one header switcher decides
which profile the pages read/write. Plugin discovery and the enabled/disabled +
visibility state must follow that selected profile rather than the dashboard
process's own profile.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def _write_plugin(home: Path, name: str) -> Path:
    """Create a minimal user dashboard plugin under ``<home>/plugins/<name>``."""
    dashboard_dir = home / "plugins" / name / "dashboard"
    dashboard_dir.mkdir(parents=True)
    dist_dir = dashboard_dir / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.js").write_text("console.log('x');")
    (dashboard_dir / "manifest.json").write_text(json.dumps({
        "name": name,
        "label": name.title(),
        "entry": "dist/index.js",
    }))
    return dashboard_dir


def test_search_dirs_include_request_profile_home(tmp_path, monkeypatch):
    """A plugin installed into the selected profile's plugins dir must be
    discoverable while a request is scoped to that profile — otherwise a
    profile-scoped install is orphaned from the UI (issue #46408)."""
    from hermes_cli import web_server_dashboard as wsd

    root = tmp_path / "hermes"
    root.mkdir()
    profile = root / "profiles" / "worker"
    (profile / "plugins").mkdir(parents=True)

    monkeypatch.setenv("HERMES_HOME", str(root))
    token = set_hermes_home_override(str(profile))
    try:
        roots = {str(d) for d, _src in wsd._dashboard_plugin_search_dirs()}
    finally:
        reset_hermes_home_override(token)

    assert str(profile / "plugins") in roots


def test_discovery_and_cache_are_profile_scoped(tmp_path, monkeypatch):
    """Discovery under a profile scope returns that profile's local plugins,
    and the per-process cache must not leak one profile's result into another."""
    from hermes_cli import web_server

    root = tmp_path / "hermes"
    root.mkdir()
    worker = root / "profiles" / "worker"
    worker.mkdir(parents=True)
    _write_plugin(worker, "worker-only")

    monkeypatch.setenv("HERMES_HOME", str(root))

    token = set_hermes_home_override(str(worker))
    try:
        scoped = {p["name"] for p in web_server._get_dashboard_plugins()}
    finally:
        reset_hermes_home_override(token)
    # Same-process, unscoped: the process home has no plugins/ of its own.
    unscoped = {p["name"] for p in web_server._get_dashboard_plugins()}

    assert "worker-only" in scoped
    assert "worker-only" not in unscoped


@pytest.mark.asyncio
async def test_profile_only_plugin_asset_resolves_under_profile_scope(tmp_path, monkeypatch):
    """End-to-end: a plugin present only in the selected profile's dir is both discovered
    AND served by the profile-scoped static-asset route — the "carry the fix through asset
    resolution" ask (asset URLs + server resolution carry ``?profile=``)."""
    from hermes_cli.web_routers import dashboard_ui

    root = tmp_path / "hermes"
    root.mkdir()
    profile = root / "profiles" / "worker"
    _write_plugin(profile, "worker-only")
    (profile / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["worker-only"]}})
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    resp = await dashboard_ui.serve_plugin_asset("worker-only", "dist/index.js", profile="worker")
    assert resp.status_code == 200
    assert str(resp.path).endswith("dist/index.js")

    # Unscoped, the profile-only plugin is not discoverable, so its asset 404s.
    with pytest.raises(HTTPException) as exc:
        await dashboard_ui.serve_plugin_asset("worker-only", "dist/index.js")
    assert exc.value.status_code == 404
