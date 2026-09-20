"""Regression for #49724: inbound Mattermost emoji reaction events.

Mattermost delivers ``reaction_added`` / ``reaction_removed`` over the
WebSocket with ``data["reaction"]`` as a JSON-encoded string of a Reaction
object. These tests lock the adapter's behaviour contracts: the hook is
ungated and shaped, agent routing is opt-in (boolean = bot's own posts only,
list = emoji allowlist on any post), self-reactions and malformed payloads are
inert, redeliveries dedup, routed events carry a reaction-scoped message id,
channel gating parity holds, and DM/thread session continuity resolves
through the reacted-to post.
"""
import json
import os
import pytest
from unittest.mock import AsyncMock

from tests.gateway.test_mattermost import _make_adapter

BOT = "bot_user_id"
HUMAN = "user_123"
POST = "post_1"
CHAN = "chan_456"
CREATE_AT = 1700000000000


@pytest.fixture(autouse=True)
def _clean_reaction_env(monkeypatch):
    """The gate/triggers readers consult os.environ — start every test clean."""
    for var in (
        "MATTERMOST_REACTION_TRIGGERS",
        "MATTERMOST_ALLOWED_CHANNELS",
        "MATTERMOST_REQUIRE_MENTION",
        "MATTERMOST_FREE_RESPONSE_CHANNELS",
    ):
        monkeypatch.delenv(var, raising=False)


class _FakeApiGet:
    """Async stand-in for the adapter's REST GET helper.

    Answers BOTH shapes the reaction path needs — ``posts/{id}`` and
    ``channels/{id}`` — and records every path in ``.calls`` so tests can
    prove which lookups actually happened (a one-shape fake makes
    assertions vacuous).
    """

    def __init__(self, *, posts=None, channels=None):
        self._table = {
            "posts": posts if posts is not None else {POST: {"user_id": HUMAN, "channel_id": CHAN, "root_id": ""}},
            "channels": channels if channels is not None else {CHAN: {"type": "O"}},
        }
        self.calls = []

    async def __call__(self, path):
        self.calls.append(path)
        kind, _, ident = path.partition("/")
        return self._table.get(kind, {}).get(ident, {})


def _reaction_adapter(*, triggers=None, posts=None, channels=None):
    """Adapter wired like the existing mattermost module's WS-parsing tests.

    ``triggers`` seeds the ``reaction_triggers`` config key (absent → the
    default-off contract).
    """
    adapter = _make_adapter()
    adapter._bot_user_id = BOT
    adapter._bot_username = "hermes-bot"
    adapter.handle_message = AsyncMock()
    adapter._reaction_handler = AsyncMock()
    api_get = _FakeApiGet(posts=posts, channels=channels)
    adapter._api_get = api_get
    if triggers is not None:
        adapter.config.extra["reaction_triggers"] = triggers
    return adapter


def _reaction_event(emoji="white_check_mark", *, event="reaction_added", user=HUMAN,
                    post=POST, channel=CHAN, create_at=CREATE_AT, raw=None):
    """Envelope shaped like Mattermost's: data.reaction is a JSON string and
    the broadcast carries the channel id."""
    if raw is not None:
        data = {"reaction": raw}
    else:
        reaction = {
            "user_id": user, "post_id": post, "emoji_name": emoji,
            "channel_id": channel, "create_at": create_at,
        }
        data = {"reaction": json.dumps(reaction)}
    return {"event": event, "data": data, "broadcast": {"channel_id": channel}}


def _added_event_without(key):
    reaction = {"user_id": HUMAN, "post_id": POST, "emoji_name": "tada",
                "channel_id": CHAN, "create_at": CREATE_AT}
    reaction.pop(key)
    return {"event": "reaction_added", "data": {"reaction": json.dumps(reaction)},
            "broadcast": {"channel_id": CHAN}}


# ---------------------------------------------------------------------------
# Contract 1: default off — hook fires, routing doesn't
# ---------------------------------------------------------------------------

class TestReactionDefaultOff:
    @pytest.mark.asyncio
    async def test_human_reaction_fires_hook_but_does_not_route(self):
        adapter = _reaction_adapter()
        await adapter._handle_ws_event(_reaction_event())
        assert adapter._reaction_handler.await_count == 1
        assert not adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_default_off_never_routes_even_on_bots_own_post(self):
        adapter = _reaction_adapter(
            posts={POST: {"user_id": BOT, "channel_id": CHAN, "root_id": ""}})
        await adapter._handle_ws_event(_reaction_event())
        assert not adapter.handle_message.called


# ---------------------------------------------------------------------------
# Contract 2: hook is ungated and shaped
# ---------------------------------------------------------------------------

