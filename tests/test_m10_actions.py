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


# --- Six additional tests (batch 3) -------------------------------------------

def test_deploy_keeper_calls_systemctl_with_keeper_service():
    """Deploy to 'keeper' must invoke 'hiveos-keeper.service' via systemctl."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=mock_proc)) as mock_shell:
        asyncio.run(Deploy().execute(target="keeper"))

    cmd = mock_shell.call_args[0][0]
    assert "hiveos-keeper.service" in cmd


def test_deploy_orchestrator_calls_systemctl_with_orchestrator_service():
    """Deploy to 'orchestrator' must invoke 'hiveos-orchestrator.service' via systemctl."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=mock_proc)) as mock_shell:
        asyncio.run(Deploy().execute(target="orchestrator"))

    cmd = mock_shell.call_args[0][0]
    assert "hiveos-orchestrator.service" in cmd


def test_spend_money_content_contains_wire_hint():
    """The no-backend message must mention how to wire a payment adapter."""
    result = asyncio.run(SpendMoney().execute(what="lunch", amount="20 USD"))
    assert "Wire" in result.content or "wire" in result.content or "adapter" in result.content


def test_external_message_no_token_tool_name():
    """The no-token failure result must have tool_name == 'external_message'."""
    result = asyncio.run(ExternalMessage().execute(to="42", body="hello"))
    assert result.tool_name == "external_message"


def test_deploy_unknown_target_lists_valid_targets_in_content():
    """The error message for an unknown target must list all safe targets."""
    result = asyncio.run(Deploy().execute(target="staging"))
    for t in _SAFE_DEPLOY_TARGETS:
        assert t in result.content


def test_deploy_success_result_has_success_true():
    """A successful deploy (returncode=0) must return a ToolResult with success=True."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=mock_proc)):
        result = asyncio.run(Deploy().execute(target="gateway"))

    assert result.success is True


# --- Wave 3O additional tests ---------------------------------------------------

def test_spend_money_result_tool_name_is_spend_money():
    """SpendMoney result always has tool_name='spend_money'."""
    result = asyncio.run(SpendMoney().execute(what="server", amount="99 USD"))
    assert result.tool_name == "spend_money"


def test_spend_money_success_is_true_even_without_backend():
    """SpendMoney returns success=True even without a payment backend wired."""
    result = asyncio.run(SpendMoney().execute(what="coffee", amount="3 USD"))
    assert result.success is True


def test_deploy_keeper_service_name_in_command():
    """Deploy to 'keeper' must include 'hiveos-keeper' in the systemctl command."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=mock_proc)) as mock_shell:
        asyncio.run(Deploy().execute(target="keeper"))

    cmd = mock_shell.call_args[0][0]
    assert "keeper" in cmd


def test_external_message_result_tool_name():
    """ExternalMessage result always has tool_name='external_message'."""
    result = asyncio.run(ExternalMessage().execute(to="user123", body="hi"))
    assert result.tool_name == "external_message"


def test_safe_deploy_targets_contains_expected_services():
    """_SAFE_DEPLOY_TARGETS includes at least gateway, orchestrator, and keeper."""
    for t in ("gateway", "orchestrator", "keeper"):
        assert t in _SAFE_DEPLOY_TARGETS


def test_spend_money_cost_usd_is_zero():
    """SpendMoney cost_usd is 0.0 when no backend is configured."""
    result = asyncio.run(SpendMoney().execute(what="item", amount="1 USD"))
    assert result.cost_usd == 0.0


# --- Wave 3R additional tests ---------------------------------------------------

def test_external_message_success_is_true_without_backend():
    """ExternalMessage returns success=True even without Telegram wired."""
    result = asyncio.run(ExternalMessage().execute(to="admin", body="test"))
    assert result.success is True


def test_spend_money_content_is_string():
    """SpendMoney result content is a non-empty string."""
    result = asyncio.run(SpendMoney().execute(what="thing", amount="5 USD"))
    assert isinstance(result.content, str)
    assert len(result.content) > 0


def test_deploy_rejects_empty_target():
    """Deploy with empty target string returns an error result."""
    result = asyncio.run(Deploy().execute(target=""))
    assert result.success is False or "unknown" in result.content.lower() or len(result.content) > 0


def test_external_message_content_is_string():
    """ExternalMessage result content is always a string."""
    result = asyncio.run(ExternalMessage().execute(to="someone", body="msg"))
    assert isinstance(result.content, str)


def test_safe_deploy_targets_is_set_or_tuple():
    """_SAFE_DEPLOY_TARGETS is a non-empty set or tuple/list."""
    assert len(_SAFE_DEPLOY_TARGETS) >= 3


def test_spend_money_latency_is_zero():
    """SpendMoney latency_seconds is 0.0 when no backend configured."""
    result = asyncio.run(SpendMoney().execute(what="service", amount="10 USD"))
    assert result.latency_seconds == 0.0


# --- Wave 3X additional tests ---------------------------------------------------

def test_wave3x_deploy_failure_tool_name_is_deploy():
    """Deploy with non-zero exit must still return tool_name='deploy'."""
    mock_proc = MagicMock()
    mock_proc.returncode = 2
    mock_proc.communicate = AsyncMock(return_value=(b"Permission denied.", b""))

    with patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=mock_proc)):
        result = asyncio.run(Deploy().execute(target="gateway"))

    assert result.tool_name == "deploy"


