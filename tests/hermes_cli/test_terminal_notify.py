"""display.bell_on_prompt / bell_on_complete also drive OSC 9 + Warp OSC 777 via _ring_bell."""

import json

import pytest

from cli import HermesCLI
from hermes_cli import os_notify, terminal_notify

_WARP_OK = {
    "TERM_PROGRAM": "WarpTerminal",
    "WARP_CLI_AGENT_PROTOCOL_VERSION": "1",
    "WARP_CLIENT_VERSION": "v0.2026.08.01.00.00.stable_01",
}


def _ring(monkeypatch, *, flag_on, env, **kwargs):
    for key in _WARP_OK:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    written = []
    monkeypatch.setattr(terminal_notify, "_write_tty", written.append)
    cli = HermesCLI.__new__(HermesCLI)
    cli.bell_on_prompt = flag_on
    cli.session_id = "sess-1"
    cli._ring_bell(prompt=True, **kwargs)
    return "".join(written)


def test_osc9_body_emitted_and_sanitized_only_when_flag_on(monkeypatch):
    out = _ring(monkeypatch, flag_on=True, env={}, context="approval\x1b\x07\x00\x7f!")
    assert out == "\x1b]9;Hermes: approval!\x07"
    assert _ring(monkeypatch, flag_on=False, env={}, context="approval") == ""


def test_warp_osc777_only_under_supported_warp_build(monkeypatch):
    out = _ring(monkeypatch, flag_on=True, env=_WARP_OK, context="approval", detail="rm -rf build")
    prefix = "\x1b]777;notify;warp://cli-agent;"
    assert out.count(prefix) == 1
    payload = json.loads(out.split(prefix, 1)[1].rstrip("\x07"))
    assert payload["agent"] == "hermes"
    assert payload["event"] == "permission_request"
    assert payload["summary"] == "rm -rf build"
    assert payload["session_id"] == "sess-1"
    assert payload["v"] == 1
    # Broken build (advertises the protocol var but can't render) → OSC 9 only.
    broken = dict(_WARP_OK, WARP_CLIENT_VERSION="v0.2026.03.25.08.24.stable_05")
    assert prefix not in _ring(monkeypatch, flag_on=True, env=broken, context="approval")
    # Not Warp at all → OSC 9 only.
    not_warp = dict(_WARP_OK, TERM_PROGRAM="ghostty")
    assert prefix not in _ring(monkeypatch, flag_on=True, env=not_warp, context="approval")


def test_clarify_callback_notification_carries_question(monkeypatch):
    for key in _WARP_OK:
        monkeypatch.delenv(key, raising=False)
    written = []
    monkeypatch.setattr(terminal_notify, "_write_tty", written.append)
    cli = HermesCLI.__new__(HermesCLI)
    cli.bell_on_prompt = True
    cli.session_id = "sess-1"
    cli._paint_now = lambda: None
    cli._poll_modal_queue = lambda queue, deadline_attr: "a.txt"
    cli._persist_prompt_summary = lambda *args, **kwargs: None

    cli._clarify_callback("Which output file should I write?", ["a.txt", "b.txt"])
    out = "".join(written)
    assert "Which output file should I write?" in out
    assert out == "\x1b]9;Hermes: clarify — Which output file should I write?\x07"


def test_clarify_callback_batch_notification_carries_first_question(monkeypatch):
    for key in _WARP_OK:
        monkeypatch.delenv(key, raising=False)
    written = []
    monkeypatch.setattr(terminal_notify, "_write_tty", written.append)
    cli = HermesCLI.__new__(HermesCLI)
    cli.bell_on_prompt = True
    cli.session_id = "sess-1"
    cli._paint_now = lambda: None
    cli._poll_modal_queue = lambda queue, deadline_attr: {"q0": "a.txt"}
    cli._persist_prompt_summary = lambda *args, **kwargs: None

    questions = [
        {"qid": "q0", "question": "Which output file should I write?", "choices": ["a.txt", "b.txt"], "multi_select": False},
        {"qid": "q1", "question": "Overwrite existing files?", "choices": ["yes", "no"], "multi_select": False},
    ]
    cli._clarify_callback("", None, questions=questions)
    out = "".join(written)
    assert "Which output file should I write?" in out
    assert "Overwrite existing files?" not in out
    assert out == "\x1b]9;Hermes: clarify — Which output file should I write?\x07"


def test_prompt_body_multiline_collapses_to_one_line():
    question = "Which output file\nshould I write?\n\n  Please pick   one."
    assert terminal_notify.prompt_body("clarify", question) == (
        "clarify — Which output file should I write? Please pick one."
    )


def test_prompt_body_empty_or_whitespace_detail_yields_kind():
    assert terminal_notify.prompt_body("clarify", "") == "clarify"
    assert terminal_notify.prompt_body("clarify", "   \t\n  ") == "clarify"
    assert terminal_notify.prompt_body("clarify") == "clarify"