class TestReactionHookShape:
    @pytest.mark.asyncio
    async def test_added_hook_payload_shape(self):
        adapter = _reaction_adapter()
        await adapter._handle_ws_event(_reaction_event(emoji="white_check_mark"))
        assert adapter._reaction_handler.await_count == 1
        payload = adapter._reaction_handler.await_args[0][0]
        assert payload["platform"] == "mattermost"
        assert payload["event_name"] == "reaction:added"
        assert payload["reaction"] == "white_check_mark"
        assert payload["user_id"] == HUMAN
        assert payload["message_ts"] == POST
        assert payload["channel_id"] == CHAN
        assert payload["event_ts"] == str(CREATE_AT)
        assert payload["item_type"] == "message"
        assert payload["item_user_id"] == ""
        assert payload["team_id"] == ""

    @pytest.mark.asyncio
    async def test_removed_hook_event_name(self):
        adapter = _reaction_adapter()
        await adapter._handle_ws_event(_reaction_event(event="reaction_removed"))
        payload = adapter._reaction_handler.await_args[0][0]
        assert payload["event_name"] == "reaction:removed"

    @pytest.mark.asyncio
    async def test_hook_fires_for_every_human_reaction_on_any_post(self):
        """Routing is disabled (default) and the post is not the bot's — the
        hook must still fire; it is the ungated surface."""
        adapter = _reaction_adapter()
        await adapter._handle_ws_event(_reaction_event(emoji="tada"))
        assert adapter._reaction_handler.await_count == 1
        assert not adapter.handle_message.called


# ---------------------------------------------------------------------------
# Contract 3: boolean form is bot-posts-only (security-relevant default)
# ---------------------------------------------------------------------------

class TestReactionBooleanForm:
    @pytest.mark.asyncio
    async def test_boolean_routes_reaction_on_bots_own_post_once(self):
        adapter = _reaction_adapter(
            triggers=True,
            posts={POST: {"user_id": BOT, "channel_id": CHAN, "root_id": ""}})
        await adapter._handle_ws_event(_reaction_event(emoji="white_check_mark"))
        assert adapter.handle_message.await_count == 1
        msg = adapter.handle_message.await_args[0][0]
        assert msg.text == "reaction:added:✅"

    @pytest.mark.asyncio
    async def test_boolean_ignores_reaction_on_non_bot_post(self):
        adapter = _reaction_adapter(triggers=True)
        await adapter._handle_ws_event(_reaction_event(emoji="white_check_mark"))
        # The author gate was actually consulted (post was fetched), not skipped.
        assert f"posts/{POST}" in adapter._api_get.calls
        assert not adapter.handle_message.called
        assert adapter._reaction_handler.await_count == 1  # hook is ungated


# ---------------------------------------------------------------------------
# Contract 4: allowlist form
# ---------------------------------------------------------------------------

class TestReactionAllowlist:
    @pytest.mark.asyncio
    async def test_allowlist_routes_listed_emoji_on_non_bot_post(self):
        adapter = _reaction_adapter(triggers=["white_check_mark"])
        await adapter._handle_ws_event(_reaction_event(emoji="white_check_mark"))
        assert adapter.handle_message.await_count == 1
        msg = adapter.handle_message.await_args[0][0]
        assert msg.text == "reaction:added:✅"

    @pytest.mark.asyncio
    async def test_allowlist_drops_unlisted_emoji(self):
        adapter = _reaction_adapter(triggers=["white_check_mark"])
        await adapter._handle_ws_event(_reaction_event(emoji="tada"))
        assert adapter._reaction_handler.await_count == 1
        assert not adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_unmapped_emoji_name_passes_through_raw(self):
        adapter = _reaction_adapter(triggers=["shark"])
        await adapter._handle_ws_event(_reaction_event(emoji="shark"))
        msg = adapter.handle_message.await_args[0][0]
        assert msg.text == "reaction:added:shark"


# ---------------------------------------------------------------------------
# Contract 5: reaction_removed shape
# ---------------------------------------------------------------------------

class TestReactionRemoved:
    @pytest.mark.asyncio
    async def test_removed_text_and_hook_event_name(self):
        adapter = _reaction_adapter(triggers=["white_check_mark"])
        await adapter._handle_ws_event(
            _reaction_event(emoji="white_check_mark", event="reaction_removed"))
        assert adapter.handle_message.await_count == 1
        msg = adapter.handle_message.await_args[0][0]
        assert msg.text == "reaction:removed:✅"
        payload = adapter._reaction_handler.await_args[0][0]
        assert payload["event_name"] == "reaction:removed"


# ---------------------------------------------------------------------------
# Contract 6: self-reaction loop break
# ---------------------------------------------------------------------------

class TestReactionSelfLoopBreak:
    @pytest.mark.asyncio
    async def test_bot_own_reaction_fires_neither_hook_nor_routing(self):
        adapter = _reaction_adapter(triggers=["white_check_mark"])
        await adapter._handle_ws_event(_reaction_event(user=BOT))
        assert not adapter._reaction_handler.called
        assert not adapter.handle_message.called


