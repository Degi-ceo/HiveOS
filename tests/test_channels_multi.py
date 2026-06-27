"""SPRINT_6 P-E — Slack / Discord / Email channel transports + gateway wiring."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time

import httpx
import nacl.signing
import pytest
from starlette.testclient import TestClient

from hive.core.config import HiveConfig
from hive.gateway.app import create_app
from hive.gateway.channels.base import ChannelAdapter, MessageEvent, OutgoingMessage, SendResult
from hive.gateway.channels.discord import DiscordChannel
from hive.gateway.channels.email import EmailChannel
from hive.gateway.channels.slack import SlackChannel


# --- shared helpers ------------------------------------------------------------

class _StubRouter:
    """Minimal router stub: `ask()` returns deterministic echo."""
    async def complete(self, messages, **kw):
        from hive.llm.adapters.base import CompletionResult
        return CompletionResult(text="ok", model="m")

    async def stream(self, messages, **kw):
        yield "ok"

    async def aclose(self):
        pass


def _build_hive(tmp_path, **env):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    if env:
        import dataclasses
        cfg = dataclasses.replace(cfg, **env)
    from hive.runtime import HiveOS
    hive = HiveOS.build(cfg, router=_StubRouter())
    hive.config = cfg
    return hive


def _sign_slack(secret: str, body: bytes, ts: int | None = None) -> dict[str, str]:
    ts = ts if ts is not None else int(time.time())
    base = f"v0:{ts}:".encode() + body
    digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": str(ts),
        "X-Slack-Signature": f"v0={digest}",
    }


def _sign_discord(body: bytes, ts: int | None = None) -> tuple[bytes, dict[str, str], str]:
    ts = ts if ts is not None else int(time.time())
    seed = nacl.signing.SigningKey(b"\x01" * 32)
    sig = seed.sign(str(ts).encode() + body).signature
    hex_sig = sig.hex()
    public_hex = seed.verify_key.encode().hex()
    headers = {
        "X-Signature-Ed25519": hex_sig,
        "X-Signature-Timestamp": str(ts),
    }
    return sig, headers, public_hex


def _msg_bytes(subject: str, body: str, *, from_: str = "Alice <alice@example.com>",
               msg_id: str | None = None, in_reply_to: str | None = None,
               html_body: str | None = None) -> bytes:
    raw = (
        f"From: {from_}\r\n"
        f"To: hive@example.com\r\n"
        f"Subject: {subject}\r\n"
    )
    if msg_id:
        raw += f"Message-ID: {msg_id}\r\n"
    if in_reply_to:
        raw += f"In-Reply-To: {in_reply_to}\r\n"
    if html_body:
        raw += (
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/alternative; boundary=BOUNDARY\r\n"
            "\r\n"
            "--BOUNDARY\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            f"{body}\r\n"
            "--BOUNDARY\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "\r\n"
            f"{html_body}\r\n"
            "--BOUNDARY--\r\n"
        )
    else:
        raw += f"\r\n{body}"
    return raw.encode()


# --- SlackChannel --------------------------------------------------------------

def test_slack_parse_url_verification_returns_none():
    assert SlackChannel("tok").parse_update({"type": "url_verification", "challenge": "abc"}) is None


def test_slack_parse_wrong_root_type_returns_none():
    assert SlackChannel("tok").parse_update({"type": "event_callback"}) is None


def test_slack_parse_other_root_type_returns_none():
    assert SlackChannel("tok").parse_update({"type": "block_actions"}) is None


def test_slack_parse_message_changed_returns_none():
    raw = {"type": "event_callback", "event": {"type": "message", "subtype": "message_changed", "text": "old", "channel": "C1", "user": "U1"}}
    assert SlackChannel("tok").parse_update(raw) is None


def test_slack_parse_missing_text_returns_none():
    raw = {"type": "event_callback", "event": {"type": "message", "channel": "C1", "user": "U1"}}
    assert SlackChannel("tok").parse_update(raw) is None


def test_slack_parse_non_dict_event_returns_none():
    raw = {"type": "event_callback", "event": "not-a-dict"}
    assert SlackChannel("tok").parse_update(raw) is None


def test_slack_parse_non_message_event_returns_none():
    raw = {"type": "event_callback", "event": {"type": "reaction_added", "channel": "C1"}}
    assert SlackChannel("tok").parse_update(raw) is None


def test_slack_parse_missing_channel_returns_none():
    raw = {"type": "event_callback", "event": {"type": "message", "text": "hi", "user": "U1"}}
    assert SlackChannel("tok").parse_update(raw) is None


def test_slack_parse_non_dict_returns_none():
    assert SlackChannel("tok").parse_update("not-a-dict") is None  # type: ignore[arg-type]


def test_slack_parse_valid_message():
    raw = {"type": "event_callback",
           "event": {"type": "message", "text": "hi", "channel": "C1", "user": "U1", "ts": "1700000000.000100"}}
    evt = SlackChannel("tok").parse_update(raw)
    assert isinstance(evt, MessageEvent)
    assert evt.text == "hi" and evt.chat_id == "C1" and evt.user_id == "U1"
    assert evt.message_id == "1700000000.000100" and evt.platform == "slack"


def test_slack_verify_signature_accepts_valid():
    body = b'{"type":"event_callback","event":{"type":"message"}}'
    headers = _sign_slack("secret", body)
    assert SlackChannel.verify_signature(headers, body, "secret") is True


def test_slack_verify_signature_rejects_wrong_sig():
    body = b"abc"
    headers = _sign_slack("secret", body)
    headers["X-Slack-Signature"] = "v0=deadbeef"
    assert SlackChannel.verify_signature(headers, body, "secret") is False


def test_slack_verify_signature_rejects_stale_timestamp():
    body = b"abc"
    headers = _sign_slack("secret", body, ts=int(time.time()) - 3600)
    assert SlackChannel.verify_signature(headers, body, "secret") is False


def test_slack_verify_signature_handles_missing_v0_prefix():
    body = b"abc"
    headers = _sign_slack("secret", body)
    headers["X-Slack-Signature"] = headers["X-Slack-Signature"][3:]  # strip v0=
    assert SlackChannel.verify_signature(headers, body, "secret") is True


def test_slack_verify_signature_rejects_missing_headers():
    assert SlackChannel.verify_signature({}, b"abc", "secret") is False


def test_slack_verify_signature_rejects_non_numeric_timestamp():
    body = b"abc"
    headers = {"X-Slack-Request-Timestamp": "NaN", "X-Slack-Signature": "v0=00"}
    assert SlackChannel.verify_signature(headers, body, "secret") is False


def test_slack_verify_signature_rejects_empty_secret():
    body = b"abc"
    headers = _sign_slack("secret", body)
    assert SlackChannel.verify_signature(headers, body, "") is False


def test_slack_send_calls_chat_postmessage():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"ok": True, "ts": "1.0"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ch = SlackChannel("xoxb-tok", client=client)
    res = asyncio.run(ch.send(OutgoingMessage(chat_id="C1", text="hi")))
    assert res.ok and res.message_id == "1.0"
    assert captured["url"].endswith("/chat.postMessage")
    assert captured["auth"] == "Bearer xoxb-tok"


def test_slack_send_includes_reply_to_thread_ts():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "ts": "2.0"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ch = SlackChannel("xoxb-tok", client=client)
    res = asyncio.run(ch.send(OutgoingMessage(chat_id="C1", text="hi", reply_to="1.0")))
    assert res.ok
    assert seen["payload"]["thread_ts"] == "1.0"


def test_slack_send_returns_error_on_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ch = SlackChannel("xoxb-tok", client=client)
    res = asyncio.run(ch.send(OutgoingMessage(chat_id="C0", text="hi")))
    assert not res.ok and "channel_not_found" in res.error


def test_slack_send_returns_error_on_network_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ch = SlackChannel("xoxb-tok", client=client)
    res = asyncio.run(ch.send(OutgoingMessage(chat_id="C0", text="hi")))
    assert not res.ok and "boom" in res.error


def test_slack_send_without_token_returns_error():
    ch = SlackChannel("")
    res = asyncio.run(ch.send(OutgoingMessage(chat_id="C0", text="hi")))
    assert not res.ok and "token" in res.error


def test_slack_aclose_closes_owned_client():
    ch = SlackChannel("tok")
    asyncio.run(ch.aclose())


def test_slack_aclose_does_not_close_injected_client():
    closed = {"x": False}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    real_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    orig = real_client.aclose
    async def track_close():
        closed["x"] = True
        await orig()
    real_client.aclose = track_close  # type: ignore[assignment]
    ch = SlackChannel("tok", client=real_client)
    asyncio.run(ch.aclose())
    assert closed["x"] is False


# --- DiscordChannel ------------------------------------------------------------

def test_discord_parse_ping_returns_none():
    assert DiscordChannel().parse_update({"t": 0, "d": {}}) is None


def test_discord_parse_wrong_type_returns_none():
    assert DiscordChannel().parse_update({"t": 1}) is None


def test_discord_parse_bot_message_returns_none():
    raw = {"t": 2, "d": {"content": "hi", "channel_id": "1",
                         "author": {"id": "99", "bot": True}}}
    assert DiscordChannel().parse_update(raw) is None


def test_discord_parse_empty_content_returns_none():
    raw = {"t": 2, "d": {"content": "", "channel_id": "1",
                         "author": {"id": "99", "bot": False}}}
    assert DiscordChannel().parse_update(raw) is None


def test_discord_parse_missing_channel_returns_none():
    raw = {"t": 2, "d": {"content": "hi", "author": {"id": "99", "bot": False}}}
    assert DiscordChannel().parse_update(raw) is None


def test_discord_parse_missing_author_returns_none():
    raw = {"t": 2, "d": {"content": "hi", "channel_id": "1"}}
    assert DiscordChannel().parse_update(raw) is None


def test_discord_parse_non_dict_data_returns_none():
    assert DiscordChannel().parse_update({"t": 2, "d": "not-a-dict"}) is None


def test_discord_parse_non_dict_returns_none():
    assert DiscordChannel().parse_update("not-a-dict") is None  # type: ignore[arg-type]


def test_discord_parse_valid_message():
    raw = {"t": 2, "d": {"content": "hi", "channel_id": "12345", "id": "999",
                         "author": {"id": "U1", "bot": False, "username": "alice"}}}
    evt = DiscordChannel().parse_update(raw)
    assert isinstance(evt, MessageEvent)
    assert evt.text == "hi" and evt.chat_id == "12345" and evt.user_id == "U1"
    assert evt.message_id == "999" and evt.platform == "discord"


def test_discord_verify_signature_accepts_valid():
    body = b"hello"
    _, headers, public_hex = _sign_discord(body)
    ch = DiscordChannel(public_key=public_hex)
    assert ch.verify_signature(headers, body) is True


def test_discord_verify_signature_rejects_invalid():
    body = b"hello"
    _, headers, public_hex = _sign_discord(body)
    headers["X-Signature-Ed25519"] = "00" * 64
    ch = DiscordChannel(public_key=public_hex)
    assert ch.verify_signature(headers, body) is False


def test_discord_verify_signature_rejects_stale_timestamp():
    body = b"hello"
    _, headers, public_hex = _sign_discord(body, ts=int(time.time()) - 3600)
    ch = DiscordChannel(public_key=public_hex)
    assert ch.verify_signature(headers, body) is False


def test_discord_verify_signature_rejects_missing_headers():
    body = b"hello"
    _, _, public_hex = _sign_discord(body)
    ch = DiscordChannel(public_key=public_hex)
    assert ch.verify_signature({}, body) is False


def test_discord_verify_signature_rejects_invalid_key():
    body = b"hello"
    _, headers, _ = _sign_discord(body)
    ch = DiscordChannel(public_key="00" * 32)
    assert ch.verify_signature(headers, body) is False


def test_discord_verify_signature_rejects_non_numeric_ts():
    body = b"hello"
    _, headers, public_hex = _sign_discord(body)
    headers["X-Signature-Timestamp"] = "NaN"
    ch = DiscordChannel(public_key=public_hex)
    assert ch.verify_signature(headers, body) is False


def test_discord_verify_signature_rejects_empty_default_key():
    body = b"hello"
    _, headers, _ = _sign_discord(body)
    ch = DiscordChannel()  # no key
    assert ch.verify_signature(headers, body) is False


def test_discord_verify_signature_uses_explicit_public_key():
    body = b"hello"
    _, headers, public_hex = _sign_discord(body)
    ch = DiscordChannel()  # no key in instance, but passed via arg
    assert ch.verify_signature(headers, body, public_key=public_hex) is True


def test_discord_send_uses_webhook_url():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(204, json={"id": "m1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ch = DiscordChannel(application_id="APP1", webhook_token="WH1", client=client)
    res = asyncio.run(ch.send(OutgoingMessage(chat_id="C1", text="hi")))
    assert res.ok
    assert "/webhooks/APP1/WH1" in captured["url"]
    assert captured["body"]["content"] == "hi"


def test_discord_send_uses_bot_token_when_no_webhook():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"id": "m1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ch = DiscordChannel(bot_token="bot-tok", client=client)
    res = asyncio.run(ch.send(OutgoingMessage(chat_id="C1", text="hi")))
    assert res.ok
    assert "/channels/C1/messages" in captured["url"]
    assert captured["auth"] == "Bot bot-tok"


def test_discord_send_returns_error_on_4xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Missing Permissions"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ch = DiscordChannel(bot_token="bot-tok", client=client)
    res = asyncio.run(ch.send(OutgoingMessage(chat_id="C1", text="hi")))
    assert not res.ok and "403" in res.error


def test_discord_send_returns_error_on_network_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("kaboom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ch = DiscordChannel(bot_token="bot-tok", client=client)
    res = asyncio.run(ch.send(OutgoingMessage(chat_id="C1", text="hi")))
    assert not res.ok and "kaboom" in res.error


def test_discord_send_without_credentials_returns_error():
    ch = DiscordChannel()
    res = asyncio.run(ch.send(OutgoingMessage(chat_id="C1", text="hi")))
    assert not res.ok and "credentials" in res.error


def test_discord_send_returns_error_on_invalid_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json", headers={"content-type": "application/json"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ch = DiscordChannel(bot_token="bot-tok", client=client)
    res = asyncio.run(ch.send(OutgoingMessage(chat_id="C1", text="hi")))
    assert not res.ok and "json" in res.error.lower()


def test_discord_aclose_closes_owned_client():
    asyncio.run(DiscordChannel().aclose())


def test_discord_aclose_does_not_close_injected_client():
    closed = {"x": False}
    real_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    orig = real_client.aclose
    async def track_close():
        closed["x"] = True
        await orig()
    real_client.aclose = track_close  # type: ignore[assignment]
    ch = DiscordChannel(client=real_client)
    asyncio.run(ch.aclose())
    assert closed["x"] is False


# --- EmailChannel --------------------------------------------------------------

def test_email_parse_extracts_subject_and_body():
    raw = _msg_bytes("hi there", "first line\nsecond line")
    evt = EmailChannel().parse_update({"raw_bytes": raw})
    assert evt is not None
    assert "hi there" in evt.text
    assert "first line" in evt.text


def test_email_parse_returns_none_for_empty_body():
    raw = _msg_bytes("only subject", "")
    assert EmailChannel().parse_update({"raw_bytes": raw}) is None


def test_email_parse_handles_multipart_first_text_plain_wins():
    raw = _msg_bytes("subj", "the plain text body", html_body="<p>html body</p>")
    evt = EmailChannel().parse_update({"raw_bytes": raw})
    assert evt is not None
    assert "plain text body" in evt.text
    assert "<p>" not in evt.text


def test_email_parse_falls_back_to_html_when_no_plain():
    raw = _msg_bytes("subj", "", html_body="<p>html only body</p>")
    evt = EmailChannel().parse_update({"raw_bytes": raw})
    assert evt is not None
    assert "html only body" in evt.text


def test_email_parse_multipart_no_text_parts_returns_none():
    raw = (
        b"From: a@b\r\nTo: c@d\r\nSubject: no text\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
        b"--B\r\n"
        b"Content-Type: application/octet-stream\r\n\r\n"
        b"binary data\r\n"
        b"--B--\r\n"
    )
    assert EmailChannel().parse_update({"raw_bytes": raw}) is None


def test_email_parse_uses_from_and_message_id():
    raw = _msg_bytes("subj", "body", from_="bob@x.com", msg_id="<m1@x.com>")
    evt = EmailChannel().parse_update({"raw_bytes": raw})
    assert evt is not None
    assert evt.user_id == "bob@x.com"
    assert evt.message_id == "<m1@x.com>"
    assert evt.platform == "email"


def test_email_parse_returns_none_for_missing_raw_bytes():
    assert EmailChannel().parse_update({}) is None
    assert EmailChannel().parse_update({"raw_bytes": "not-bytes"}) is None  # type: ignore[arg-type]


def test_email_parse_non_dict_returns_none():
    assert EmailChannel().parse_update("not-a-dict") is None  # type: ignore[arg-type]


def test_email_send_uses_smtp(monkeypatch):
    sent = {}

    async def fake_send(msg, **kw):
        sent["msg"] = msg
        sent["kw"] = kw

    monkeypatch.setattr("hive.gateway.channels.email.aiosmtplib.send", fake_send)
    ch = EmailChannel(smtp_host="smtp.x.com", smtp_user="u@x.com", smtp_pass="p", smtp_from="hive@x.com")
    res = asyncio.run(ch.send(OutgoingMessage(chat_id="bob@x.com", text="Hello\n\nbody line")))
    assert res.ok
    assert sent["kw"]["hostname"] == "smtp.x.com"
    assert sent["kw"]["port"] == 587
    assert sent["msg"]["From"] == "hive@x.com"
    assert sent["msg"]["To"] == "bob@x.com"
    assert sent["msg"]["Subject"] == "Hello"


def test_email_send_returns_error_on_smtp_failure(monkeypatch):
    async def fake_send(msg, **kw):
        raise RuntimeError("auth failed")

    monkeypatch.setattr("hive.gateway.channels.email.aiosmtplib.send", fake_send)
    ch = EmailChannel(smtp_host="smtp.x.com", smtp_user="u@x.com", smtp_pass="p", smtp_from="hive@x.com")
    res = asyncio.run(ch.send(OutgoingMessage(chat_id="bob@x.com", text="hi")))
    assert not res.ok and "auth failed" in res.error


def test_email_send_without_host_returns_error():
    ch = EmailChannel()
    res = asyncio.run(ch.send(OutgoingMessage(chat_id="bob@x.com", text="hi")))
    assert not res.ok and "host/from" in res.error


def test_email_send_includes_reply_headers(monkeypatch):
    sent = {}

    async def fake_send(msg, **kw):
        sent["msg"] = msg

    monkeypatch.setattr("hive.gateway.channels.email.aiosmtplib.send", fake_send)
    ch = EmailChannel(smtp_host="smtp.x.com", smtp_user="u@x.com", smtp_pass="p", smtp_from="hive@x.com")
    res = asyncio.run(ch.send(OutgoingMessage(chat_id="bob@x.com", text="hi", reply_to="<old@x.com>")))
    assert res.ok
    assert sent["msg"]["In-Reply-To"] == "<old@x.com>"
    assert sent["msg"]["References"] == "<old@x.com>"


def test_email_verify_signature_always_true():
    # v1: gateway authenticates POSTs via X-Webhook-Secret; DKIM out of scope.
    assert EmailChannel().verify_signature({}, b"", "anything") is True


def test_email_aclose_noop():
    asyncio.run(EmailChannel().aclose())


# --- Gateway wiring ------------------------------------------------------------

def test_slack_webhook_bad_signature_returns_401(tmp_path):
    hive = _build_hive(tmp_path, slack_signing_secret="secret", slack_bot_token="xoxb")
    with TestClient(create_app(hive)) as c:
        r = c.post("/slack/webhook",
                   json={"type": "event_callback", "event": {"type": "message", "text": "hi",
                                                             "channel": "C1", "user": "U1"}})
        assert r.status_code == 401


def test_slack_webhook_url_verification_returns_challenge(tmp_path):
    hive = _build_hive(tmp_path, slack_signing_secret="secret")
    payload = {"type": "url_verification", "challenge": "abc123"}
    body = json.dumps(payload).encode()
    headers = _sign_slack("secret", body)
    with TestClient(create_app(hive)) as c:
        r = c.post("/slack/webhook", content=body, headers={**headers, "Content-Type": "application/json"})
        assert r.status_code == 200
        assert r.json() == {"challenge": "abc123"}


def test_discord_webhook_bad_signature_returns_401(tmp_path):
    hive = _build_hive(tmp_path, discord_public_key="ab" * 32)
    with TestClient(create_app(hive)) as c:
        r = c.post("/discord/webhook", json={"t": 2, "d": {}})
        assert r.status_code == 401


def test_discord_webhook_ping_returns_pong(tmp_path):
    _, _, public_hex = _sign_discord(b"")
    hive = _build_hive(tmp_path, discord_public_key=public_hex)
    body = json.dumps({"t": 0, "d": {}}).encode()
    _, headers, _ = _sign_discord(body)
    with TestClient(create_app(hive)) as c:
        r = c.post("/discord/webhook", content=body,
                   headers={**headers, "Content-Type": "application/json"})
        assert r.status_code == 200
        assert r.json() == {"type": 1}


def test_email_webhook_bad_secret_returns_401(tmp_path):
    hive = _build_hive(tmp_path, smtp_webhook_secret="secret")
    with TestClient(create_app(hive)) as c:
        r = c.post("/email/webhook", content=b"From: x")
        assert r.status_code == 401


def test_email_webhook_processes_message_with_fake_channel(tmp_path, monkeypatch):
    class FakeEmailChannel(ChannelAdapter):
        name = "email"

        def __init__(self, **kw):
            self.sent: list[OutgoingMessage] = []

        def parse_update(self, raw):
            return MessageEvent(text="hello hive", chat_id="alice@x.com",
                                user_id="alice@x.com", message_id="<1@x.com>",
                                platform="email", raw=raw)

        async def send(self, message):
            self.sent.append(message)
            return SendResult(ok=True, message_id="m1")

    from hive.gateway.channels import email as email_mod
    monkeypatch.setattr(email_mod, "EmailChannel", FakeEmailChannel)
    hive = _build_hive(tmp_path, smtp_webhook_secret="secret")
    fake = FakeEmailChannel()
    monkeypatch.setattr(email_mod, "EmailChannel", lambda **kw: fake)
    with TestClient(create_app(hive)) as c:
        r = c.post("/email/webhook", content=b"raw",
                   headers={"X-Webhook-Secret": "secret"})
        assert r.status_code == 200
        assert fake.sent and fake.sent[0].text == "ok"


# --- Optional: gateway WITHOUT channels configured ----------------------------

def test_webhooks_absent_when_config_empty(tmp_path):
    hive = _build_hive(tmp_path)
    with TestClient(create_app(hive)) as c:
        # Endpoints should not be registered when config is empty.
        for path in ("/slack/webhook", "/discord/webhook", "/email/webhook"):
            r = c.post(path, json={})
            assert r.status_code == 404, f"{path} should be 404 when config empty (got {r.status_code})"