def test_prompt_body_capped_to_limit():
    long_question = "x" * 250
    res = terminal_notify.prompt_body("clarify", long_question)
    assert res == f"clarify — {'x' * terminal_notify._BODY_LIMIT}"
    assert len(res) == len("clarify — ") + terminal_notify._BODY_LIMIT


def test_prompt_body_control_characters_sanitized_in_emitted_bytes(monkeypatch):
    for key in _WARP_OK:
        monkeypatch.delenv(key, raising=False)
    written = []
    monkeypatch.setattr(terminal_notify, "_write_tty", written.append)
    body = terminal_notify.prompt_body("clarify", "Which\x1b file\x07 to\x00 write\x7f?")
    terminal_notify.notify(body, prompt=True)
    out = "".join(written)
    assert out == "\x1b]9;Hermes: clarify — Which file to write?\x07"


def test_notify_fallback_when_terminal_not_osc9_capable(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    monkeypatch.delenv("TERM", raising=False)
    for key in ("WARP_CLI_AGENT_PROTOCOL_VERSION", "WARP_CLIENT_VERSION"):
        monkeypatch.delenv(key, raising=False)
    written = []
    monkeypatch.setattr(terminal_notify, "_write_tty", written.append)
    spy = []
    monkeypatch.setattr(os_notify, "notify", lambda title, body: spy.append((title, body)) or True)

    prompt_text = "clarify — Which output file should I write?"
    terminal_notify.notify(prompt_text, prompt=True)

    assert len(spy) == 1
    assert spy[0] == ("Hermes", prompt_text)
    out = "".join(written)
    assert out == f"\x1b]9;Hermes: {prompt_text}\x07"


def test_notify_no_double_notification_when_terminal_osc9_capable(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.delenv("TERM", raising=False)
    for key in ("WARP_CLI_AGENT_PROTOCOL_VERSION", "WARP_CLIENT_VERSION"):
        monkeypatch.delenv(key, raising=False)
    written = []
    monkeypatch.setattr(terminal_notify, "_write_tty", written.append)
    spy = []
    monkeypatch.setattr(os_notify, "notify", lambda title, body: spy.append((title, body)) or True)

    prompt_text = "clarify — Which output file should I write?"
    terminal_notify.notify(prompt_text, prompt=True)

    assert len(spy) == 0
    out = "".join(written)
    assert out == f"\x1b]9;Hermes: {prompt_text}\x07"


def test_notify_fallback_failure_isolation(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    monkeypatch.delenv("TERM", raising=False)
    for key in ("WARP_CLI_AGENT_PROTOCOL_VERSION", "WARP_CLIENT_VERSION"):
        monkeypatch.delenv(key, raising=False)
    written = []
    monkeypatch.setattr(terminal_notify, "_write_tty", written.append)

    def _broken_notify(title, body):
        raise OSError("notification daemon unreachable")

    monkeypatch.setattr(os_notify, "notify", _broken_notify)

    prompt_text = "clarify — Which output file should I write?"
    terminal_notify.notify(prompt_text, prompt=True)

    out = "".join(written)
    assert out == f"\x1b]9;Hermes: {prompt_text}\x07"


@pytest.mark.parametrize(
    "term_prog",
    [
        "iterm.app",
        "iTerm.app",
        "ITERM.APP",
        "ghostty",
        "Ghostty",
        "GHOSTTY",
        "wezterm",
        "WezTerm",
        "WEZTERM",
        "warpterminal",
        "WarpTerminal",
        "WARPTERMINAL",
        "vscode",
        "VSCode",
        "VSCODE",
        "cursor",
        "Cursor",
        "CURSOR",
    ],
)
def test_osc9_capable_set_members_case_insensitive(term_prog):
    assert terminal_notify.osc9_capable({"TERM_PROGRAM": term_prog}) is True


def test_osc9_capable_kitty():
    assert terminal_notify.osc9_capable({"TERM": "xterm-kitty"}) is True
    assert terminal_notify.osc9_capable({"TERM": "kitty"}) is True
    assert terminal_notify.osc9_capable({"TERM": "xterm-kitty", "TERM_PROGRAM": ""}) is True


def test_osc9_capable_incapable_and_empty():
    assert terminal_notify.osc9_capable({"TERM_PROGRAM": "Apple_Terminal"}) is False
    assert terminal_notify.osc9_capable({"TERM_PROGRAM": "alacritty"}) is False
    assert terminal_notify.osc9_capable({"TERM_PROGRAM": "unknown_term"}) is False
    assert terminal_notify.osc9_capable({}) is False
    assert terminal_notify.osc9_capable({"TERM_PROGRAM": "", "TERM": ""}) is False


def test_osc9_capable_defaults_to_environ(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    assert terminal_notify.osc9_capable() is True
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    monkeypatch.delenv("TERM", raising=False)
    assert terminal_notify.osc9_capable() is False