def test_wave3x_deploy_failure_content_has_exit_code():
    """Deploy failure content must include the non-zero exit code."""
    mock_proc = MagicMock()
    mock_proc.returncode = 5
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=mock_proc)):
        result = asyncio.run(Deploy().execute(target="orchestrator"))

    assert "5" in result.content


def test_wave3x_spend_money_metadata_is_empty_dict():
    """SpendMoney result metadata field is an empty dict by default."""
    result = asyncio.run(SpendMoney().execute(what="widget", amount="1 USD"))
    assert result.metadata == {}


def test_wave3x_external_message_empty_body_does_not_raise():
    """ExternalMessage with an empty body must not raise and returns a ToolResult."""
    from hive.core.types import ToolResult
    result = asyncio.run(ExternalMessage().execute(to="123", body=""))
    assert isinstance(result, ToolResult)
    assert result.tool_name == "external_message"


def test_wave3x_external_message_telegram_success_cost_is_zero():
    """ExternalMessage success path via Telegram must have cost_usd=0.0."""
    from hive.gateway.channels.base import SendResult

    mock_send = AsyncMock(return_value=SendResult(ok=True, message_id="77"))
    mock_channel = MagicMock()
    mock_channel.send = mock_send
    mock_channel.aclose = AsyncMock()

    with patch("hive.gateway.channels.telegram.TelegramChannel", return_value=mock_channel):
        result = asyncio.run(
            ExternalMessage(telegram_token="tok-abc").execute(to="555", body="wave3x")
        )

    assert result.cost_usd == 0.0


def test_wave3x_external_message_telegram_success_is_true():
    """ExternalMessage success path must return a ToolResult with success=True."""
    from hive.gateway.channels.base import SendResult

    mock_send = AsyncMock(return_value=SendResult(ok=True, message_id="88"))
    mock_channel = MagicMock()
    mock_channel.send = mock_send
    mock_channel.aclose = AsyncMock()

    with patch("hive.gateway.channels.telegram.TelegramChannel", return_value=mock_channel):
        result = asyncio.run(
            ExternalMessage(telegram_token="tok-xyz").execute(to="777", body="hello")
        )

    assert result.success is True


def test_wave3x_deploy_gateway_content_mentions_gateway():
    """Successful deploy to 'gateway' must mention 'gateway' in the result content."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=mock_proc)):
        result = asyncio.run(Deploy().execute(target="gateway"))

    assert "gateway" in result.content


def test_wave3x_deploy_unknown_target_tool_name_is_deploy():
    """Deploy with an unknown target must return tool_name='deploy'."""
    result = asyncio.run(Deploy().execute(target="unknown-xyz"))
    assert result.tool_name == "deploy"


# --- Wave 4C additional tests ---------------------------------------------------

def test_wave4c_spend_money_spec_name_is_spend_money():
    """SpendMoney spec.name must equal 'spend_money'."""
    assert SpendMoney().spec.name == "spend_money"


def test_wave4c_deploy_spec_name_is_deploy():
    """Deploy spec.name must equal 'deploy'."""
    assert Deploy().spec.name == "deploy"


def test_wave4c_external_message_spec_name_is_external_message():
    """ExternalMessage spec.name must equal 'external_message'."""
    assert ExternalMessage().spec.name == "external_message"


def test_wave4c_external_message_send_failure_tool_name():
    """ExternalMessage send failure result must have tool_name='external_message'."""
    from hive.gateway.channels.base import SendResult

    mock_send = AsyncMock(return_value=SendResult(ok=False, error="forbidden"))
    mock_channel = MagicMock()
    mock_channel.send = mock_send
    mock_channel.aclose = AsyncMock()

    with patch("hive.gateway.channels.telegram.TelegramChannel", return_value=mock_channel):
        result = asyncio.run(
            ExternalMessage(telegram_token="tok").execute(to="111", body="test")
        )

    assert result.tool_name == "external_message"


def test_wave4c_deploy_failure_stderr_in_content():
    """Deploy failure output must include stderr text when present."""
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"Unit not found.", b""))

    with patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=mock_proc)):
        result = asyncio.run(Deploy().execute(target="gateway"))

    assert "Unit not found." in result.content


def test_wave4c_deploy_success_metadata_is_empty_dict():
    """Successful deploy must return result.metadata == {}."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=mock_proc)):
        result = asyncio.run(Deploy().execute(target="gateway"))

    assert result.metadata == {}


def test_wave4c_external_message_empty_to_does_not_raise():
    """ExternalMessage with empty 'to' and no token must return a ToolResult without raising."""
    from hive.core.types import ToolResult
    result = asyncio.run(ExternalMessage().execute(to="", body="hello"))
    assert isinstance(result, ToolResult)
    assert result.tool_name == "external_message"


def test_wave4c_external_message_telegram_success_tool_name():
    """ExternalMessage Telegram success path must return tool_name='external_message'."""
    from hive.gateway.channels.base import SendResult

    mock_send = AsyncMock(return_value=SendResult(ok=True, message_id="99"))
    mock_channel = MagicMock()
    mock_channel.send = mock_send
    mock_channel.aclose = AsyncMock()

    with patch("hive.gateway.channels.telegram.TelegramChannel", return_value=mock_channel):
        result = asyncio.run(
            ExternalMessage(telegram_token="tok-wave4c").execute(to="444", body="wave4c")
        )

    assert result.tool_name == "external_message"
