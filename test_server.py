"""Unit tests for grok-mcp-bridge. No real xAI calls and no real secrets."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

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
    body = res.json()
    assert body["ok"] is True
    assert body["model"] == "grok-4.3"
    assert body["budget"]["cap_usd"] == 15
    assert body["budget"]["spent_usd"] == 0
    assert "rate_limit_per_minute" in body
    dumped = res.text.lower()
    assert "test-xai-key" not in dumped
    assert TOKEN.lower() not in dumped
    assert "authorization" not in dumped


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
