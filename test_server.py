"""Unit tests for grok-mcp-bridge. No real xAI calls and no real secrets."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

os.environ["XAI_API_KEY"] = "test-xai-key"
os.environ["BRIDGE_TOKEN"] = "t" * 32
os.environ["MONTHLY_BUDGET_USD"] = "15"
os.environ["RATE_LIMIT_PER_MINUTE"] = "30"
os.environ["MAX_PROMPT_CHARS"] = "16000"
os.environ.setdefault("USAGE_FILE", "/tmp/grok-bridge-test-usage.json")

import server  # noqa: E402

TOKEN = "t" * 32


def _http_app() -> Starlette:
    """BearerAuth + dummy routes. Avoids FastMCP's one-shot session manager."""

    async def dummy(_request):
        return JSONResponse({"ok": "authed"})

    app = Starlette(
        routes=[
            Route("/", dummy),
            Route("/mcp", dummy, methods=["GET", "POST"]),
            Route("/status", dummy, methods=["GET"]),
        ]
    )
    app.add_middleware(server.BearerAuth)
    return app


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-xai-key")
    monkeypatch.setenv("BRIDGE_TOKEN", TOKEN)
    monkeypatch.setenv("GROK_MODEL", "grok-4.3")
    monkeypatch.setenv("MONTHLY_BUDGET_USD", "15")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "30")
    monkeypatch.setenv("MAX_PROMPT_CHARS", "100")
    monkeypatch.setenv("USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("LIVE_SEARCH_SURCHARGE_USD", "0.01")
    monkeypatch.setenv("OUTPUT_RESERVE_TOKENS", "100")
    server.reset_state(server.State.from_settings(server.Settings.from_env()))
    yield
    server.reset_state(None)


@pytest.fixture
def client(configured):
    with TestClient(_http_app()) as test_client:
        yield test_client


def test_health_is_open_and_omits_secrets(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    dumped = res.text.lower()
    assert "test-xai-key" not in dumped
    assert TOKEN.lower() not in dumped
    assert "authorization" not in dumped
    assert "budget" not in dumped
    assert "grok-4.3" not in dumped


def test_status_requires_auth_and_omits_secrets(client):
    assert client.get("/status").status_code == 401
    res = client.get("/status", headers={"Authorization": f"Bearer {TOKEN}"})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["model"] == "grok-4.3"
    assert body["budget"]["cap_usd"] == 15
    assert body["budget"]["spent_usd"] == 0
    assert "rate_limit_per_minute" in body
    dumped = res.text.lower()
    assert "test-xai-key" not in dumped
    assert TOKEN.lower() not in dumped


def test_root_requires_auth(client):
    assert client.get("/").status_code == 401
    assert client.get("/", headers={"Authorization": "Bearer wrong-token-value-00000000"}).status_code == 401
    assert client.get("/", headers={"Authorization": "Basic " + TOKEN}).status_code == 401
    ok = client.get("/", headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200
    assert ok.json() == {"ok": "authed"}


def test_mcp_requires_auth(client):
    assert client.post("/mcp").status_code == 401
    ok = client.post("/mcp", headers={"Authorization": f"bearer {TOKEN}"})
    assert ok.status_code == 200


def test_bearer_is_case_insensitive_and_constant_time():
    assert server.authorized(f"Bearer {TOKEN}", TOKEN)
    assert server.authorized(f"bearer {TOKEN}", TOKEN)
    assert server.authorized(f"BEARER {TOKEN}", TOKEN)
    assert not server.authorized(f"Bearer {TOKEN[:-1]}x", TOKEN)
    assert not server.authorized("", TOKEN)
    assert not server.authorized("Bearer", TOKEN)
    assert not server.authorized(f"Bearer {TOKEN} extra", TOKEN)


def test_cost_estimate_matches_published_rates():
    settings = server.Settings.from_env()
    cost = server.estimate_cost_usd(settings, 1_000_000, 1_000_000, live=False)
    assert cost == pytest.approx(3.75)
    live = server.estimate_cost_usd(settings, 1_000_000, 1_000_000, live=True)
    assert live == pytest.approx(3.76)


@pytest.mark.asyncio
async def test_prompt_too_long_does_not_call_xai(configured):
    with patch.object(server, "_call_grok", new_callable=AsyncMock) as mocked:
        result = await server.ask_grok("x" * 101)
    assert "too long" in result
    mocked.assert_not_called()


@pytest.mark.asyncio
async def test_empty_prompt_does_not_call_xai(configured):
    with patch.object(server, "_call_grok", new_callable=AsyncMock) as mocked:
        result = await server.ask_grok("   ")
    assert "required" in result.lower()
    mocked.assert_not_called()


@pytest.mark.asyncio
async def test_rate_limit_blocks_excess_calls(configured, monkeypatch, tmp_path):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
    monkeypatch.setenv("USAGE_FILE", str(tmp_path / "usage-rl.json"))
    server.reset_state(server.State.from_settings(server.Settings.from_env()))

    async def fake(prompt, system, tools):
        return "ok", {"usage": {"input_tokens": 4, "output_tokens": 4}}, 200

    with patch.object(server, "_call_grok", new=fake):
        assert await server.ask_grok("hi") == "ok"
        assert await server.ask_grok("hi") == "ok"
        blocked = await server.ask_grok("hi")
    assert "rate limit" in blocked.lower()


@pytest.mark.asyncio
async def test_budget_cap_blocks_before_xai(configured, monkeypatch, tmp_path):
    usage = tmp_path / "usage-cap.json"
    usage.write_text('{"month": "%s", "spent_usd": 15, "calls": 9}' % server.Budget.month_key())
    monkeypatch.setenv("USAGE_FILE", str(usage))
    monkeypatch.setenv("MONTHLY_BUDGET_USD", "15")
    server.reset_state(server.State.from_settings(server.Settings.from_env()))

    with patch.object(server, "_call_grok", new_callable=AsyncMock) as mocked:
        result = await server.ask_grok("hello")
    assert "budget cap" in result.lower()
    mocked.assert_not_called()
    snap = server.get_state().budget.snapshot()
    assert snap["spent_usd"] == 15
    assert snap["remaining_usd"] == 0


@pytest.mark.asyncio
async def test_successful_call_records_usage_from_response(configured, tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_FILE", str(tmp_path / "usage-ok.json"))
    server.reset_state(server.State.from_settings(server.Settings.from_env()))

    payload = {
        "output_text": "second opinion",
        "usage": {"input_tokens": 1_000, "output_tokens": 500},
    }

    async def fake(prompt, system, tools):
        assert tools is None
        return "second opinion", payload, 200

    with patch.object(server, "_call_grok", new=fake):
        result = await server.ask_grok("need a critique")
    assert result == "second opinion"
    snap = server.get_state().budget.snapshot()
    expected = 1000 / 1_000_000 * 1.25 + 500 / 1_000_000 * 2.50
    assert snap["spent_usd"] == pytest.approx(expected, rel=1e-6)
    assert snap["calls"] == 1


@pytest.mark.asyncio
async def test_live_call_adds_surcharge_and_tools(configured, tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_FILE", str(tmp_path / "usage-live.json"))
    monkeypatch.setenv("LIVE_SEARCH_SURCHARGE_USD", "0.01")
    server.reset_state(server.State.from_settings(server.Settings.from_env()))

    seen = {}

    async def fake(prompt, system, tools):
        seen["tools"] = tools
        return "live", {"usage": {"input_tokens": 0, "output_tokens": 0}}, 200

    with patch.object(server, "_call_grok", new=fake):
        result = await server.ask_grok_live("what's the news")
    assert result == "live"
    assert seen["tools"] == [{"type": "web_search"}, {"type": "x_search"}]
    snap = server.get_state().budget.snapshot()
    assert snap["spent_usd"] == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_upstream_error_does_not_charge_budget(configured):
    async def fake(prompt, system, tools):
        return "Grok API error 500", None, 500

    with patch.object(server, "_call_grok", new=fake):
        result = await server.ask_grok("hello")
    assert result == "Grok API error 500"
    assert server.get_state().budget.snapshot()["spent_usd"] == 0
    assert server.get_state().budget.snapshot()["calls"] == 0


def test_short_token_fails_fast(monkeypatch, tmp_path):
    monkeypatch.setenv("XAI_API_KEY", "test-xai-key")
    monkeypatch.setenv("BRIDGE_TOKEN", "short")
    monkeypatch.setenv("USAGE_FILE", str(tmp_path / "usage-short.json"))
    server.reset_state(None)
    with pytest.raises(RuntimeError, match="at least 16"):
        server.Settings.from_env()



MAILBOX_TOKEN = "m" * 32
WEBHOOK_URL = "https://example.test/automations/webhook/test"
WEBHOOK_KEY = "w" * 32


@pytest.fixture
def mailbox_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-xai-key")
    monkeypatch.setenv("BRIDGE_TOKEN", TOKEN)
    monkeypatch.setenv("GROK_MODEL", "grok-4.3")
    monkeypatch.setenv("MONTHLY_BUDGET_USD", "15")
    monkeypatch.setenv("USAGE_FILE", str(tmp_path / "usage-mailbox.json"))
    monkeypatch.setenv("GROK_BOT_WEBHOOK_URL", WEBHOOK_URL)
    monkeypatch.setenv("GROK_BOT_WEBHOOK_KEY", WEBHOOK_KEY)
    monkeypatch.setenv("MAILBOX_TOKEN", MAILBOX_TOKEN)
    monkeypatch.setenv("MAILBOX_MAX_PER_HOUR", "10")
    monkeypatch.setenv("MAILBOX_MAX_PER_DAY", "20")
    monkeypatch.setenv("MAILBOX_MAX_CHARS", "16000")
    monkeypatch.setenv("MAILBOX_MAX_WAIT_SECONDS", "25")
    server.reset_state(server.State.from_settings(server.Settings.from_env()))
    yield
    server.reset_state(None)


@pytest.fixture
def mailbox_client(mailbox_configured):
    with TestClient(_http_app()) as test_client:
        yield test_client


@pytest.mark.asyncio
async def test_message_grok_bot_disabled_when_env_unset(configured):
    result = await server.message_grok_bot("need a Render check")
    assert result["ok"] is False
    assert "disabled" in result["error"].lower()
    assert "GROK_BOT_WEBHOOK_URL" in result["error"]


@pytest.mark.asyncio
async def test_message_grok_bot_posts_expected_payload(mailbox_configured):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["Authorization"]
        seen["content_type"] = request.headers["Content-Type"]
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"accepted": True})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        kwargs.setdefault("timeout", 8.0)
        return real_client(*args, **kwargs)

    with patch("server.httpx.AsyncClient", side_effect=fake_client):
        result = await server.message_grok_bot(
            "Need Render status for joyride-web",
            context="staging autoDeploy is off",
            project="joyride-web",
            thread_id="thread-1",
            kind="message",
        )
    assert result["ok"] is True
    assert result["accepted"] is True
    assert result["kind"] == "message"
    assert seen["url"] == WEBHOOK_URL
    assert seen["authorization"] == f"Bearer {WEBHOOK_KEY}"
    assert "application/json" in seen["content_type"]
    body = seen["body"]
    assert body == {
        "source": "claude",
        "kind": "message",
        "message_id": result["message_id"],
        "text": "Need Render status for joyride-web",
        "context": "staging autoDeploy is off",
        "project": "joyride-web",
        "thread_id": "thread-1",
        "ts": result["ts"],
    }
    assert body["message_id"] == result["message_id"]


@pytest.mark.asyncio
async def test_message_grok_bot_flush_allows_empty_text(mailbox_configured):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        assert body["kind"] == "flush"
        assert body["text"] == ""
        assert body["context"] is None
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    with patch("server.httpx.AsyncClient", side_effect=fake_client):
        result = await server.message_grok_bot(kind="flush")
    assert result["ok"] is True
    assert result["kind"] == "flush"


@pytest.mark.asyncio
async def test_message_grok_bot_requires_text_for_message(mailbox_configured):
    result = await server.message_grok_bot("  ", kind="message")
    assert result["ok"] is False
    assert "text is required" in result["error"]


@pytest.mark.asyncio
async def test_message_grok_bot_rejects_long_payload(mailbox_configured, monkeypatch, tmp_path):
    monkeypatch.setenv("MAILBOX_MAX_CHARS", "20")
    monkeypatch.setenv("USAGE_FILE", str(tmp_path / "usage-mailbox-long.json"))
    server.reset_state(server.State.from_settings(server.Settings.from_env()))
    result = await server.message_grok_bot("x" * 21)
    assert result["ok"] is False
    assert "too long" in result["error"]


@pytest.mark.asyncio
async def test_message_grok_bot_hour_and_day_caps(mailbox_configured, monkeypatch, tmp_path):
    monkeypatch.setenv("MAILBOX_MAX_PER_HOUR", "2")
    monkeypatch.setenv("MAILBOX_MAX_PER_DAY", "3")
    monkeypatch.setenv("USAGE_FILE", str(tmp_path / "usage-mailbox-rl.json"))
    server.reset_state(server.State.from_settings(server.Settings.from_env()))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    with patch("server.httpx.AsyncClient", side_effect=fake_client):
        first = await server.message_grok_bot("one")
        second = await server.message_grok_bot("two")
        third = await server.message_grok_bot("three")
    assert first["ok"] is True
    assert second["ok"] is True
    assert third["ok"] is False
    assert "per hour" in third["error"]

    # Advance just over an hour so the hour cap resets but the day cap still
    # counts the earlier calls.
    limiter = server.get_state().mailbox_limiter
    for idx, _ts in enumerate(list(limiter._hits)):
        limiter._hits[idx] -= 3601

    with patch("server.httpx.AsyncClient", side_effect=fake_client):
        fourth = await server.message_grok_bot("four")
        fifth = await server.message_grok_bot("five")
    assert fourth["ok"] is True
    assert fifth["ok"] is False
    assert "per day" in fifth["error"]


@pytest.mark.asyncio
async def test_message_grok_bot_webhook_error_is_reported(mailbox_configured):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    with patch("server.httpx.AsyncClient", side_effect=fake_client):
        result = await server.message_grok_bot("hello")
    assert result["ok"] is False
    assert "500" in result["error"]
    assert "message_id" in result


def test_bot_replies_requires_mailbox_token_not_bridge_token(mailbox_client):
    assert mailbox_client.post("/bot-replies").status_code == 401
    assert mailbox_client.post(
        "/bot-replies",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"message_id": "m1", "reply": "hi"},
    ).status_code == 401
    assert mailbox_client.post(
        "/bot-replies",
        headers={"Authorization": "Bearer wrong-mailbox-token-0000000000"},
        json={"message_id": "m1", "reply": "hi"},
    ).status_code == 401
    res = mailbox_client.post(
        "/bot-replies",
        headers={"Authorization": f"Bearer {MAILBOX_TOKEN}"},
        json={"message_id": "m1", "reply": "hello from grok bot", "status": "ok"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["message_id"] == "m1"
    assert body["status"] == "ok"
    dumped = res.text.lower()
    assert MAILBOX_TOKEN.lower() not in dumped
    assert TOKEN.lower() not in dumped


def test_bot_replies_disabled_without_token(configured):
    with TestClient(_http_app()) as client:
        res = client.post(
            "/bot-replies",
            headers={"Authorization": f"Bearer {MAILBOX_TOKEN}"},
            json={"message_id": "m1", "reply": "hi"},
        )
    assert res.status_code == 503
    assert res.json() == {"error": "mailbox disabled"}


def test_mailbox_token_matching_bridge_token_is_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-xai-key")
    monkeypatch.setenv("BRIDGE_TOKEN", TOKEN)
    monkeypatch.setenv("MAILBOX_TOKEN", TOKEN)
    monkeypatch.setenv("USAGE_FILE", str(tmp_path / "usage-same-token.json"))
    server.reset_state(server.State.from_settings(server.Settings.from_env()))
    assert server.get_state().settings.mailbox_enabled is False
    with TestClient(_http_app()) as client:
        res = client.post(
            "/bot-replies",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"message_id": "m1", "reply": "hi"},
        )
    assert res.status_code == 503
    server.reset_state(None)


def test_short_mailbox_token_is_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-xai-key")
    monkeypatch.setenv("BRIDGE_TOKEN", TOKEN)
    monkeypatch.setenv("MAILBOX_TOKEN", "short")
    monkeypatch.setenv("USAGE_FILE", str(tmp_path / "usage-short-mail.json"))
    settings = server.Settings.from_env()
    assert settings.mailbox_enabled is False
    server.reset_state(None)


def test_bot_replies_validation(mailbox_client):
    headers = {"Authorization": f"Bearer {MAILBOX_TOKEN}"}
    assert mailbox_client.get("/bot-replies", headers=headers).status_code == 405
    assert mailbox_client.post("/bot-replies", headers=headers, json=["x"]).status_code == 400
    assert mailbox_client.post(
        "/bot-replies", headers=headers, json={"reply": "hi"}
    ).status_code == 400
    assert mailbox_client.post(
        "/bot-replies", headers=headers, json={"message_id": "m1"}
    ).status_code == 400
    assert mailbox_client.post(
        "/bot-replies",
        headers=headers,
        json={"message_id": "m1", "reply": "hi", "status": "nope"},
    ).status_code == 400


@pytest.mark.asyncio
async def test_get_replies_and_ack(mailbox_client, mailbox_configured):
    headers = {"Authorization": f"Bearer {MAILBOX_TOKEN}"}
    posted = mailbox_client.post(
        "/bot-replies",
        headers=headers,
        json={
            "message_id": "abc-123",
            "reply": "staging is up",
            "status": "ok",
            "ts": "2026-11-26T22:35:00+00:00",
        },
    )
    assert posted.status_code == 200
    listed = await server.get_grok_bot_replies()
    assert listed["ok"] is True
    assert listed["pending"] is False
    assert listed["replies"] == [
        {
            "message_id": "abc-123",
            "reply": "staging is up",
            "status": "ok",
            "ts": "2026-11-26T22:35:00+00:00",
        }
    ]
    filtered = await server.get_grok_bot_replies(message_id="abc-123")
    assert len(filtered["replies"]) == 1
    empty = await server.get_grok_bot_replies(message_id="no-such")
    assert empty["replies"] == []
    assert empty["pending"] is True

    ack = mailbox_client.post(
        "/bot-replies/ack",
        headers=headers,
        json={"message_id": "abc-123"},
    )
    assert ack.status_code == 200
    assert ack.json() == {"ok": True, "acked": 1}
    after = await server.get_grok_bot_replies()
    assert after["replies"] == []
    assert after["pending"] is True
    still = await server.get_grok_bot_replies(include_acked=True, message_id="abc-123")
    assert len(still["replies"]) == 1


@pytest.mark.asyncio
async def test_get_replies_ack_ids_and_long_poll(mailbox_configured):
    await server.get_state().mailbox.add(
        server.MailboxItem(
            message_id="first",
            reply="one",
            status="ok",
            ts="2026-11-26T22:35:00+00:00",
        )
    )
    listed = await server.get_grok_bot_replies(ack_ids=["first"])
    # ack happens first, so the already-stored reply is hidden unless include_acked.
    assert listed["replies"] == []

    async def later_reply():
        await asyncio.sleep(0.05)
        await server.get_state().mailbox.add(
            server.MailboxItem(
                message_id="second",
                reply="two",
                status="pending_owner",
                ts="2026-11-26T22:36:00+00:00",
            )
        )

    task = asyncio.create_task(later_reply())
    waited = await server.get_grok_bot_replies(wait_seconds=2)
    await task
    assert waited["replies"][0]["message_id"] == "second"
    assert waited["replies"][0]["status"] == "pending_owner"


def test_bot_replies_rejects_bridge_token_on_ack(mailbox_client):
    res = mailbox_client.post(
        "/bot-replies/ack",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"message_id": "m1"},
    )
    assert res.status_code == 401


def test_mailbox_token_authorized_is_constant_time():
    assert server.authorized(f"Bearer {MAILBOX_TOKEN}", MAILBOX_TOKEN)
    assert server.authorized(f"bearer {MAILBOX_TOKEN}", MAILBOX_TOKEN)
    assert not server.authorized(f"Bearer {TOKEN}", MAILBOX_TOKEN)
    assert not server.authorized(f"Bearer {MAILBOX_TOKEN}", TOKEN)
    assert not server.authorized("", MAILBOX_TOKEN)


@pytest.mark.asyncio
async def test_logs_omit_secrets_and_message_text(mailbox_configured, caplog):
    caplog.set_level("INFO")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    secret_text = "SECRET_PROMPT_SHOULD_NOT_BE_LOGGED"
    with patch("server.httpx.AsyncClient", side_effect=fake_client):
        result = await server.message_grok_bot(secret_text, context="SECRET_CONTEXT")
    assert result["ok"] is True

    with TestClient(_http_app()) as client:
        client.post(
            "/bot-replies",
            headers={"Authorization": f"Bearer {MAILBOX_TOKEN}"},
            json={
                "message_id": result["message_id"],
                "reply": "SECRET_REPLY_SHOULD_NOT_BE_LOGGED",
                "status": "ok",
            },
        )

    dumped = caplog.text
    assert secret_text not in dumped
    assert "SECRET_CONTEXT" not in dumped
    assert "SECRET_REPLY_SHOULD_NOT_BE_LOGGED" not in dumped
    assert WEBHOOK_KEY not in dumped
    assert MAILBOX_TOKEN not in dumped
    assert TOKEN not in dumped
    assert "test-xai-key" not in dumped
    assert "Bearer " not in dumped