# ---------------------------------------------------------------------------
# Contract 7: malformed input is inert
# ---------------------------------------------------------------------------

class TestReactionMalformedInput:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("event", [
        pytest.param(_reaction_event(raw="not json"), id="not-json"),
        pytest.param({"event": "reaction_added", "data": {},
                      "broadcast": {"channel_id": CHAN}}, id="missing-reaction-key"),
        pytest.param(_reaction_event(raw=json.dumps({})), id="empty-reaction"),
        pytest.param(_added_event_without("post_id"), id="missing-post-id"),
        pytest.param(_added_event_without("user_id"), id="missing-user-id"),
        pytest.param(_added_event_without("emoji_name"), id="missing-emoji-name"),
    ])
    async def test_malformed_reaction_is_a_noop(self, event):
        """No hook, no routing, no exception escaping _handle_ws_event."""
        adapter = _reaction_adapter(triggers=["white_check_mark"])
        await adapter._handle_ws_event(event)
        assert not adapter._reaction_handler.called
        assert not adapter.handle_message.called


# ---------------------------------------------------------------------------
# Contract 8: redelivery dedup
# ---------------------------------------------------------------------------

class TestReactionRedeliveryDedup:
    @pytest.mark.asyncio
    async def test_same_reaction_delivered_twice_emits_once(self):
        adapter = _reaction_adapter(
            triggers=True,
            posts={POST: {"user_id": BOT, "channel_id": CHAN, "root_id": ""}})
        event = _reaction_event()
        await adapter._handle_ws_event(event)
        await adapter._handle_ws_event(event)
        assert adapter._reaction_handler.await_count == 1
        assert adapter.handle_message.await_count == 1

    @pytest.mark.asyncio
    async def test_different_emoji_same_user_same_post_not_swallowed(self):
        adapter = _reaction_adapter(triggers=["white_check_mark", "tada"])
        await adapter._handle_ws_event(_reaction_event(emoji="white_check_mark"))
        await adapter._handle_ws_event(_reaction_event(emoji="tada"))
        assert adapter._reaction_handler.await_count == 2
        assert adapter.handle_message.await_count == 2
        texts = [c[0][0].text for c in adapter.handle_message.await_args_list]
        assert texts == ["reaction:added:✅", "reaction:added:🎉"]


# ---------------------------------------------------------------------------
# Contract 9: reaction-scoped message id
# ---------------------------------------------------------------------------

class TestReactionScopedMessageId:
    @pytest.mark.asyncio
    async def test_message_id_is_not_the_reacted_to_post_id(self):
        """Reusing the reacted-to post's id would let the gateway's message-id
        dedup conflate the reaction with the reacted-to message."""
        adapter = _reaction_adapter(triggers=["white_check_mark"])
        await adapter._handle_ws_event(_reaction_event(emoji="white_check_mark"))
        msg = adapter.handle_message.await_args[0][0]
        assert msg.message_id != POST
        assert msg.source.message_id != POST
        assert msg.message_id == msg.source.message_id


# ---------------------------------------------------------------------------
# Contract 10: gate parity — force_process skips mention, never the whitelist
# ---------------------------------------------------------------------------

class TestReactionGateParity:
    @pytest.mark.asyncio
    async def test_force_process_skips_only_the_mention_requirement(self):
        """require_mention defaults true and chan_456 is neither free-response
        nor mentioned — the reaction still routes: force_process skips ONLY
        the mention requirement."""
        adapter = _reaction_adapter(triggers=["white_check_mark"])
        await adapter._handle_ws_event(_reaction_event())
        assert adapter.handle_message.await_count == 1

    @pytest.mark.asyncio
    async def test_allowed_channels_whitelist_still_blocks_reaction(self, monkeypatch):
        """The channel whitelist is enforced even on the force_process path."""
        monkeypatch.setenv("MATTERMOST_ALLOWED_CHANNELS", "chan_something_else")
        adapter = _reaction_adapter(triggers=["white_check_mark"])
        await adapter._handle_ws_event(_reaction_event())
        assert not adapter.handle_message.called


# ---------------------------------------------------------------------------
# Contract 11: session continuity (DM chat_type, thread root_id)
# ---------------------------------------------------------------------------

