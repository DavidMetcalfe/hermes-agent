"""Wizard flows (``hermes model``) must honour declared ``providers.<slug>.models``.

A ``providers.<slug>.models: [...]`` block extends that provider's model list on every
picker row surface (``list_authenticated_providers`` sections 1/2/2b and lmstudio);
the ``hermes model`` wizard flows must extend the list they prompt with the same way —
declared-first and deduped. Offline and deterministic: the credential step, the catalog
source, pricing, and the selection prompt are all stubbed — no network, no config writes.
"""


def test_openrouter_flow_prompts_with_declared_ids(monkeypatch):
    """``_model_flow_openrouter`` prompts with the declared ids merged in.

    The flow's catalog does not contain the declared id, so the pre-fix list silently
    drops it. The second arm declares an id the catalog ALSO carries, proving the merge
    dedupes it to a single declared-first entry instead of repeating it.
    """
    import hermes_cli.auth as auth_module
    import hermes_cli.models as hm
    from hermes_cli import model_setup_flows as setup

    declared = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
    curated = ["z-ai/glm-5.2", "openai/gpt-5.4"]
    captured: list = []

    # The flow does function-level imports of model_ids / _prompt_model_selection, so
    # patching the source-module attributes binds what the call resolves at run time.
    monkeypatch.setattr(setup, "_ensure_flow_api_key", lambda *_a, **_kw: ("sk-test", "sk-test", False))
    monkeypatch.setattr(setup, "_finish_model", lambda *_a, **_kw: None)
    monkeypatch.setattr(hm, "model_ids", lambda **_kw: list(curated))
    monkeypatch.setattr("hermes_cli.models_pricing.get_pricing_for_provider", lambda *_a, **_kw: {})
    monkeypatch.setattr(
        auth_module, "_prompt_model_selection",
        lambda model_ids, **_kw: captured.append(list(model_ids)) or "")

    config = {"providers": {"openrouter": {"models": [declared]}}}
    setup._model_flow_openrouter(config)
    assert captured[-1] == [declared, *curated]

    # Declared id also present in the catalog: exactly one entry, declared-first.
    monkeypatch.setattr(hm, "model_ids", lambda **_kw: [declared, *curated])
    setup._model_flow_openrouter(config)
    assert captured[-1] == [declared, *curated]


def test_xai_oauth_flow_prompts_with_declared_ids(monkeypatch):
    """``_model_flow_xai_oauth`` prompts with the declared ids merged in.

    The OAuth gate, the catalog and the selection prompt are stubbed; the flow's
    own catalog has no ``xai-oauth`` entry for the declared id, so the pre-fix
    list silently drops it. Second arm: declared id already in the catalog —
    merged exactly once, declared-first.
    """
    import hermes_cli.auth as auth_module
    import hermes_cli.models as hm
    from hermes_cli import model_setup_flows as setup

    declared = "grok-4.6-heavy"
    curated = ["grok-4.6", "grok-4.1-fast"]
    captured: list = []

    # The flow does function-level imports from hermes_cli.auth / hermes_cli.models,
    # so patching the source-module attributes binds what the calls resolve at run time.
    monkeypatch.setattr(setup, "_oauth_gate", lambda *_a, **_kw: True)
    monkeypatch.setattr(setup, "_activate_provider_model", lambda *_a, **_kw: None)
    monkeypatch.setattr(auth_module, "get_xai_oauth_auth_status", lambda: {"logged_in": True})
    monkeypatch.setattr(
        auth_module, "resolve_xai_oauth_runtime_credentials",
        lambda: {"base_url": "https://api.x.ai/v1"})
    monkeypatch.setattr(hm, "provider_model_ids", lambda *_a, **_kw: list(curated))
    monkeypatch.setattr(
        auth_module, "_prompt_model_selection",
        lambda model_ids, **_kw: captured.append(list(model_ids)) or "")

    config = {"providers": {"xai-oauth": {"models": [declared]}}}
    setup._model_flow_xai_oauth(config)
    assert captured[-1] == [declared, *curated]

    # Declared id also present in the catalog: exactly one entry, declared-first.
    monkeypatch.setattr(hm, "provider_model_ids", lambda *_a, **_kw: [declared, *curated])
    setup._model_flow_xai_oauth(config)
    assert captured[-1] == [declared, *curated]
