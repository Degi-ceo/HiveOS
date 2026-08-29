"""SPRINT_7 Batch F — Budget forecast, /budget/forecast endpoint, Telegram alert."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from starlette.testclient import TestClient

from hive.autonomy.budget_alert import BudgetAlert
from hive.core.budgeter import Budgeter, ForecastResult
from hive.core.config import HiveConfig
from hive.gateway.app import create_app
from hive.gateway.channels.base import OutgoingMessage, SendResult
from hive.runtime import HiveOS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ScriptRouter:
    def __init__(self, script=None):
        self._script = list(script or [])

    async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
        from hive.llm.adapters.base import CompletionResult
        item = self._script.pop(0) if self._script else CompletionResult(text="ok", model="m")
        return item if isinstance(item, CompletionResult) else CompletionResult(text=item, model="m")

    async def aclose(self):
        pass


def _hive(tmp_path, script=None) -> HiveOS:
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return HiveOS.build(cfg, router=_ScriptRouter(script))


def _client(hive) -> TestClient:
    return TestClient(create_app(hive))


_TOKEN = {"X-Hive-Token": "change_me"}


def _drive(b: Budgeter, days_costs):
    """Simulate `days_costs` days passing by mutating history directly.

    Also resets _cost_today_usd and _day to today so the forecast uses the
    history buffer + a synthetic today cost of 0.0. This keeps the test
    deterministic regardless of wall-clock time.
    """
    import time as _time
    from collections import deque
    b._day = _time.strftime("%Y-%m-%d", _time.localtime(b._clock()))
    b._cost_today_usd = 0.0
    b._calls_today = 0
    b._daily_history = deque(days_costs, maxlen=b._history_window)


# ---------------------------------------------------------------------------
# F1.1 — Budgeter.forecast_spend()
# ---------------------------------------------------------------------------


def test_forecast_empty_history_returns_safe_defaults():
    """Fresh budgeter with no spend: safe defaults, status=ok, days_until_cap=None."""
    b = Budgeter(daily_cap=3000, daily_spend_cap_usd=3000)
    f = b.forecast_spend(days=7)
    assert isinstance(f, ForecastResult)
    assert f.status == "ok"
    assert f.days_until_cap is None
    assert f.projected_total == 0.0
    assert f.daily_avg == 0.0
    assert f.max_daily == 0.0
    assert f.confidence == 0.0


def test_forecast_constant_rate_projects_linearly():
    """Constant $1/day history → forecast(7) projects +$7 from today."""
    b = Budgeter(daily_cap=3000, daily_spend_cap_usd=3000)
    _drive(b, [1.0] * 6)  # 6 past days @ $1 → avg from history = 1.0
    b._cost_today_usd = 1.0  # today: $1 spent so far
    f = b.forecast_spend(days=7)
    assert f.daily_avg == 1.0
    assert f.max_daily == 1.0
    # current_spend (1) + daily_avg * 7 (7) = 8
    assert f.projected_total == 8.0
    # ($3000 - $1) / $1/day = 2999 days until the USD spend cap
    assert f.days_until_cap == 2999
    assert f.status == "ok"


def test_forecast_bursty_rate_uses_max_daily_for_worst_case():
    """A single $50 day in history should be visible in max_daily."""
    b = Budgeter(daily_cap=3000)
    _drive(b, [1.0, 1.0, 50.0, 1.0, 1.0, 1.0])  # one burst in history
    b._cost_today_usd = 1.0
    f = b.forecast_spend(days=7)
    assert f.max_daily == 50.0
    # avg over the 6 history samples = (1+1+50+1+1+1)/6 = 55/6
    assert abs(f.daily_avg - (55.0 / 6.0)) < 1e-9


def test_forecast_does_not_compare_usd_spend_with_call_count_cap():
    """Without an explicit USD cap, cost telemetry remains informational."""
    b = Budgeter(daily_cap=100)
    _drive(b, [30.0] * 6)
    b._cost_today_usd = 150.0
    f = b.forecast_spend(days=7)
    assert f.days_until_cap is None
    assert f.status == "ok"


def test_forecast_days_until_cap_when_under_cap():
    """Spending $1/day, USD cap $100, current $50 → 50 days remaining."""
    b = Budgeter(daily_cap=3000, daily_spend_cap_usd=100)
    _drive(b, [1.0] * 6)
    b._cost_today_usd = 50.0
    f = b.forecast_spend(days=7)
    assert f.days_until_cap == 50
    assert f.status == "ok"


def test_forecast_days_until_cap_when_over_cap():
    """Today's cost already exceeds the USD cap → status=exceeded."""
    b = Budgeter(daily_cap=3000, daily_spend_cap_usd=100)
    _drive(b, [1.0] * 6)
    b._cost_today_usd = 150.0   # already past the cap
    f = b.forecast_spend(days=7)
    assert f.days_until_cap == 0
    assert f.status == "exceeded"


