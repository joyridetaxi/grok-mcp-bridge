"""Stateless MCP bridge from Claude (or any MCP client) to xAI Grok."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

log = logging.getLogger("grok-bridge")

XAI_URL = "https://api.x.ai/v1/responses"
DEFAULT_MODEL = "grok-4.3"
# grok-4.3 standard short-context rates (docs.x.ai, checked 2026-11-26).
DEFAULT_INPUT_USD_PER_MTOK = 1.25
DEFAULT_OUTPUT_USD_PER_MTOK = 2.50
DEFAULT_BUDGET_USD = 15.0
DEFAULT_RATE_LIMIT = 30
DEFAULT_MAX_PROMPT_CHARS = 16_000
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_OUTPUT_RESERVE_TOKENS = 4_096
DEFAULT_LIVE_SURCHARGE_USD = 0.01
MIN_TOKEN_CHARS = 32

mcp = FastMCP(
    "grok-bridge",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


class Budget:
    """Calendar-month USD cap. File-backed so a process restart keeps the tally."""

    def __init__(self, path: Path, cap_usd: float) -> None:
        self.path = path
        self.cap_usd = cap_usd
        self._lock = asyncio.Lock()
        self._month, self._spent, self._calls = self._read()

    @staticmethod
    def month_key() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _read(self) -> tuple[str, float, int]:
        month = self.month_key()
        try:
            data = json.loads(self.path.read_text())
            if data.get("month") == month:
                return month, float(data.get("spent_usd", 0)), int(data.get("calls", 0))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            log.warning("usage file unreadable; starting month at zero (%s)", type(exc).__name__)
        return month, 0.0, 0

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"month": self._month, "spent_usd": round(self._spent, 6), "calls": self._calls}
        )
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(payload)
        tmp.replace(self.path)

    def _roll(self) -> None:
        current = self.month_key()
        if self._month != current:
            self._month, self._spent, self._calls = current, 0.0, 0

    def snapshot(self) -> dict[str, Any]:
        self._roll()
        remaining = max(0.0, self.cap_usd - self._spent)
        return {
            "month": self._month,
            "spent_usd": round(self._spent, 6),
            "cap_usd": self.cap_usd,
            "remaining_usd": round(remaining, 6),
            "calls": self._calls,
        }

    async def check_and_reserve(self, estimate_usd: float) -> tuple[bool, float]:
        async with self._lock:
            self._roll()
            if self._spent + estimate_usd > self.cap_usd:
                return False, max(0.0, self.cap_usd - self._spent)
            return True, max(0.0, self.cap_usd - self._spent)

    async def record(self, actual_usd: float) -> None:
        async with self._lock:
            self._roll()
            self._spent += max(0.0, actual_usd)
            self._calls += 1
            self._write()


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def allow(self) -> bool:
        if self.per_minute <= 0:
            return False
        now = time.monotonic()
        async with self._lock:
            cutoff = now - 60.0
            while self._hits and self._hits[0] < cutoff:
                self._hits.popleft()
            if len(self._hits) >= self.per_minute:
                return False
            self._hits.append(now)
            return True


@dataclass
class Settings:
    api_key: str
    token: str
    model: str
    timeout_seconds: float
    max_prompt_chars: int
    rate_limit_per_minute: int
    monthly_budget_usd: float
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    output_reserve_tokens: int
    live_surcharge_usd: float
    usage_file: Path

    @classmethod
    def from_env(cls) -> "Settings":
        api_key = os.environ.get("XAI_API_KEY", "")
        token = os.environ.get("BRIDGE_TOKEN", "")
        if not api_key:
            raise RuntimeError("XAI_API_KEY is required")
        if not token:
            raise RuntimeError("BRIDGE_TOKEN is required")
        if len(token) < 16:
            raise RuntimeError("BRIDGE_TOKEN must be at least 16 characters")
        if len(token) < MIN_TOKEN_CHARS:
            log.warning(
                "BRIDGE_TOKEN is shorter than the recommended %s characters; consider rotating",
                MIN_TOKEN_CHARS,
            )
        usage = os.environ.get("USAGE_FILE", "/tmp/grok-bridge-usage.json")
        return cls(
            api_key=api_key,
            token=token,
            model=os.environ.get("GROK_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL,
            timeout_seconds=_float_env("XAI_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
            max_prompt_chars=_int_env("MAX_PROMPT_CHARS", DEFAULT_MAX_PROMPT_CHARS),
            rate_limit_per_minute=_int_env("RATE_LIMIT_PER_MINUTE", DEFAULT_RATE_LIMIT),
            monthly_budget_usd=_float_env("MONTHLY_BUDGET_USD", DEFAULT_BUDGET_USD),
            input_usd_per_mtok=_float_env("INPUT_USD_PER_MTOK", DEFAULT_INPUT_USD_PER_MTOK),
            output_usd_per_mtok=_float_env("OUTPUT_USD_PER_MTOK", DEFAULT_OUTPUT_USD_PER_MTOK),
            output_reserve_tokens=_int_env("OUTPUT_RESERVE_TOKENS", DEFAULT_OUTPUT_RESERVE_TOKENS),
            live_surcharge_usd=_float_env("LIVE_SEARCH_SURCHARGE_USD", DEFAULT_LIVE_SURCHARGE_USD),
            usage_file=Path(usage),
        )


@dataclass
class State:
    settings: Settings
    budget: Budget
    limiter: RateLimiter

    @classmethod
    def from_settings(cls, settings: Settings) -> "State":
        return cls(
            settings=settings,
            budget=Budget(settings.usage_file, settings.monthly_budget_usd),
            limiter=RateLimiter(settings.rate_limit_per_minute),
        )


_state: State | None = None


def get_state() -> State:
    global _state
    if _state is None:
        _state = State.from_settings(Settings.from_env())
    return _state


def reset_state(state: State | None = None) -> None:
    """Test hook. Pass None to rebuild from the current environment."""
    global _state
    _state = state


def authorized(header: str, token: str) -> bool:
    """Constant-time bearer check. Scheme match is case-insensitive."""
    scheme, sep, rest = header.partition(" ")
    provided = rest.strip() if sep and scheme.lower() == "bearer" else ""
    digest = lambda value: hmac.new(
        b"grok-mcp-bridge-auth", value.encode("utf-8"), hashlib.sha256
    ).digest()
    return hmac.compare_digest(digest(provided), digest(token))


def estimate_tokens(text: str) -> int:
    # Rough 4-chars-per-token estimate; actual usage is reconciled after the call.
    return max(1, (len(text) + 3) // 4)


def estimate_cost_usd(
    settings: Settings, input_tokens: int, output_tokens: int, live: bool
) -> float:
    cost = (
        input_tokens / 1_000_000 * settings.input_usd_per_mtok
        + output_tokens / 1_000_000 * settings.output_usd_per_mtok
    )
    if live:
        cost += settings.live_surcharge_usd
    return cost


def usage_tokens(data: dict) -> tuple[int, int] | None:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    raw_in = usage.get("input_tokens", usage.get("prompt_tokens"))
    raw_out = usage.get("output_tokens", usage.get("completion_tokens"))
    if raw_in is None or raw_out is None:
        return None
    try:
        return int(raw_in), int(raw_out)
    except (TypeError, ValueError):
        return None


def _extract_text(data: dict) -> str:
    if isinstance(data.get("output_text"), str) and data["output_text"]:
        return data["output_text"]
    parts = []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for block in item.get("content", []):
                if block.get("type") in ("output_text", "text") and block.get("text"):
                    parts.append(block["text"])
    return "\n".join(parts) or str(data)


async def _call_grok(
    prompt: str, system: str | None, tools: list[dict] | None
) -> tuple[str, dict | None, int]:
    settings = get_state().settings
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body: dict = {"model": settings.model, "input": messages}
    if tools:
        body["tools"] = tools

    try:
        async with httpx.AsyncClient(timeout=settings.timeout_seconds) as client:
            resp = await client.post(
                XAI_URL,
                headers={"Authorization": f"Bearer {settings.api_key}"},
                json=body,
            )
    except httpx.TimeoutException:
        log.warning("xAI request timed out timeout_s=%s", settings.timeout_seconds)
        return (
            f"Grok API error: request timed out after {settings.timeout_seconds:.0f}s.",
            None,
            598,
        )
    except httpx.HTTPError as exc:
        log.warning("xAI transport error type=%s", type(exc).__name__)
        return "Grok API error: transport failure.", None, 599
    if resp.status_code != 200:
        log.warning("xAI request failed status=%s bytes=%s", resp.status_code, len(resp.content))
        return f"Grok API error {resp.status_code}", None, resp.status_code
    try:
        data = resp.json()
    except ValueError:
        log.warning("xAI returned non-JSON body bytes=%s", len(resp.content))
        return "Grok API error: invalid JSON response", None, resp.status_code
    if not isinstance(data, dict):
        return "Grok API error: unexpected response shape", None, resp.status_code
    return _extract_text(data), data, resp.status_code


async def _ask(prompt: str, system: str | None, live: bool) -> str:
    state = get_state()
    settings = state.settings
    tool_name = "ask_grok_live" if live else "ask_grok"
    started = time.monotonic()

    if not prompt or not prompt.strip():
        return "Error: prompt is required."
    prompt_chars = len(prompt) + (len(system) if system else 0)
    if prompt_chars > settings.max_prompt_chars:
        log.info("tool=%s rejected=prompt_too_long chars=%s", tool_name, prompt_chars)
        return (
            f"Error: prompt is too long ({prompt_chars} chars; "
            f"max {settings.max_prompt_chars})."
        )

    if not await state.limiter.allow():
        log.warning("tool=%s rejected=rate_limited", tool_name)
        return (
            f"Error: rate limit exceeded ({settings.rate_limit_per_minute} requests/minute). "
            "Try again shortly."
        )

    input_tokens = estimate_tokens(prompt) + (estimate_tokens(system) if system else 0)
    reserved = estimate_cost_usd(
        settings, input_tokens, settings.output_reserve_tokens, live
    )
    ok, remaining = await state.budget.check_and_reserve(reserved)
    if not ok:
        log.warning(
            "tool=%s rejected=budget_cap remaining_usd=%.4f cap_usd=%.2f",
            tool_name,
            remaining,
            settings.monthly_budget_usd,
        )
        return (
            f"Error: monthly xAI budget cap reached "
            f"(${settings.monthly_budget_usd:.2f} USD). Request blocked."
        )

    tools = [{"type": "web_search"}, {"type": "x_search"}] if live else None
    text, data, status = await _call_grok(prompt, system, tools)

    if data is not None:
        used = usage_tokens(data)
        if used:
            in_tok, out_tok = used
        else:
            in_tok, out_tok = input_tokens, estimate_tokens(text)
        actual = estimate_cost_usd(settings, in_tok, out_tok, live)
    else:
        # Failed upstream: do not charge the reserved output, only a tiny floor of zero.
        in_tok, out_tok, actual = 0, 0, 0.0
        if live:
            # A failed live call should not add the search surcharge.
            actual = 0.0

    if actual > 0:
        await state.budget.record(actual)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    snap = state.budget.snapshot()
    log.info(
        "tool=%s status=%s duration_ms=%s prompt_chars=%s tokens_in=%s tokens_out=%s "
        "cost_usd=%.6f spent_usd=%.4f remaining_usd=%.4f",
        tool_name,
        status,
        elapsed_ms,
        prompt_chars,
        in_tok,
        out_tok,
        actual,
        snap["spent_usd"],
        snap["remaining_usd"],
    )
    return text


@mcp.tool()
async def ask_grok(prompt: str, system: str | None = None) -> str:
    """Ask Grok a question and get its answer. Use for a second opinion,
    critique, or alternative approach. No live data."""
    return await _ask(prompt, system, live=False)


@mcp.tool()
async def ask_grok_live(prompt: str, system: str | None = None) -> str:
    """Ask Grok with live web search and X (Twitter) search enabled. Use for
    current events, market chatter, or sentiment on a ticker or topic."""
    return await _ask(prompt, system, live=True)


class BearerAuth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # /health is open and must not leak model, budget, or rate-limit config.
        if request.url.path == "/health":
            return JSONResponse({"ok": True})
        header = request.headers.get("authorization", "")
        if not authorized(header, get_state().settings.token):
            log.info("auth=rejected path=%s", request.url.path)
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if request.url.path == "/status":
            state = get_state()
            snap = state.budget.snapshot()
            return JSONResponse(
                {
                    "ok": True,
                    "model": state.settings.model,
                    "budget": snap,
                    "rate_limit_per_minute": state.settings.rate_limit_per_minute,
                }
            )
        return await call_next(request)


app = mcp.streamable_http_app()
app.add_middleware(BearerAuth)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Fail fast if required env is missing, before binding the port.
    get_state()
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
