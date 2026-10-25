from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from gateway.platforms.helpers import MessageDeduplicator
from plugins.platforms.discord.adapter import DiscordAdapter
from tests.e2e.conftest import (
    _make_discord_adapter_wired,
    make_discord_message,
    make_fake_thread,
)


BOT_ID = 99
SELF_ROLE_ID = 700
OTHER_ROLE_ID = 701


def _adapter(*, allowed=True):
    adapter = object.__new__(DiscordAdapter)
    adapter.config = SimpleNamespace(extra={})
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=BOT_ID, bot=True))
    adapter._dedup = MessageDeduplicator()
    adapter._get_allow_bots = Mock(return_value="mentions")
    adapter._is_allowed_user = Mock(return_value=allowed)
    adapter._text_batch_delay_seconds = 0.6
    adapter._text_batch_split_delay_seconds = 2.0
    adapter._bot_tag_debounce_until = {}
    adapter._discord_bots_require_inline_mention = Mock(return_value=True)
    adapter._allowed_role_ids = set()
    adapter._warn_if_fail_closed_default = Mock()
    return adapter


def _message(adapter, *, content="", author_bot=False, roles=(), direct_user=False):
    guild = SimpleNamespace(self_role=SimpleNamespace(id=SELF_ROLE_ID))
    return SimpleNamespace(
        id=123,
        author=SimpleNamespace(id=42, bot=author_bot),
        channel=SimpleNamespace(id=10, parent_id=None),
        content=content,
        mentions=[adapter._client.user] if direct_user else [],
        role_mentions=[SimpleNamespace(id=role_id) for role_id in roles],
        guild=guild,
        type=discord.MessageType.default,
    )


def test_managed_role_admits_human_but_unrelated_role_does_not():
    adapter = _adapter()
    managed = _message(adapter, roles=[SELF_ROLE_ID])
    unrelated = _message(adapter, roles=[OTHER_ROLE_ID])
    raw_unrelated = _message(adapter, content=f"<@&{OTHER_ROLE_ID}>")
    direct = _message(adapter, direct_user=True)

    assert adapter._self_is_explicitly_mentioned(managed)
    assert not adapter._self_is_explicitly_mentioned(unrelated)
    assert not adapter._self_is_explicitly_mentioned(raw_unrelated)
    assert adapter._self_is_explicitly_mentioned(direct)


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_thread", [False, True])
async def test_managed_role_token_reaches_slash_command_dispatch(
    monkeypatch, auto_thread
):
    monkeypatch.setenv("DISCORD_AUTO_THREAD", str(auto_thread).lower())
    adapter, _runner = _make_discord_adapter_wired()
    adapter.handle_message = AsyncMock()
    adapter._discord_history_backfill = Mock(return_value=False)
    message = make_discord_message(content=f"<@&{SELF_ROLE_ID}> /help")
    message.guild.self_role = SimpleNamespace(id=SELF_ROLE_ID)
    if auto_thread:
        thread = make_fake_thread(thread_id=90001, name="help")
        original_content = message.content

        async def create_thread(**kwargs):
            message.content = original_content
            return thread

        message.create_thread = AsyncMock(side_effect=create_thread)

    assert await adapter._handle_message(message)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "/help"
    assert event.message_type.value == "command"
    if auto_thread:
        message.create_thread.assert_awaited_once()
        assert event.source.thread_id == "90001"
    else:
        assert message.content == "/help"


def test_bot_authored_managed_role_only_message_stays_rejected():
    adapter = _adapter()
    message = _message(
        adapter,
        content=f"<@&{SELF_ROLE_ID}> handoff",
        author_bot=True,
        roles=[SELF_ROLE_ID],
    )

    assert adapter._discord_message_admission(message, claim=False) == (False, False)


def test_managed_role_does_not_bypass_user_authorization():
    adapter = _adapter(allowed=False)
    message = _message(adapter, roles=[SELF_ROLE_ID])

    admitted, _ = adapter._discord_message_admission(message, claim=False)
    assert admitted is False


@pytest.mark.asyncio
async def test_managed_role_does_not_bypass_ignored_channel(monkeypatch):
    adapter, _runner = _make_discord_adapter_wired()
    adapter.handle_message = AsyncMock()
    adapter.config.extra["ignored_channels"] = ["22222"]
    message = make_discord_message(content=f"<@&{SELF_ROLE_ID}> /help")
    message.guild.self_role = SimpleNamespace(id=SELF_ROLE_ID)

    assert not await adapter._handle_message(message)
    adapter.handle_message.assert_not_awaited()