class TestReactionSessionContinuity:
    @pytest.mark.asyncio
    async def test_dm_reaction_resolves_dm_chat_type(self):
        adapter = _reaction_adapter(
            triggers=["white_check_mark"],
            posts={POST: {"user_id": HUMAN, "channel_id": "chan_dm", "root_id": ""}},
            channels={"chan_dm": {"type": "D"}})
        await adapter._handle_ws_event(_reaction_event(channel="chan_dm"))
        assert adapter.handle_message.await_count == 1
        msg = adapter.handle_message.await_args[0][0]
        assert msg.source.chat_type == "dm"
        # The type came from the channels API, not a default guess.
        assert "channels/chan_dm" in adapter._api_get.calls

    @pytest.mark.asyncio
    async def test_thread_reply_reaction_resolves_root_id_as_thread(self):
        adapter = _reaction_adapter(
            triggers=["white_check_mark"],
            posts={POST: {"user_id": HUMAN, "channel_id": CHAN, "root_id": "root_9"}})
        await adapter._handle_ws_event(_reaction_event())
        assert adapter.handle_message.await_count == 1
        msg = adapter.handle_message.await_args[0][0]
        assert msg.source.thread_id == "root_9"


# ---------------------------------------------------------------------------
# Contract 12: _reaction_triggers() parsing table
# ---------------------------------------------------------------------------

class TestReactionTriggersParsing:
    @pytest.mark.parametrize("raw,expected", [
        pytest.param(..., None, id="absent"),
        pytest.param(False, None, id="false-bool"),
        pytest.param("false", None, id="false-str"),
        pytest.param("0", None, id="zero-str"),
        pytest.param("no", None, id="no-str"),
        pytest.param("off", None, id="off-str"),
        pytest.param("", None, id="empty-str"),
        pytest.param(True, set(), id="true-bool"),
        pytest.param("true", set(), id="true-str"),
        pytest.param("all", set(), id="all-str"),
        pytest.param("on", set(), id="on-str"),
        pytest.param("1", set(), id="one-str"),
        pytest.param("*", set(), id="star-str"),
        pytest.param("white_check_mark,thumbsup", {"white_check_mark", "thumbsup"}, id="comma-list"),
        pytest.param("white_check_mark thumbsup", {"white_check_mark", "thumbsup"}, id="whitespace-list"),
        pytest.param(["thumbsup"], {"thumbsup"}, id="list"),
        pytest.param(":thumbsup:", {"thumbsup"}, id="colon-stripped"),
    ])
    def test_triggers_table(self, raw, expected):
        adapter = _make_adapter()
        if raw is not ...:
            adapter.config.extra["reaction_triggers"] = raw
        assert adapter._reaction_triggers() == expected

    @pytest.mark.parametrize("env_value,expected", [
        pytest.param("white_check_mark,:thumbsup:", {"white_check_mark", "thumbsup"}, id="env-list"),
        pytest.param("all", set(), id="env-all"),
        pytest.param("false", None, id="env-false"),
    ])
    def test_env_var_honoured_when_yaml_key_absent(self, monkeypatch, env_value, expected):
        monkeypatch.setenv("MATTERMOST_REACTION_TRIGGERS", env_value)
        adapter = _make_adapter()
        assert adapter._reaction_triggers() == expected


# ---------------------------------------------------------------------------
# Contract 13: YAML bridge — mattermost.reaction_triggers reaches the env var
# ---------------------------------------------------------------------------

class TestReactionYamlBridge:
    def test_yaml_bridge_reaches_the_trigger_reader(self):
        """Drives the real ``apply_yaml_config_fn`` bridge (_apply_yaml_config)
        and asserts the observable outcome: config.yaml's
        ``mattermost.reaction_triggers`` reaches MATTERMOST_REACTION_TRIGGERS
        and, through it, ``_reaction_triggers()``."""
        from plugins.platforms.mattermost.adapter import _apply_yaml_config
        seeded = _apply_yaml_config({}, {"reaction_triggers": ["white_check_mark", "thumbsup"]})
        try:
            assert seeded is not None and "reaction_triggers" in seeded
            assert os.environ.get("MATTERMOST_REACTION_TRIGGERS") == "white_check_mark,thumbsup"
            # Env bridge alone (no extra key) must reach the parser.
            adapter = _make_adapter()
            assert adapter._reaction_triggers() == {"white_check_mark", "thumbsup"}
            # And the seeded extra the gateway lifts into PlatformConfig.extra.
            adapter2 = _make_adapter()
            adapter2.config.extra.update(seeded)
            assert adapter2._reaction_triggers() == {"white_check_mark", "thumbsup"}
        finally:
            os.environ.pop("MATTERMOST_REACTION_TRIGGERS", None)

    def test_yaml_bridge_boolean_form(self):
        """``reaction_triggers: true`` in config.yaml bridges through and the
        parser reads it as the bot-posts-only empty set."""
        from plugins.platforms.mattermost.adapter import _apply_yaml_config
        seeded = _apply_yaml_config({}, {"reaction_triggers": True})
        assert seeded is not None
        try:
            assert seeded == {"reaction_triggers": True}
            adapter = _make_adapter()
            adapter.config.extra.update(seeded)
            assert adapter._reaction_triggers() == set()
        finally:
            os.environ.pop("MATTERMOST_REACTION_TRIGGERS", None)
