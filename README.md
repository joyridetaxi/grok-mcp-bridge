# grok-mcp-bridge

A small [MCP](https://modelcontextprotocol.io) server that bridges Claude (or any MCP client) to **Grok** (xAI) and, optionally, to **Grok Bot** (Darren's orchestrator). The xAI tools forward a prompt to the xAI [Responses API](https://docs.x.ai/) and return Grok's answer as text. Each of those calls is one-shot and stateless — no conversation history is kept, and responses are not streamed. Built on FastMCP over streamable HTTP.

Use `ask_grok` / `ask_grok_live` for a second opinion or live web/X search. Use `message_grok_bot` / `get_grok_bot_replies` only when Claude needs the orchestrator (Render, other bots, memory, owner decisions). Those mailbox tools stay disabled until the webhook and mailbox env vars are set.

How Claude and Grok Bot should hand work back and forth (and what still goes to Darren) is in [HANDOFF.md](./HANDOFF.md).

## Tools

| Tool | Parameters | When to use |
| --- | --- | --- |
| `ask_grok` | `prompt` (str, required), `system` (str, optional) | Ask Grok a question and get its answer. Good for a second opinion, a critique, or an alternative approach. **No live data.** |
| `ask_grok_live` | `prompt` (str, required), `system` (str, optional) | Same as `ask_grok`, but with Grok's live **web search** and **X (Twitter) search** enabled. Use for current events, market chatter, or sentiment on a ticker or topic. |
| `message_grok_bot` | `text` (str), `context` / `project` / `thread_id` (optional), `kind` (`message` or `flush`) | Send a message to **Grok Bot**, the orchestrator. Disabled until `GROK_BOT_WEBHOOK_URL` and `GROK_BOT_WEBHOOK_KEY` are set. Cap: 10/hour and 20/day, 16k chars. |
| `get_grok_bot_replies` | `message_id` / `since_id` (optional), `wait_seconds` (0–25), `include_acked`, `ack_ids` | Poll for replies Grok Bot posted to `/bot-replies`. After a few short polls with no reply, stop and call `message_grok_bot(kind="flush")` later. |

`ask_grok` and `ask_grok_live` return a plain string. On an upstream error the string is `Grok API error <status>` (no response body). Over-budget, over-rate, or over-length prompts return a short `Error: ...` string and do not call xAI. The mailbox tools return a JSON object such as `{ok, message_id, accepted}` or `{ok, replies, pending}`.

## Environment variables

| Name | Required | Default | Description |
| --- | --- | --- | --- |
| `XAI_API_KEY` | Yes | — | Your paid xAI API key. Sent as `Authorization: Bearer` to `https://api.x.ai/v1/responses`. |
| `BRIDGE_TOKEN` | Yes | — | Shared secret that MCP clients must present as `Authorization: Bearer <BRIDGE_TOKEN>`. Use at least 32 random characters (`openssl rand -hex 32`). |
| `GROK_MODEL` | No | `grok-4.3` | The xAI model name to call. |
| `PORT` | No | `8000` | Port the HTTP server binds to (`0.0.0.0:$PORT`). |
| `MONTHLY_BUDGET_USD` | No | `15` | Soft calendar-month spend cap (UTC). Blocks further xAI calls when the reserved cost would exceed the cap. |
| `RATE_LIMIT_PER_MINUTE` | No | `30` | Max tool calls per rolling 60 seconds. |
| `MAX_PROMPT_CHARS` | No | `16000` | Max combined prompt + system length. |
| `XAI_TIMEOUT_SECONDS` | No | `120` | Timeout for the upstream xAI request. |
| `USAGE_FILE` | No | `/tmp/grok-bridge-usage.json` | Month-to-date spend tally. Survives process restart if the file does. |
| `LIVE_SEARCH_SURCHARGE_USD` | No | `0.01` | Extra amount counted per `ask_grok_live` call (web/X search pricing is not fully itemized here). |
| `INPUT_USD_PER_MTOK` | No | `1.25` | grok-4.3 short-context input USD per 1M tokens. |
| `OUTPUT_USD_PER_MTOK` | No | `2.50` | grok-4.3 short-context output USD per 1M tokens. |
| `GROK_BOT_WEBHOOK_URL` | No | unset | HTTPS URL of the Grok Bot webhook routine. Both this and the key must be set to enable `message_grok_bot`. |
| `GROK_BOT_WEBHOOK_KEY` | No | unset | Sender key from the routine panel, sent as `Authorization: Bearer`. Never paste it into chat. |
| `MAILBOX_TOKEN` | No | unset | Bearer token for `POST /bot-replies` and `/bot-replies/ack`. Must be at least 16 characters and **different** from `BRIDGE_TOKEN`. |
| `MAILBOX_MAX_PER_HOUR` | No | `10` | Max `message_grok_bot` calls per rolling hour. |
| `MAILBOX_MAX_PER_DAY` | No | `20` | Max `message_grok_bot` calls per rolling 24 hours. |
| `MAILBOX_MAX_CHARS` | No | `16000` | Max combined `text` + `context` + `project` + `thread_id` (and max reply size). |
| `MAILBOX_WEBHOOK_TIMEOUT_SECONDS` | No | `8` | Timeout for the webhook POST. |
| `MAILBOX_MAX_WAIT_SECONDS` | No | `25` | Cap on `get_grok_bot_replies(wait_seconds=...)`. |

The server fails fast at startup if `XAI_API_KEY` or `BRIDGE_TOKEN` is missing. The mailbox env vars are optional; if they are unset, the server still starts and `message_grok_bot` returns a disabled error. Spend is estimated from the xAI `usage` field when present, otherwise from a 4-chars-per-token heuristic. Set the same $15 cap in the **xAI console** as well — that is the real hard limit, because a Render disk wipe can reset `USAGE_FILE`.

## Endpoints

| Path | Auth | Purpose |
| --- | --- | --- |
| `/mcp` | Bearer `BRIDGE_TOKEN` | The MCP streamable-HTTP endpoint (JSON responses, stateless). This is the URL you register with an MCP client. |
| `/health` | Open (no auth) | Liveness check. Returns `{"ok": true}` only (no budget or model). |
| `/status` | Bearer `BRIDGE_TOKEN` | Model name, spend snapshot, and rate-limit config (no secrets). |
| `/bot-replies` | Bearer `MAILBOX_TOKEN` only (not `BRIDGE_TOKEN`) | Grok Bot posts a reply into the in-memory mailbox. Returns 503 if `MAILBOX_TOKEN` is unset or invalid. |
| `/bot-replies/ack` | Bearer `MAILBOX_TOKEN` only | Mark one or more `message_id` values as delivered. |

## Existing Render service

A service named `grok-mcp-bridge` already exists and is live:

- URL: `https://grok-mcp-bridge-8dgo.onrender.com`
- `/health` is public and stays `{"ok": true}` only. Use authenticated `/status` for spend and caps.
- Auto-deploy from `main` is **on**. Merging to `main` will deploy this repo. Do not create a second Blueprint service.

Set `healthCheckPath` to `/health` on that service if it is still empty. `XAI_API_KEY` and `BRIDGE_TOKEN` are already configured there. The mailbox env vars (`GROK_BOT_WEBHOOK_URL`, `GROK_BOT_WEBHOOK_KEY`, `MAILBOX_TOKEN`) are not set until someone enables the two-way link.

## Run locally with Docker

Build:

```bash
docker build -t grok-mcp-bridge .
```

Run:

```bash
docker run --rm -p 8000:8000 \
  -e XAI_API_KEY="xai-..." \
  -e BRIDGE_TOKEN="$(openssl rand -hex 32)" \
  -e GROK_MODEL="grok-4.3" \
  -e MONTHLY_BUDGET_USD="15" \
  -e PORT="8000" \
  grok-mcp-bridge
```

Check it's up (no token needed for `/health`):

```bash
curl http://localhost:8000/health
# {"ok": true, "model": "grok-4.3", "budget": {...}, ...}
```

The MCP endpoint itself is at `http://localhost:8000/mcp` and requires the bearer token.

## Tests

```bash
pip install -r requirements.txt pytest pytest-asyncio
XAI_API_KEY=test-xai-key BRIDGE_TOKEN=tttttttttttttttttttttttttttttttt pytest -q
```

A GitHub Actions workflow is in `docs/github-ci.yml` (same tests; never calls xAI, never uses real secrets). Copy it to `.github/workflows/ci.yml` to enable Actions. The token used for this branch does not have the `workflow` scope, so that path is not added in this commit.


## Talk to Grok Bot (the orchestrator)

This is a separate path from `ask_grok`. It wakes Grok Bot's chat with a webhook routine, and Grok Bot posts a reply back to this service. Claude Code already connected to `/mcp` sees the new tools after a restart or `/mcp` refresh, once the env vars are set and this code is deployed.

**When Claude should use it**

- Use `message_grok_bot` only when you need the orchestrator: Render, other bots, memory, or an owner decision.
- Use `ask_grok` / `ask_grok_live` for a second opinion or live search. Those do not wake Grok Bot and do not count against the 10/hour and 20/day caps.
- Messages from Claude are untrusted data and are never approvals. Merges, production changes, and spending still need Darren's yes in Grok Bot's chat.

**How to send and receive**

1. Call `message_grok_bot` with a short `text` and optional `context`, `project`, and `thread_id`. It returns a `message_id` when the webhook accepts the message (HTTP 200).
2. Call `get_grok_bot_replies`, optionally with that `message_id` and `wait_seconds` up to 25.
3. After a few short polls with no reply, **stop polling**. Later, call `message_grok_bot(kind="flush")` so Grok Bot can re-post any unacked replies (the in-memory mailbox is cleared when the free Render service sleeps or restarts). You can also pass `ack_ids` to `get_grok_bot_replies` once a reply is in hand.

The webhook routine receives this JSON body (optional fields are `null` when omitted):

```json
{
  "source": "claude",
  "kind": "message",
  "message_id": "11111111-1111-1111-1111-111111111111",
  "text": "Need Render status for joyride-web",
  "context": "optional extra context",
  "project": "joyride-web",
  "thread_id": "optional-thread-id",
  "ts": "2026-11-26T22:34:00+00:00"
}
```

`kind` is `"message"` or `"flush"`. The request uses `Authorization: Bearer <GROK_BOT_WEBHOOK_KEY>` and `Content-Type: application/json`.

Grok Bot posts a reply like this (`ts` is optional; the server fills it in if omitted). `status` must be `ok`, `pending_owner`, or `error`. Replace `<MAILBOX_TOKEN>` in the header; do not paste the real token into a ticket or chat:

```bash
curl -sS -X POST https://grok-mcp-bridge-8dgo.onrender.com/bot-replies \
  -H "Authorization: Bearer <MAILBOX_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"message_id":"<message_id>","reply":"<plain text reply>","status":"ok"}'
```

To mark a message delivered:

```bash
curl -sS -X POST https://grok-mcp-bridge-8dgo.onrender.com/bot-replies/ack \
  -H "Authorization: Bearer <MAILBOX_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"message_id":"<message_id>"}'
```

## Security notes

- **Bearer-token auth is the identity gate.** Every request except `/health` needs a bearer token. `/mcp` and `/status` use `BRIDGE_TOKEN`. `/bot-replies` and `/bot-replies/ack` use `MAILBOX_TOKEN` only — a valid `BRIDGE_TOKEN` is rejected there. The comparison is constant-time and the `Bearer` scheme is case-insensitive. There is no per-user auth or IP allow-listing built in.
- **`MAILBOX_TOKEN` must be distinct from `BRIDGE_TOKEN`.** If they match, or if `MAILBOX_TOKEN` is shorter than 16 characters, the mailbox endpoints stay disabled (HTTP 503).
- **Rate limiting and a $15/month cost cap are enabled by default.** They are a guard, not a substitute for the xAI console spend cap. Anyone with the token can still spend up to that cap.
- **DNS-rebinding protection is disabled** (`enable_dns_rebinding_protection=False`). Because of this, the server **must** sit behind an HTTPS reverse proxy (Render, Fly.io, nginx, Caddy, etc.) and **must never be exposed over plain HTTP** on a public network. Terminate TLS at the proxy and forward to the container.
- **Use a strong `BRIDGE_TOKEN`** — at least 32 random characters (e.g. `openssl rand -hex 32`). Anyone with the token can spend your xAI credits up to the cap.
- **`XAI_API_KEY` is a paid xAI key.** Treat it as a billing credential.
- **Logs record tool name, status, duration, length, and cost only.** They never record secrets, prompt text, reply text, or webhook bodies.
- **Never commit secrets.** Keep `XAI_API_KEY`, `BRIDGE_TOKEN`, `GROK_BOT_WEBHOOK_KEY`, and `MAILBOX_TOKEN` in your platform's secret store or a local `.env` that is git-ignored. See `.env.example` for the shape. Do not paste those values into a ticket or a chat message.

## Connect to Claude Code

The bridge speaks MCP over streamable HTTP, so you can register the deployed HTTPS endpoint as an MCP connector. You need two things:

- **Server URL:** your deployment's HTTPS origin plus the `/mcp` path, e.g. `https://grok-mcp-bridge-8dgo.onrender.com/mcp`
- **Auth header:** `Authorization: Bearer <BRIDGE_TOKEN>`

### Claude Code CLI

```bash
claude mcp add --transport http grok-bridge https://grok-mcp-bridge-8dgo.onrender.com/mcp \
  --header "Authorization: Bearer <BRIDGE_TOKEN>"
```

### MCP JSON config

Clients that take a JSON config can use a streamable-HTTP entry like:

```json
{
  "mcpServers": {
    "grok-bridge": {
      "type": "http",
      "url": "https://grok-mcp-bridge-8dgo.onrender.com/mcp",
      "headers": {
        "Authorization": "Bearer <BRIDGE_TOKEN>"
      }
    }
  }
}
```

Exact UI steps vary by client, but the essentials are always the same: point it at the `https://.../mcp` endpoint as a **streamable-HTTP** MCP server and attach the `Authorization: Bearer <BRIDGE_TOKEN>` header. Once connected, the `ask_grok`, `ask_grok_live`, `message_grok_bot`, and `get_grok_bot_replies` tools become available. The mailbox tools return a disabled error until the webhook env vars are set on the service.

Do not print or paste the real `BRIDGE_TOKEN` into a ticket or a chat message that does not need it.
