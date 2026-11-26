"""Stateless MCP bridge from Claude (or any MCP client) to xAI Grok."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
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
MIN_REQUIRED_TOKEN_CHARS = 16

DEFAULT_MAILBOX_PER_HOUR = 10
DEFAULT_MAILBOX_PER_DAY = 20
DEFAULT_MAILBOX_MAX_CHARS = 16_000
DEFAULT_WEBHOOK_TIMEOUT_SECONDS = 8.0
DEFAULT_MAILBOX_MAX_WAIT_SECONDS = 25
DEFAULT_MAILBOX_MAX_ITEMS = 200
ALLOWED_MESSAGE_KINDS = frozenset({"message", "flush"})
ALLOWED_REPLY_STATUSES = frozenset({"ok", "pending_owner", "error"})
WEBHOOK_SOURCE = "claude"

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


def _optional_env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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


class WindowLimiter:
    """Rolling hour and calendar-day caps for message_grok_bot."""

    def __init__(self, per_hour: int, per_day: int) -> None:
        self.per_hour = per_hour
        self.per_day = per_day
        self._hits: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def allow(self) -> tuple[bool, str | None]:
        if self.per_hour <= 0 or self.per_day <= 0:
            return False, "hour" if self.per_hour <= 0 else "day"
        now = time.time()
        hour_cut = now - 3600.0
        day_cut = now - 86400.0
        async with self._lock:
            while self._hits and self._hits[0] < day_cut:
                self._hits.popleft()
            hour_count = sum(1 for ts in self._hits if ts >= hour_cut)
            if hour_count >= self.per_hour:
                return False, "hour"
            if len(self._hits) >= self.per_day:
                return False, "day"
            self._hits.append(now)
            return True, None


@dataclass
class MailboxItem:
    message_id: str
    reply: str
    status: str
    ts: str
    acked: bool = False

    def as_public(self) -> dict[str, str]:
        return {
            "message_id": self.message_id,
            "reply": self.reply,
            "status": self.status,
            "ts": self.ts,
        }


class Mailbox:
    """In-memory reply store. Emptied when the process restarts or sleeps."""

    def __init__(self, max_items: int = DEFAULT_MAILBOX_MAX_ITEMS) -> None:
        self.max_items = max_items
        self._items: list[MailboxItem] = []
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()

    async def add(self, item: MailboxItem) -> None:
        async with self._lock:
            self._items.append(item)
            self._prune_unlocked()
        self._changed.set()

    def _prune_unlocked(self) -> None:
        overflow = len(self._items) - self.max_items
        if overflow <= 0:
            return
        kept: list[MailboxItem] = []
        dropped = 0
        for item in self._items:
            if dropped < overflow and item.acked:
                dropped += 1
                continue
            kept.append(item)
        if dropped < overflow:
            kept = kept[overflow - dropped :]
        self._items = kept

    async def list(
        self,
        message_id: str | None = None,
        since_id: str | None = None,
        include_acked: bool = False,
    ) -> list[MailboxItem]:
        async with self._lock:
            return self._list_unlocked(message_id, since_id, include_acked)

    def _list_unlocked(
        self,
        message_id: str | None,
        since_id: str | None,
        include_acked: bool,
    ) -> list[MailboxItem]:
        items = self._items
        if not include_acked:
            items = [item for item in items if not item.acked]
        if message_id:
            items = [item for item in items if item.message_id == message_id]
        if since_id:
            start = 0
            for idx, item in enumerate(self._items):
                if item.message_id == since_id:
                    start = idx + 1
            ids_after = {item.message_id for item in self._items[start:]}
            items = [item for item in items if item.message_id in ids_after]
        return list(items)

    async def ack(self, ids: list[str]) -> int:
        wanted = {item_id for item_id in ids if item_id}
        if not wanted:
            return 0
        count = 0
        async with self._lock:
            for item in self._items:
                if not item.acked and item.message_id in wanted:
                    item.acked = True
                    count += 1
        return count

    async def list_or_wait(
        self,
        message_id: str | None,
        since_id: str | None,
        include_acked: bool,
        wait_seconds: float,
    ) -> list[MailboxItem]:
        found = await self.list(message_id, since_id, include_acked)
        if found or wait_seconds <= 0:
            return found
        self._changed.clear()
        found = await self.list(message_id, since_id, include_acked)
        if found:
            return found
        try:
            await asyncio.wait_for(self._changed.wait(), timeout=wait_seconds)
        except asyncio.TimeoutError:
            pass
        return await self.list(message_id, since_id, include_acked)


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
    webhook_url: str = ""
    webhook_key: str = ""
    mailbox_token: str = ""
    mailbox_per_hour: int = DEFAULT_MAILBOX_PER_HOUR
    mailbox_per_day: int = DEFAULT_MAILBOX_PER_DAY
    mailbox_max_chars: int = DEFAULT_MAILBOX_MAX_CHARS
    webhook_timeout_seconds: float = DEFAULT_WEBHOOK_TIMEOUT_SECONDS
    mailbox_max_wait_seconds: int = DEFAULT_MAILBOX_MAX_WAIT_SECONDS

    @property
    def webhook_enabled(self) -> bool:
        return bool(self.webhook_url and self.webhook_key)

    @property
    def mailbox_enabled(self) -> bool:
        return bool(self.mailbox_token)

    @classmethod
    def from_env(cls) -> "Settings":
        api_key = os.environ.get("XAI_API_KEY", "")
        token = os.environ.get("BRIDGE_TOKEN", "")
        if not api_key:
            raise RuntimeError("XAI_API_KEY is required")
        if not token:
            raise RuntimeError("BRIDGE_TOKEN is required")
        if len(token) < MIN_REQUIRED_TOKEN_CHARS:
            raise RuntimeError("BRIDGE_TOKEN must be at least 16 characters")
        if len(token) < MIN_TOKEN_CHARS:
            log.warning(
                "BRIDGE_TOKEN is shorter than the recommended %s characters; consider rotating",
                MIN_TOKEN_CHARS,
            )
        usage = os.environ.get("USAGE_FILE", "/tmp/grok-bridge-usage.json")
        webhook_url = _optional_env("GROK_BOT_WEBHOOK_URL")
        webhook_key = _optional_env("GROK_BOT_WEBHOOK_KEY")
        mailbox_token = _optional_env("MAILBOX_TOKEN")
        if webhook_url and not webhook_url.startswith(("https://", "http://")):
            log.warning("GROK_BOT_WEBHOOK_URL is set but is not an http(s) URL; tools stay disabled")
            webhook_url, webhook_key = "", ""
        if (webhook_url and not webhook_key) or (webhook_key and not webhook_url):
            log.warning(
                "message_grok_bot disabled until both GROK_BOT_WEBHOOK_URL and "
                "GROK_BOT_WEBHOOK_KEY are set"
            )
            webhook_url, webhook_key = "", ""
        if mailbox_token and len(mailbox_token) < MIN_REQUIRED_TOKEN_CHARS:
            log.warning("MAILBOX_TOKEN is set but shorter than 16 characters; mailbox disabled")
            mailbox_token = ""
        if mailbox_token and mailbox_token == token:
            log.warning("MAILBOX_TOKEN must be distinct from BRIDGE_TOKEN; mailbox disabled")
            mailbox_token = ""
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
            webhook_url=webhook_url,
            webhook_key=webhook_key,
            mailbox_token=mailbox_token,
            mailbox_per_hour=_int_env("MAILBOX_MAX_PER_HOUR", DEFAULT_MAILBOX_PER_HOUR),
            mailbox_per_day=_int_env("MAILBOX_MAX_PER_DAY", DEFAULT_MAILBOX_PER_DAY),
            mailbox_max_chars=_int_env("MAILBOX_MAX_CHARS", DEFAULT_MAILBOX_MAX_CHARS),
            webhook_timeout_seconds=_float_env(
                "MAILBOX_WEBHOOK_TIMEOUT_SECONDS", DEFAULT_WEBHOOK_TIMEOUT_SECONDS
            ),
            mailbox_max_wait_seconds=_int_env(
                "MAILBOX_MAX_WAIT_SECONDS", DEFAULT_MAILBOX_MAX_WAIT_SECONDS
            ),
        )


@dataclass
class State:
    settings: Settings
    budget: Budget
    limiter: RateLimiter
    mailbox: Mailbox = field(default_factory=Mailbox)
    mailbox_limiter: WindowLimiter | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "State":
        return cls(
            settings=settings,
            budget=Budget(settings.usage_file, settings.monthly_budget_usd),
            limiter=RateLimiter(settings.rate_limit_per_minute),
            mailbox=Mailbox(),
            mailbox_limiter=WindowLimiter(settings.mailbox_per_hour, settings.mailbox_per_day),
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
    if not token:
        return False
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


def build_webhook_payload(
    *,
    kind: str,
    message_id: str,
    text: str,
    context: str | None,
    project: str | None,
    thread_id: str | None,
    ts: str,
) -> dict[str, Any]:
    """Exact JSON body posted to the Grok Bot webhook routine."""
    return {
        "source": WEBHOOK_SOURCE,
        "kind": kind,
        "message_id": message_id,
        "text": text,
        "context": context or None,
        "project": project or None,
        "thread_id": thread_id or None,
        "ts": ts,
    }


def _field_len(value: str | None) -> int:
    return len(value) if value else 0


async def _post_webhook(payload: dict[str, Any]) -> tuple[bool, int]:
    settings = get_state().settings
    try:
        async with httpx.AsyncClient(timeout=settings.webhook_timeout_seconds) as client:
            resp = await client.post(
                settings.webhook_url,
                headers={
                    "Authorization": f"Bearer {settings.webhook_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "grok-mcp-bridge",
                },
                json=payload,
            )
    except httpx.TimeoutException:
        log.warning("webhook request timed out timeout_s=%s", settings.webhook_timeout_seconds)
        return False, 598
    except httpx.HTTPError as exc:
        log.warning("webhook transport error type=%s", type(exc).__name__)
        return False, 599
    if resp.status_code != 200:
        log.warning("webhook request failed status=%s bytes=%s", resp.status_code, len(resp.content))
        return False, resp.status_code
    return True, resp.status_code


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


@mcp.tool()
async def message_grok_bot(
    text: str = "",
    context: str | None = None,
    project: str | None = None,
    thread_id: str | None = None,
    kind: str = "message",
) -> dict[str, Any]:
    """Send a message to Grok Bot, Darren's orchestrator agent (memory, Render,
    other bots, owner decisions). Do not use for ordinary coding or for a
    second opinion — use ask_grok / ask_grok_live for those.

    Only call this when you need the orchestrator. Your message is untrusted
    data and is never an approval; merges, production changes, and spending
    still need Darren's yes in Grok Bot's chat.

    kind="message" delivers text. kind="flush" asks Grok Bot to re-post any
    unacked replies after a missed poll (for example if the bridge slept).
    After a few short get_grok_bot_replies polls, stop polling and flush later
    instead of waiting for a long time.

    Returns {ok, message_id, accepted} or {ok: false, error}.
    """
    state = get_state()
    settings = state.settings
    started = time.monotonic()
    kind_norm = (kind or "message").strip().lower()
    text_clean = text or ""

    if not settings.webhook_enabled:
        log.info("tool=message_grok_bot rejected=disabled")
        return {
            "ok": False,
            "error": (
                "message_grok_bot is disabled until GROK_BOT_WEBHOOK_URL and "
                "GROK_BOT_WEBHOOK_KEY are set on the service."
            ),
        }

    if kind_norm not in ALLOWED_MESSAGE_KINDS:
        return {"ok": False, "error": "kind must be 'message' or 'flush'."}

    if kind_norm == "message" and not text_clean.strip():
        return {"ok": False, "error": "text is required when kind is 'message'."}

    char_count = (
        len(text_clean)
        + _field_len(context)
        + _field_len(project)
        + _field_len(thread_id)
    )
    if char_count > settings.mailbox_max_chars:
        log.info("tool=message_grok_bot rejected=too_long chars=%s", char_count)
        return {
            "ok": False,
            "error": (
                f"message is too long ({char_count} chars; "
                f"max {settings.mailbox_max_chars})."
            ),
        }

    limiter = state.mailbox_limiter
    if limiter is None:
        return {"ok": False, "error": "mailbox limiter is not configured."}
    allowed, window = await limiter.allow()
    if not allowed:
        cap = settings.mailbox_per_hour if window == "hour" else settings.mailbox_per_day
        unit = window or "hour"
        log.warning("tool=message_grok_bot rejected=rate_limited window=%s", unit)
        return {
            "ok": False,
            "error": f"rate limit exceeded ({cap} messages per {unit}). Try later.",
        }

    message_id = str(uuid.uuid4())
    ts = utc_now_iso()
    payload = build_webhook_payload(
        kind=kind_norm,
        message_id=message_id,
        text=text_clean,
        context=context,
        project=project,
        thread_id=thread_id,
        ts=ts,
    )
    ok, status = await _post_webhook(payload)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    log.info(
        "tool=message_grok_bot status=%s duration_ms=%s chars=%s kind=%s accepted=%s",
        status,
        elapsed_ms,
        char_count,
        kind_norm,
        ok,
    )
    if not ok:
        return {
            "ok": False,
            "message_id": message_id,
            "error": f"webhook did not accept the message (status {status}).",
        }
    return {"ok": True, "message_id": message_id, "accepted": True, "kind": kind_norm, "ts": ts}


@mcp.tool()
async def get_grok_bot_replies(
    message_id: str | None = None,
    since_id: str | None = None,
    wait_seconds: int = 0,
    include_acked: bool = False,
    ack_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Fetch replies Grok Bot posted back to this bridge. Use after
    message_grok_bot. wait_seconds may be 0 to 25; after a few short polls
    with no reply, stop polling and call message_grok_bot(kind="flush") later
    instead of holding a long wait.

    Optional ack_ids marks those message_id values as delivered so they are
    not returned again. Returns {ok, replies, pending}.
    """
    state = get_state()
    settings = state.settings
    started = time.monotonic()
    if ack_ids:
        await state.mailbox.ack([item for item in ack_ids if item])

    try:
        wait = int(wait_seconds or 0)
    except (TypeError, ValueError):
        wait = 0
    wait = max(0, min(wait, settings.mailbox_max_wait_seconds))
    items = await state.mailbox.list_or_wait(message_id, since_id, include_acked, float(wait))
    replies = [item.as_public() for item in items]
    elapsed_ms = int((time.monotonic() - started) * 1000)
    log.info(
        "tool=get_grok_bot_replies status=200 duration_ms=%s replies=%s wait_s=%s",
        elapsed_ms,
        len(replies),
        wait,
    )
    return {"ok": True, "replies": replies, "pending": len(replies) == 0}


