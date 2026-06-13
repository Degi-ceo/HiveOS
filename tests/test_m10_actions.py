"""
test_m10_actions.py — M10-b: un-stubbed action tools.

All tests are offline (mocked httpx / subprocess). Verifies:
  - ExternalMessage sends via TelegramChannel when token is set, or returns
    a clear capability-absent message when it is not.
  - Deploy rejects unknown targets; calls systemctl for known safe targets.
  - SpendMoney returns an honest capability-absent message (no payment backend).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from hive.tools.builtins import Deploy, ExternalMessage, SpendMoney, _SAFE_DEPLOY_TARGETS


# ---------------------------------------------------------------------------
# SpendMoney
# ---------------------------------------------------------------------------

def test_spend_money_no_backend():
    result = asyncio.run(SpendMoney().execute(what="pizza", amount="10 GBP"))
    assert result.tool_name == "spend_money"
    assert "no payment backend" in result.content
    assert "10 GBP" in result.content
    assert "pizza" in result.content


def test_spend_money_still_gated():
    assert SpendMoney().spec.dangerous is True


# ---------------------------------------------------------------------------
# Deploy — unknown target
# ---------------------------------------------------------------------------

def test_deploy_unknown_target():
    result = asyncio.run(Deploy().execute(target="production"))
    assert "unknown target" in result.content
    assert "production" in result.content


def test_deploy_safe_targets_set():
    assert "gateway" in _SAFE_DEPLOY_TARGETS
    assert "orchestrator" in _SAFE_DEPLOY_TARGETS
    assert "keeper" in _SAFE_DEPLOY_TARGETS


def test_deploy_unknown_not_in_safe():
    assert "production" not in _SAFE_DEPLOY_TARGETS
    assert "web" not in _SAFE_DEPLOY_TARGETS


def test_deploy_still_gated():
    assert Deploy().spec.dangerous is True


def test_deploy_known_target_calls_systemctl():
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=mock_proc)) as mock_shell:
        result = asyncio.run(Deploy().execute(target="gateway"))

    mock_shell.assert_called_once()
    call_cmd = mock_shell.call_args[0][0]
    assert "systemctl" in call_cmd
    assert "hiveos-gateway.service" in call_cmd
    assert "ok" in result.content


def test_deploy_non_zero_exit_reports_error():
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"Unit not found.", b""))

    with patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=mock_proc)):
        result = asyncio.run(Deploy().execute(target="orchestrator"))

    assert "exit 1" in result.content


# ---------------------------------------------------------------------------
# ExternalMessage — no token
# ---------------------------------------------------------------------------

def test_external_message_no_token():
    result = asyncio.run(ExternalMessage().execute(to="12345", body="hello"))
    assert "TELEGRAM_BOT_TOKEN not set" in result.content


def test_external_message_still_gated():
    assert ExternalMessage().spec.dangerous is True


# ---------------------------------------------------------------------------
# ExternalMessage — with token, mocked Telegram API
# ---------------------------------------------------------------------------

def test_external_message_sends_via_telegram():
    from hive.gateway.channels.base import SendResult

    mock_send = AsyncMock(return_value=SendResult(ok=True, message_id="42"))
    mock_channel = MagicMock()
    mock_channel.send = mock_send
    mock_channel.aclose = AsyncMock()

    with patch("hive.gateway.channels.telegram.TelegramChannel", return_value=mock_channel):
        result = asyncio.run(
            ExternalMessage(telegram_token="bot123").execute(to="99999", body="test message")
        )

    mock_send.assert_awaited_once()
    sent_msg = mock_send.call_args[0][0]
    assert sent_msg.chat_id == "99999"
    assert sent_msg.text == "test message"
    assert "sent to 99999" in result.content
    assert "42" in result.content


def test_external_message_reports_send_failure():
    from hive.gateway.channels.base import SendResult

    mock_send = AsyncMock(return_value=SendResult(ok=False, error="chat not found"))
    mock_channel = MagicMock()
    mock_channel.send = mock_send
    mock_channel.aclose = AsyncMock()

    with patch("hive.gateway.channels.telegram.TelegramChannel", return_value=mock_channel):
        result = asyncio.run(
            ExternalMessage(telegram_token="bot123").execute(to="99999", body="hi")
        )

    assert "send failed" in result.content
    assert "chat not found" in result.content
