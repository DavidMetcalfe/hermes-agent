"""Tests for the second_voice plugin.

Covers:
  * Middleware wiring: register() registers a ``tool_execution`` middleware.
  * Approve → the tool executes (``next_call`` invoked).
  * REDO → the tool is blocked (``next_call`` NOT invoked) and an error result with the critique
    is returned so the executor corrects course.
  * Breaker → after ``max_consecutive_rejections`` REDOs, the block escalates to "ask the user".
  * Pass-through → a non-gated tool always executes; reflection fail-open on LLM error.
"""

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    yield hermes_home


def _load_plugin():
    """Import the plugin package with the ``hermes_plugins.<name>`` namespace so relative imports work."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_dir = repo_root / "plugins" / "second_voice"
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    pkg_name = "hermes_plugins.second_voice"
    spec = importlib.util.spec_from_file_location(
        pkg_name, plugin_dir / "__init__.py", submodule_search_locations=[str(plugin_dir)]
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = pkg_name
    mod.__path__ = [str(plugin_dir)]
    sys.modules[pkg_name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    """Minimal PluginContext stand-in: config dict + middleware recording."""

    def __init__(self, config=None):
        self.config = config or {}
        self.middleware = {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_middleware(self, kind, callback):
        self.middleware[kind] = callback


def _fake_call_llm(verdict_text):
    """Return a call_llm replacement that returns a fixed one-line verdict."""
    def _impl(**kwargs):
        msg = SimpleNamespace(content=verdict_text)
        choice = SimpleNamespace(message=msg)
        return SimpleNamespace(choices=[choice])
    return _impl


def _patch_call_llm(monkeypatch, verdict_text):
    monkeypatch.setattr("agent.auxiliary_client.call_llm", _fake_call_llm(verdict_text))


def _reflection_config(**overrides):
    cfg = {
        "reflect_tools": ["terminal", "write_file"],
        "max_consecutive_rejections": 3,
        "max_instruction_chars": 2000,
    }
    cfg.update(overrides)
    return cfg


class TestMiddleware:
    def _get_callback(self, **config):
        plugin = _load_plugin()
        ctx = FakeCtx(config if config else _reflection_config())
        plugin.register(ctx)
        return ctx.middleware.get("tool_execution")

    def test_registers_tool_execution_middleware(self, _isolate_env):
        assert self._get_callback() is not None

    def test_approve_invokes_next_call(self, _isolate_env, monkeypatch):
        _patch_call_llm(monkeypatch, "APPROVE")
        cb = self._get_callback()
        calls = []
        result = cb(tool_name="terminal", args={"command": "ls"}, next_call=calls.append,
                    session_id="", turn_id="t1")
        assert calls == [{"command": "ls"}]
        # next_call's return is the downstream tool result; it is piped straight through.
        assert result is None  # mock append returns None

    def test_redo_blocks_and_feeds_critique(self, _isolate_env, monkeypatch):
        _patch_call_llm(monkeypatch, "REDO: this deletes the wrong directory")
        cb = self._get_callback()
        dispatched = []
        result = cb(tool_name="terminal", args={"command": "rm -rf /tmp/x"}, next_call=dispatched.append,
                    session_id="", turn_id="t1")
        assert dispatched == []  # tool never executed
        import json
        payload = json.loads(result) if isinstance(result, str) else result
        assert payload["blocked_by"] == "second_voice"
        assert "wrong directory" in payload["error"]

    def test_non_gated_tool_passes_through(self, _isolate_env, monkeypatch):
        _patch_call_llm(monkeypatch, "REDO: should not reach the LLM")
        cb = self._get_callback()
        dispatched = []
        cb(tool_name="web_search", args={"query": "x"}, next_call=dispatched.append,
           session_id="", turn_id="t1")
        assert dispatched == [{"query": "x"}]

    def test_breaker_escalates_after_n_rejections(self, _isolate_env, monkeypatch):
        _patch_call_llm(monkeypatch, "REDO: reason")
        cb = self._get_callback(max_consecutive_rejections=3)
        import json
        for i in range(3):
            result = cb(tool_name="terminal", args={}, next_call=lambda *a, **k: None,
                        session_id="s", turn_id="t")
            payload = json.loads(result) if isinstance(result, str) else result
        assert payload["escalated"] is True
        assert "ask the user" in payload["error"]

    def test_reflection_llm_error_fails_open_to_approve(self, _isolate_env, monkeypatch):
        def _boom(**kwargs):
            raise RuntimeError("provider down")
        monkeypatch.setattr("agent.auxiliary_client.call_llm", _boom)
        cb = self._get_callback()
        dispatched = []
        result = cb(tool_name="terminal", args={"command": "ls"}, next_call=dispatched.append,
                    session_id="", turn_id="t1")
        assert dispatched == [{"command": "ls"}]


class TestReflectionHelpers:
    def test_parse_verdict_approve(self):
        from types import SimpleNamespace
        plugin = _load_plugin()
        assert plugin.r.parse_verdict("APPROVE")[0] == "approve"

    def test_parse_verdict_redo_with_reason(self):
        plugin = _load_plugin()
        verdict, reason = plugin.r.parse_verdict("REDO: incomplete")
        assert verdict == "redo"
        assert reason == "incomplete"

    def test_parse_verdict_garbage_fails_open_approve(self, _isolate_env):
        plugin = _load_plugin()
        assert plugin.r.parse_verdict("maybe?")[0] == "approve"

    def test_block_result_shape(self):
        plugin = _load_plugin()
        import json
        payload = json.loads(plugin.r.block_result("nope"))
        assert payload["error"] == "nope"
        assert payload["blocked_by"] == "second_voice"


class TestBundledDiscovery:
    """Load through real PluginManager discovery with a temp HERMES_HOME."""

    def _enable(self, hermes_home, names):
        import yaml
        cfg_path = hermes_home / "config.yaml"
        cfg_path.write_text(yaml.safe_dump({"plugins": {"enabled": list(names)}}))

    def test_discovered_but_not_loaded_by_default(self, _isolate_env):
        from hermes_cli import plugins as pmod
        mgr = pmod.PluginManager()
        mgr.discover_and_load()
        entry = next(
            (p for p in mgr._plugins.values() if p.manifest.name == "second_voice"), None
        )
        assert entry is not None
        assert entry.manifest.source == "bundled"
        assert not entry.enabled

    def test_enabled_registers_tool_execution_middleware(self, _isolate_env):
        self._enable(_isolate_env, ["second_voice"])
        from hermes_cli import plugins as pmod
        mgr = pmod.PluginManager()
        mgr.discover_and_load()
        entry = mgr._plugins.get("second_voice")
        if entry is None:
            entry = next(
                (p for p in mgr._plugins.values() if p.manifest.name == "second_voice"), None
            )
        assert entry is not None and entry.enabled
        assert len(mgr._middleware.get("tool_execution", [])) > 0

