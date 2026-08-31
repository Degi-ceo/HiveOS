from dataclasses import replace

from hive.core.config import HiveConfig
from hive.gateway.telegram_readiness import report


def _config(**changes) -> HiveConfig:
    return replace(HiveConfig.from_env(load_dotenv=False), **changes)


def test_report_is_fail_closed_and_never_exposes_values():
    token = "token-that-must-not-appear"
    secret = "invalid secret"
    config = _config(
        telegram_token=token,
        telegram_webhook_secret=secret,
        telegram_allowed_user_ids=frozenset({"123456"}),
        telegram_allowed_chat_ids=frozenset({"654321"}),
    )

    result = report(config)

    assert result["ingress_ready"] is False
    assert result["webhook_secret_format_valid"] is False
    assert result["allowed_user_count"] == 1
    assert result["allowed_chat_restriction_configured"] is True
    assert result["remediation"] == ["telegram_webhook_secret_invalid"]
    assert token not in str(result)
    assert secret not in str(result)
    assert "123456" not in str(result)
    assert "654321" not in str(result)


def test_report_requires_token_secret_and_allowed_user():
    result = report(_config())

    assert result["ingress_ready"] is False
    assert result["remediation"] == [
        "telegram_token_missing",
        "telegram_webhook_secret_missing",
        "telegram_allowed_users_missing",
    ]


def test_report_marks_complete_local_ingress_configuration_ready():
    result = report(_config(
        telegram_token="test-token",
        telegram_webhook_secret="safe_webhook_secret-123",
        telegram_allowed_user_ids=frozenset({"7", "8"}),
    ))

    assert result["ingress_ready"] is True
    assert result["allowed_user_count"] == 2
    assert result["remediation"] == []
    assert result["outbound_delivery_tested"] is False
    assert result["remote_webhook_verified"] is False
