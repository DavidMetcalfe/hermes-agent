"""discover_plugins() must not load another profile's plugins from a request scope (#106608).

A request scoped to another profile sets a task-local ``HERMES_HOME`` override (the
dashboard's ``?profile=<name>`` handling). Plugin registration has process-global side
effects — dashboard-auth providers are upserted by name into a process-global registry —
so loading that profile's manager would replace the provider validating the running
dashboard's sessions and 401 every live cookie.
"""

from __future__ import annotations

from unittest.mock import patch

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def test_discover_plugins_skips_load_under_profile_override(tmp_path, monkeypatch):
    from hermes_cli import plugins as P

    other = tmp_path / "profiles" / "worker"
    other.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    token = set_hermes_home_override(str(other))
    try:
        with patch.object(P, "get_plugin_manager") as mgr, \
             patch.object(P, "_join_background_discovery") as join:
            P.discover_plugins()
    finally:
        reset_hermes_home_override(token)

    # The manager for the overridden home is never loaded (the #106608 trigger), but the
    # process-global startup discovery is still joined — skipping that would let a scoped
    # request race in-flight process-global registration.
    mgr.assert_not_called()
    join.assert_called_once()


def test_discover_plugins_loads_without_override(tmp_path, monkeypatch):
    from hermes_cli import plugins as P

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with patch.object(P, "get_plugin_manager") as mgr, \
         patch.object(P, "_join_background_discovery") as join:
        P.discover_plugins()

    join.assert_called_once()
    mgr.assert_called_once()
    mgr.return_value.discover_and_load.assert_called_once()