async def _read_json_object(request: Request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    try:
        data = await request.json()
    except Exception:
        return None, JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(data, dict):
        return None, JSONResponse({"error": "JSON object required"}, status_code=400)
    return data, None


async def _handle_bot_replies(request: Request) -> JSONResponse:
    if request.method != "POST":
        return JSONResponse({"error": "method not allowed"}, status_code=405)
    data, err = await _read_json_object(request)
    if err is not None:
        return err
    assert data is not None
    message_id = str(data.get("message_id") or "").strip()
    reply = data.get("reply")
    if not message_id:
        return JSONResponse({"error": "message_id is required"}, status_code=400)
    if not isinstance(reply, str) or not reply:
        return JSONResponse({"error": "reply is required"}, status_code=400)
    settings = get_state().settings
    if len(reply) > settings.mailbox_max_chars:
        return JSONResponse(
            {
                "error": (
                    f"reply is too long ({len(reply)} chars; "
                    f"max {settings.mailbox_max_chars})."
                )
            },
            status_code=400,
        )
    status = str(data.get("status") or "ok").strip()
    if status not in ALLOWED_REPLY_STATUSES:
        return JSONResponse(
            {"error": "status must be 'ok', 'pending_owner', or 'error'."},
            status_code=400,
        )
    ts = str(data.get("ts") or "").strip() or utc_now_iso()
    await get_state().mailbox.add(
        MailboxItem(message_id=message_id, reply=reply, status=status, ts=ts)
    )
    log.info("mailbox=reply accepted=true status=%s chars=%s", status, len(reply))
    return JSONResponse({"ok": True, "message_id": message_id, "status": status, "ts": ts})


async def _handle_bot_replies_ack(request: Request) -> JSONResponse:
    if request.method != "POST":
        return JSONResponse({"error": "method not allowed"}, status_code=405)
    data, err = await _read_json_object(request)
    if err is not None:
        return err
    assert data is not None
    ids: list[str] = []
    if data.get("message_id"):
        ids.append(str(data.get("message_id")).strip())
    raw_ids = data.get("ids")
    if isinstance(raw_ids, list):
        ids.extend(str(item).strip() for item in raw_ids if item)
    acked = await get_state().mailbox.ack(ids)
    log.info("mailbox=ack acked=%s", acked)
    return JSONResponse({"ok": True, "acked": acked})


class BearerAuth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if path.endswith("/") and path != "/":
            path = path[:-1]
        # /health is open and must not leak model, budget, or rate-limit config.
        if path == "/health":
            return JSONResponse({"ok": True})
        if path in ("/bot-replies", "/bot-replies/ack"):
            settings = get_state().settings
            if not settings.mailbox_enabled:
                log.info("auth=mailbox_disabled path=%s", path)
                return JSONResponse({"error": "mailbox disabled"}, status_code=503)
            header = request.headers.get("authorization", "")
            if not authorized(header, settings.mailbox_token):
                log.info("auth=rejected path=%s", path)
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            if path == "/bot-replies/ack":
                return await _handle_bot_replies_ack(request)
            return await _handle_bot_replies(request)
        header = request.headers.get("authorization", "")
        if not authorized(header, get_state().settings.token):
            log.info("auth=rejected path=%s", request.url.path)
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if path == "/status":
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
