"""#115079: an INFERRED provider route must not silently persist to config.yaml.

Standing ruling (PR #107366): "Possessing a credential is not selecting a provider."
When a bare ``/model <name>`` is handed to a provider by ``detect_provider_for_model``
(step e) — whose only authorization gate is credential possession — the session switch
may proceed, but writing ``model.provider`` into config.yaml records a route the user
never named. Persistence therefore fails closed unless the user has NAMED the provider:
it is the auth-store active provider (a login/selection), or the config is fresh (the
first pick must persist, ``resolve_persist_behavior``'s documented intent).

The E2E rows drive the real pipeline with ``DASHSCOPE_API_KEY`` present/absent: the
injection itself must move ``provider_inferred`` (a no-op injection invalidates the
guard). Hermetic like ``test_model_switch_configured_provider_routing.py``: catalogs,
aliases, validation and metadata probes are patched; the credential ladder, routing and
the config write are real.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from unittest.mock import patch

import yaml

from hermes_cli.model_switch import ModelSwitchResult, persist_model_selection, switch_model

_ACCEPTED = {"accepted": True, "persist": True, "recognized": True, "message": None}

# A configured (NOT fresh) install: deepseek is the standing route, and an ambient
# DASHSCOPE_API_KEY seeds the credential pool for the unconfigured alibaba provider.
_SEED = (
    "model:\n"
    "  default: deepseek-chat\n"
    "  provider: deepseek\n"
    "agent:\n"
    "  system_prompt: keepme\n"
)


def _seed_home(tmp_path, monkeypatch, config_text=_SEED, *, dashscope: bool):
    """Isolated HERMES_HOME with a seeded config.yaml and a live deepseek session key;
    the ambient alibaba (DashScope) key is the injected variable under test."""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(config_text, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-session")
    if dashscope:
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dashscope-ambient")
    return home


@contextlib.contextmanager
def _offline():
    """Patch out every catalog/network lookup the switch pipeline may reach, mirroring
    ``test_model_switch_configured_provider_routing._run_switch``. The credential ladder
    (env keys), routing and the config write stay REAL."""
    with patch("hermes_cli.model_switch.resolve_alias", return_value=None), \
         patch("hermes_cli.models.cached_provider_model_ids", return_value=[]), \
         patch("hermes_cli.models.model_ids", return_value=[]), \
         patch("hermes_cli.models_validate.validate_requested_model", return_value=_ACCEPTED), \
         patch("hermes_cli.model_switch.get_model_info", return_value=None), \
         patch("hermes_cli.model_switch.get_model_capabilities", return_value=None):
        yield


def _model_block(home) -> dict:
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))["model"]


# ---------------------------------------------------------------------------
# (a) the refusal: inferred cross-provider route → no config write, named refusal
# ---------------------------------------------------------------------------

def test_inferred_cross_provider_persist_refuses_and_names_the_provider(tmp_path, monkeypatch):
    """DASHSCOPE_API_KEY alone must not earn a ``provider: alibaba`` line in config.yaml."""
    home = _seed_home(tmp_path, monkeypatch, dashscope=True)
    with _offline():
        result = switch_model(
            raw_input="qwen3.6-plus", current_provider="deepseek", current_model="deepseek-chat",
            is_global=True)
    assert result.success is True, result.error_message
    assert result.target_provider == "alibaba"
    assert result.provider_inferred is True

    refusal = persist_model_selection(result)

    assert isinstance(refusal, str) and refusal
    assert "alibaba" in refusal.lower()
    block = _model_block(home)
    assert (block["default"], block["provider"]) == ("deepseek-chat", "deepseek")
    # The refusal must tell the user how to confirm, and the session switch still won.
    assert "/model" in refusal and "--provider" in refusal


# ---------------------------------------------------------------------------
# (b) control: the user NAMED the provider → persists
# ---------------------------------------------------------------------------

def test_explicitly_named_provider_persists(tmp_path, monkeypatch):
    home = _seed_home(tmp_path, monkeypatch, dashscope=True)
    with _offline():
        result = switch_model(
            raw_input="qwen3.6-plus", current_provider="deepseek", current_model="deepseek-chat",
            is_global=True, explicit_provider="alibaba")
    assert result.success is True, result.error_message
    assert result.provider_inferred is False

    assert persist_model_selection(result) is None

    block = _model_block(home)
    assert (block["default"], block["provider"]) == ("qwen3.6-plus", "alibaba")


# ---------------------------------------------------------------------------
# (c) control: target is the auth-store active provider → persists
# ---------------------------------------------------------------------------

def test_persists_when_target_is_the_auth_store_active_provider(tmp_path, monkeypatch):
    home = _seed_home(tmp_path, monkeypatch, dashscope=True)
    (home / "auth.json").write_text(
        json.dumps({"version": 3, "providers": {}, "active_provider": "alibaba"}),
        encoding="utf-8")
    with _offline():
        result = switch_model(
            raw_input="qwen3.6-plus", current_provider="deepseek", current_model="deepseek-chat",
            is_global=True)
    assert result.provider_inferred is True

    assert persist_model_selection(result) is None

    block = _model_block(home)
    assert (block["default"], block["provider"]) == ("qwen3.6-plus", "alibaba")


# ---------------------------------------------------------------------------
# (d) control: fresh install (no model.default, no model.provider) → persists
# ---------------------------------------------------------------------------

def test_fresh_install_first_pick_persists(tmp_path, monkeypatch):
    """Documented intent (``resolve_persist_behavior``): the first pick must persist so it
    does not evaporate into a stray ``*_API_KEY`` on the next launch."""
    home = _seed_home(
        tmp_path, monkeypatch,
        "model:\n  persist_switch_by_default: false\n", dashscope=True)
    with _offline():
        result = switch_model(
            raw_input="qwen3.6-plus", current_provider="deepseek", current_model="deepseek-chat",
            is_global=True)
    assert result.provider_inferred is True

    assert persist_model_selection(result) is None

    block = _model_block(home)
    assert (block["default"], block["provider"]) == ("qwen3.6-plus", "alibaba")


# ---------------------------------------------------------------------------
# (e) control: same-provider model-only switch → persists (no provider route named)
# ---------------------------------------------------------------------------

def test_same_provider_model_only_switch_persists(tmp_path, monkeypatch):
    home = _seed_home(tmp_path, monkeypatch, dashscope=False)
    with _offline(), patch(
            "hermes_cli.models.detect_provider_for_model",
            return_value=("deepseek", "deepseek-chat-v2")):
        result = switch_model(
            raw_input="deepseek-chat-v2", current_provider="deepseek",
            current_model="deepseek-chat", is_global=True)
    assert result.success is True, result.error_message
    assert result.provider_changed is False
    # Detection handing back the CURRENT provider is a model rename, not an inferred route.
    assert result.provider_inferred is False

    assert persist_model_selection(result) is None

    block = _model_block(home)
    assert (block["default"], block["provider"]) == ("deepseek-chat-v2", "deepseek")


# ---------------------------------------------------------------------------
# (f) env-key E2E control rows: the injection must MOVE the provenance flag
# ---------------------------------------------------------------------------

def test_env_key_injection_moves_the_provenance_flag_both_ways(tmp_path, monkeypatch):
    # WITH the ambient key: detection names alibaba → inferred → persist refused.
    home = _seed_home(tmp_path / "with", monkeypatch, dashscope=True)
    with _offline():
        with_key = switch_model(
            raw_input="qwen3.6-plus", current_provider="deepseek", current_model="deepseek-chat",
            is_global=True)
    assert with_key.success is True, with_key.error_message
    assert with_key.target_provider == "alibaba"
    assert with_key.provider_inferred is True
    assert isinstance(persist_model_selection(with_key), str)
    assert _model_block(home)["provider"] == "deepseek"

    # WITHOUT it: the credential gate skips the guess, detection returns None, the switch
    # stays put — provenance clean, nothing refused.
    home_no = _seed_home(tmp_path / "without", monkeypatch, dashscope=False)
    with _offline():
        without_key = switch_model(
            raw_input="qwen3.6-plus", current_provider="deepseek", current_model="deepseek-chat",
            is_global=True)
    assert without_key.success is True, without_key.error_message
    assert without_key.target_provider == "deepseek"
    assert without_key.provider_inferred is False
    assert persist_model_selection(without_key) is None
    assert _model_block(home_no)["provider"] == "deepseek"


# ---------------------------------------------------------------------------
# (h) a TYPED provider name is a selection, not an inference
# ---------------------------------------------------------------------------

def test_typed_provider_name_is_a_selection_and_persists(tmp_path, monkeypatch):
    """``/model alibaba`` reaches step e through detect's NAMING branch (models.py:
    "explicitly named provider: let the credential step report it"), so the provenance
    check must not read the provider DIFF as inference: naming the provider IS selecting
    it, provenance stays clean and ``--global`` persists (#115079 review)."""
    home = _seed_home(tmp_path, monkeypatch, dashscope=True)
    with _offline():
        result = switch_model(
            raw_input="alibaba", current_provider="deepseek", current_model="deepseek-chat",
            is_global=True)
    assert result.success is True, result.error_message
    assert result.target_provider == "alibaba"
    assert result.provider_inferred is False

    assert persist_model_selection(result) is None

    block = _model_block(home)
    assert block["provider"] == "alibaba"
    assert block["default"]  # step 0's bare-provider-name default model, not the old route

# ---------------------------------------------------------------------------
# (i) the naming helper must NEVER suppress the flag for a bare MODEL name —
#     that is the incident class rows (a)/(f) keep refusing
# ---------------------------------------------------------------------------

def test_model_names_never_suppress_the_inference_flag():
    """Unit-level complement of the (a)/(f) E2E rows: the helper decides provenance, so
    pin its boundary directly — an alias entry or a ``vendor/``-prefixed first token that
    resolves to the DETECTED provider counts as named; anything else (above all a bare
    model name) does not."""
    from hermes_cli.model_switch import _Switch, _raw_input_names_detected_provider
    st = _Switch(
        raw_input="", current_provider="deepseek", current_model="deepseek-chat",
        current_base_url="", current_api_key="", is_global=True, explicit_provider="",
        user_providers=None, custom_providers=None)
    named = [
        ("alibaba", "alibaba"),           # bare provider id
        ("DashScope", "alibaba"),         # _PROVIDER_ALIASES entry
        ("alibaba/qwen3.6-plus", "alibaba"),   # provider/model first token
        ("alibaba:qwen3.6-plus", "alibaba"),   # vendor:model form (kept raw until step c)
    ]
    for raw, detected in named:
        assert _raw_input_names_detected_provider(raw, detected, st) is True, (raw, detected)
    not_named = [
        ("qwen3.6-plus", "alibaba"),      # the incident class: a bare MODEL name
        ("deepseek-v4.1-flash", "alibaba"),
        ("deepseek", "alibaba"),          # naming a DIFFERENT provider names nothing here
    ]
    for raw, detected in not_named:
        assert _raw_input_names_detected_provider(raw, detected, st) is False, (raw, detected)

# ---------------------------------------------------------------------------
# (j) documented fail-closed rule: an unreadable config is NOT a fresh install
# ---------------------------------------------------------------------------

def test_unreadable_config_is_not_fresh_and_refuses(tmp_path, monkeypatch):
    """A gate that opened on "the config looks fresh" when the config merely could not be
    read would hand persistence to exactly the route with the least evidence behind it.
    Mechanism note: this patches ``load_config`` to raise — that IS the documented raise
    path; a real chmod-000 config.yaml never reaches it because ``load_config`` fails open
    to a ``FailedConfigRead`` of defaults instead of raising (see T1-fix report)."""
    from hermes_cli.model_switch import inferred_provider_persist_refusal
    with patch("hermes_cli.config.load_config",
               side_effect=OSError(13, "Permission denied")), \
         patch("hermes_cli.auth.get_active_provider", return_value=None):
        refusal = inferred_provider_persist_refusal("alibaba", "CONFIRM-HINT")
    assert refusal is not None
    assert "alibaba" in refusal.lower()
    assert refusal.endswith("CONFIRM-HINT")

# ---------------------------------------------------------------------------
# Refusal message shape (the shared helper T2 also builds on)
# ---------------------------------------------------------------------------

def test_refusal_message_names_provider_and_ends_with_the_confirm_hint():
    from hermes_cli.model_switch import inferred_provider_persist_refusal
    with patch("hermes_cli.config.load_config",
               return_value={"model": {"default": "x", "provider": "deepseek"}}), \
         patch("hermes_cli.auth.get_active_provider", return_value=None):
        refusal = inferred_provider_persist_refusal("alibaba", "CONFIRM-HINT")
    assert refusal is not None
    assert "alibaba" in refusal.lower()
    assert refusal.endswith("CONFIRM-HINT")

    # Authorized readers: the auth-store active provider is a user selection.
    with patch("hermes_cli.config.load_config",
               return_value={"model": {"default": "x", "provider": "deepseek"}}), \
             patch("hermes_cli.auth.get_active_provider", return_value=" Alibaba "):
        assert inferred_provider_persist_refusal("alibaba", "CONFIRM-HINT") is None


# ---------------------------------------------------------------------------
# (g) the three persist call sites surface the refusal on their own warning channel
# ---------------------------------------------------------------------------

def _refusing_result(**overrides) -> ModelSwitchResult:
    base = dict(
        success=True, new_model="qwen3.6-plus", target_provider="alibaba",
        provider_changed=True, provider_inferred=True, is_global=True)
    return ModelSwitchResult(**{**base, **overrides})


def test_cli_surface_prints_the_refusal_instead_of_a_saved_line(tmp_path, monkeypatch):
    import cli
    from hermes_cli import cli_model_switch_mixin as mixin
    printed: list[str] = []
    monkeypatch.setattr(cli, "_cprint", lambda *a, **k: printed.append(" ".join(map(str, a))))
    monkeypatch.setattr(
        "hermes_cli.model_switch.persist_model_selection", lambda *a: "REFUSED: alibaba")
    monkeypatch.setattr(
        cli.HermesCLI, "_persist_model_switch_to_session", lambda *a, **k: None)
    monkeypatch.setattr(mixin, "_print_switch_summary", lambda *a, **k: None)
    stub = type("Stub", (), {
        "agent": None, "model": "deepseek-chat", "_pending_one_turn_model_restore": None,
        "_stage_and_swap_model": lambda self, r, o: True})()

    mixin._commit_model_switch(stub, _refusing_result(), persist_global=True)

    assert any("REFUSED" in line for line in printed)
    assert not any("Saved to config.yaml" in line for line in printed)


def test_gateway_surface_reports_the_refusal_as_the_global_error(tmp_path, monkeypatch):
    from gateway.slash_commands_model import (
        GatewayModelCommandsMixin, _ModelSwitchContext, _persist_model_switch_to_config)
    monkeypatch.setattr(
        "hermes_cli.model_switch.persist_model_selection", lambda *a: "REFUSED: alibaba")
    result = _refusing_result()
    config_path = tmp_path / "config.yaml"

    refusal = asyncio.run(_persist_model_switch_to_config(result, config_path))
    assert refusal == "REFUSED: alibaba"

    class _Store:
        async def set_model_override(self, session_key, override):
            self.saved = override

    class _Runner(GatewayModelCommandsMixin):
        config = None

        def _evict_cached_agent(self, session_key):
            pass

    store = _Store()
    runner = _Runner()
    runner.async_session_store = store
    runner._session_model_overrides = {}
    ctx = _ModelSwitchContext(
        session_key="k", source=None, config_path=config_path, persist_global=True)
    ctx.current_model = "deepseek-chat"

    global_error = asyncio.run(
        runner._record_model_switch(result, ctx, source=None, one_turn=False, picker=False))

    assert global_error == "REFUSED: alibaba"
    # The refused switch keeps its session override instead of config.yaml becoming the
    # durable authority (#100314's failure mode runs backwards here — claiming global while
    # nothing was written is exactly what the refusal prevents).
    assert store.saved["model"] == "qwen3.6-plus"


def test_tui_surface_propagates_the_refusal_into_the_switch_warning(tmp_path, monkeypatch):
    _seed_home(tmp_path, monkeypatch, dashscope=False)  # HERMES_HOME + config sandbox
    from tui_gateway import server
    result = _refusing_result(warning_message="pre-existing warning")
    monkeypatch.setattr("hermes_cli.model_switch.switch_model", lambda **kw: result)
    monkeypatch.setattr(
        "hermes_cli.model_switch.persist_model_selection", lambda *a: "REFUSED: alibaba")
    monkeypatch.setattr("tui_gateway.server._emit", lambda *a, **k: None, raising=False)
    monkeypatch.setattr("tui_gateway.server._restart_slash_worker", lambda *a, **k: None)
    monkeypatch.setattr("tui_gateway.server._session_info", lambda *a, **k: None)

    out = server._apply_model_switch(
        "sid", {"agent": None}, "qwen3.6-plus --global", confirm_expensive_model=True)

    assert out["value"] == "qwen3.6-plus"
    assert "REFUSED: alibaba" in out["warning"]
    assert "pre-existing warning" in out["warning"]