def test_forecast_status_warn_when_1_to_3_days():
    """days_until_cap in (1, 3] → status=warn."""
    b = Budgeter(daily_cap=3000, daily_spend_cap_usd=100)
    # avg = 30/day → cap (100) - current (0) = 100 / 30 ≈ 3.33 → ceil=4 → ok
    # want exactly 2 → current = 100 - 60 = 40 (avg 30 → ceil(60/30)=2)
    _drive(b, [30.0, 30.0, 30.0, 30.0, 30.0, 30.0])
    b._cost_today_usd = 40.0
    f = b.forecast_spend(days=7)
    assert f.days_until_cap == 2
    assert f.status == "warn"


def test_forecast_status_critical_when_le_1_day():
    """days_until_cap == 1 → status=critical."""
    b = Budgeter(daily_cap=3000, daily_spend_cap_usd=100)
    _drive(b, [30.0] * 6)
    # avg 30, current 70 → remaining 30 / 30 = 1.0 → ceil = 1 → critical
    b._cost_today_usd = 70.0
    f = b.forecast_spend(days=7)
    assert f.days_until_cap == 1
    assert f.status == "critical"


def test_forecast_confidence_high_for_constant_history():
    """Constant history → stddev=0 → confidence=1.0."""
    b = Budgeter(daily_cap=3000)
    _drive(b, [1.0] * 6)
    b._cost_today_usd = 1.0
    f = b.forecast_spend(days=7)
    assert f.confidence == 1.0


def test_forecast_result_to_dict_is_json_safe():
    """ForecastResult.to_dict returns a plain dict with no Decimal/datetime."""
    b = Budgeter(daily_cap=3000)
    _drive(b, [1.0, 2.0, 3.0])
    b._cost_today_usd = 1.5
    d = b.forecast_spend(days=14).to_dict()
    assert set(d.keys()) == {
        "projected_total", "daily_avg", "max_daily",
        "days_until_cap", "status", "confidence",
    }
    # Must serialise to JSON without TypeError (no Decimal, datetime, etc.).
    json.dumps(d)


def test_forecast_history_persists_to_disk(tmp_path):
    """history_path causes daily_history to be loaded on construction."""
    hist_file = tmp_path / "hist.json"
    hist_file.write_text(json.dumps([5.0, 5.0, 5.0]))
    b = Budgeter(daily_cap=3000, history_path=str(hist_file))
    # The history buffer should now contain the loaded values.
    assert list(b._daily_history) == [5.0, 5.0, 5.0]


# ---------------------------------------------------------------------------
# F1.2 — /budget/forecast endpoint
# ---------------------------------------------------------------------------


