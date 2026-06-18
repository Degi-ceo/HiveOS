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


# ---------------------------------------------------------------------------
# Additional coverage
# ---------------------------------------------------------------------------

def test_spend_money_result_is_tool_result():
    """execute() must return a ToolResult, not a plain string."""
    from hive.core.types import ToolResult
    result = asyncio.run(SpendMoney().execute(what="coffee", amount="5 GBP"))
    assert isinstance(result, ToolResult)


def test_spend_money_empty_args_does_not_raise():
    """Missing args should not crash the execute() method."""
    result = asyncio.run(SpendMoney().execute())
    assert result.tool_name == "spend_money"
    assert "no payment backend" in result.content


def test_deploy_result_is_tool_result_for_unknown_target():
    """Deploy with an unknown target must return a ToolResult (not raise)."""
    from hive.core.types import ToolResult
    result = asyncio.run(Deploy().execute(target="unknown-svc"))
    assert isinstance(result, ToolResult)


def test_deploy_missing_target_arg_returns_unknown():
    """Calling Deploy.execute() with no arguments should get the unknown-target message."""
    result = asyncio.run(Deploy().execute())
    assert "unknown target" in result.content


def test_external_message_result_is_tool_result():
    """ExternalMessage must return a ToolResult (no token case)."""
    from hive.core.types import ToolResult
    result = asyncio.run(ExternalMessage().execute(to="1", body="hi"))
    assert isinstance(result, ToolResult)


def test_all_gated_tools_have_dangerous_spec():
    """SpendMoney, Deploy, ExternalMessage must all have dangerous=True."""
    for tool in (SpendMoney(), Deploy(), ExternalMessage()):
        assert tool.spec.dangerous is True, f"{tool.spec.name} should be dangerous"


def test_safe_deploy_targets_has_exactly_three():
    """The safe targets set must have exactly three known entries."""
    assert len(_SAFE_DEPLOY_TARGETS) == 3


# --- New tests (batch 2) -------------------------------------------------------

def test_spend_money_tool_name_in_result():
    """execute() result must have tool_name == 'spend_money'."""
    result = asyncio.run(SpendMoney().execute(what="book", amount="12 USD"))
    assert result.tool_name == "spend_money"


def test_deploy_empty_target_treated_as_unknown():
    """Deploy with an empty string target must return the unknown-target message."""
    result = asyncio.run(Deploy().execute(target=""))
    assert "unknown target" in result.content


def test_deploy_valid_targets_sorted_in_error_message():
    """Unknown-target error must list the valid targets in sorted order."""
    result = asyncio.run(Deploy().execute(target="bogus"))
    # The valid targets must appear in sorted alphabetical order in the message
    msg = result.content
    gateway_pos = msg.find("gateway")
    keeper_pos = msg.find("keeper")
    orchestrator_pos = msg.find("orchestrator")
    assert gateway_pos < keeper_pos < orchestrator_pos


def test_deploy_success_includes_target_name_in_output():
    """Successful deploy output must mention the target service name."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=mock_proc)):
        result = asyncio.run(Deploy().execute(target="keeper"))

    assert "hiveos-keeper" in result.content


def test_external_message_tool_name_in_result():
    """ExternalMessage execute() must set tool_name == 'external_message'."""
    result = asyncio.run(ExternalMessage().execute(to="1", body="hi"))
    assert result.tool_name == "external_message"


def test_spend_money_success_field_is_true():
    """SpendMoney execute() must return result with success=True (capability absent, not an error)."""
    result = asyncio.run(SpendMoney().execute(what="tea", amount="2 GBP"))
    assert result.success is True