def test_get_budget_forecast_endpoint_returns_forecast_dict(tmp_path):
    """GET /budget/forecast returns the spend-forecast shape."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/budget/forecast", headers=_TOKEN)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict)
    assert set(body.keys()) == {
        "projected_total", "daily_avg", "max_daily",
        "days_until_cap", "status", "confidence",
    }


def test_get_budget_forecast_endpoint_accepts_days_query(tmp_path):
    """GET /budget/forecast?days=14 returns the same shape (custom horizon)."""
    with _client(_hive(tmp_path)) as c:
        r = c.get("/budget/forecast?days=14", headers=_TOKEN)
    assert r.status_code == 200
    assert "projected_total" in r.json()


def test_get_budget_forecast_endpoint_clamps_days(tmp_path):
    """days <= 0 is clamped to 1, days > 365 clamps to 365 (no crash)."""
    with _client(_hive(tmp_path)) as c:
        r0 = c.get("/budget/forecast?days=0", headers=_TOKEN)
        rneg = c.get("/budget/forecast?days=-5", headers=_TOKEN)
        rhuge = c.get("/budget/forecast?days=99999", headers=_TOKEN)
    assert r0.status_code == 200
    assert rneg.status_code == 200
    assert rhuge.status_code == 200


# ---------------------------------------------------------------------------
# F1.3 — Telegram alert
# ---------------------------------------------------------------------------


class _FakeTelegram:
    def __init__(self):
        self.sent: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> SendResult:
        self.sent.append(message)
        return SendResult(ok=True, message_id="42")


class _FakeHive:
    """Minimal stand-in for HiveOS — only config + budgeter are exercised."""

    def __init__(self, budgeter, *, threshold_days: int = 1, telegram_token: str = "",
                 chat_id: str = "999"):
        class _Cfg:
            pass
        cfg = _Cfg()
        cfg.budget_forecast_alert_days = threshold_days
        cfg.telegram_token = telegram_token
        cfg.telegram_admin_chat_id = chat_id
        self.config = cfg
        self.budgeter = budgeter


def test_check_budget_alert_sends_on_transition():
    """ok → warn transition fires a Telegram message exactly once."""
    tg = _FakeTelegram()
    b = Budgeter(daily_cap=3000, daily_spend_cap_usd=100)
    # avg 30/day, current 40 → days_until_cap=2 → warn
    b._daily_history.extend([30.0, 30.0, 30.0, 30.0, 30.0, 30.0])
    b._cost_today_usd = 40.0
    hive = _FakeHive(b, threshold_days=3)
    alert = BudgetAlert(hive, telegram=tg, chat_id="999")

    sent1 = asyncio.run(alert.check())
    assert sent1 is True
    assert len(tg.sent) == 1
    assert "WARN" in tg.sent[0].text
    assert alert.last_status == "warn"


def test_check_budget_alert_no_spam_on_same_status():
    """Re-checking with the same status does NOT re-send."""
    tg = _FakeTelegram()
    b = Budgeter(daily_cap=3000, daily_spend_cap_usd=100)
    b._daily_history.extend([30.0, 30.0, 30.0, 30.0, 30.0, 30.0])
    b._cost_today_usd = 40.0
    hive = _FakeHive(b, threshold_days=3)
    alert = BudgetAlert(hive, telegram=tg, chat_id="999")

    asyncio.run(alert.check())          # transition ok → warn → send
    sent2 = asyncio.run(alert.check())  # status stays warn → no send
    assert sent2 is False
    assert len(tg.sent) == 1


def test_check_budget_alert_no_send_when_ok():
    """status=ok never fires, even on first check."""
    tg = _FakeTelegram()
    b = Budgeter(daily_cap=3000)
    hive = _FakeHive(b, threshold_days=1)
    alert = BudgetAlert(hive, telegram=tg, chat_id="999")
    sent = asyncio.run(alert.check())
    assert sent is False
    assert tg.sent == []


def test_check_budget_alert_threshold_blocks_short_horizons():
    """threshold_days=1 ignores 'warn' with days_until_cap=2."""
    tg = _FakeTelegram()
    b = Budgeter(daily_cap=3000, daily_spend_cap_usd=100)
    b._daily_history.extend([30.0] * 6)
    b._cost_today_usd = 40.0   # days_until_cap=2
    hive = _FakeHive(b, threshold_days=1)  # only alert when <= 1 day
    alert = BudgetAlert(hive, telegram=tg, chat_id="999")
    sent = asyncio.run(alert.check())
    assert sent is False
    assert tg.sent == []


def test_check_budget_alert_falls_back_to_log_when_no_telegram():
    """Without a telegram channel, alerts are no-ops (log only)."""
    b = Budgeter(daily_cap=3000, daily_spend_cap_usd=100)
    b._daily_history.extend([30.0] * 6)
    b._cost_today_usd = 40.0
    hive = _FakeHive(b, threshold_days=3)
    alert = BudgetAlert(hive, telegram=None, chat_id="999")
    sent = asyncio.run(alert.check())
    assert sent is False


# ---------------------------------------------------------------------------
# F1.4 — Config field
# ---------------------------------------------------------------------------


def test_config_alert_days_default_1(tmp_path, monkeypatch):
    """Default HIVE_BUDGET_FORECAST_ALERT_DAYS=1 when env unset."""
    monkeypatch.delenv("HIVE_BUDGET_FORECAST_ALERT_DAYS", raising=False)
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert cfg.budget_forecast_alert_days == 1


def test_config_alert_days_env_override(tmp_path, monkeypatch):
    """HIVE_BUDGET_FORECAST_ALERT_DAYS=5 overrides the default."""
    monkeypatch.setenv("HIVE_BUDGET_FORECAST_ALERT_DAYS", "5")
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert cfg.budget_forecast_alert_days == 5


def test_config_daily_spend_cap_defaults_disabled_and_accepts_usd_value(tmp_path, monkeypatch):
    monkeypatch.delenv("HIVE_DAILY_SPEND_CAP_USD", raising=False)
    assert HiveConfig.from_env(root=tmp_path, load_dotenv=False).budget_daily_spend_cap_usd == 0.0
    monkeypatch.setenv("HIVE_DAILY_SPEND_CAP_USD", "42.50")
    assert HiveConfig.from_env(root=tmp_path, load_dotenv=False).budget_daily_spend_cap_usd == 42.5


def test_config_validate_rejects_negative_alert_days(tmp_path, monkeypatch):
    """validate() flags a negative alert threshold."""
    monkeypatch.setenv("HIVE_BUDGET_FORECAST_ALERT_DAYS", "-1")
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    issues = cfg.validate()
    assert any("HIVE_BUDGET_FORECAST_ALERT_DAYS" in i for i in issues)
