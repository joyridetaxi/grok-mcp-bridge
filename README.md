# grok-mcp-bridge

A small [MCP](https://modelcontextprotocol.io) server that bridges Claude (or any MCP client) to **Grok** (xAI). It exposes two tools that forward a prompt to the xAI [Responses API](https://docs.x.ai/) and return Grok's answer as text. Each call is one-shot and stateless — no conversation history is kept, and responses are not streamed. Built on FastMCP over streamable HTTP.

Use it to get a second opinion from Grok inside a Claude workflow, or to pull in Grok's live web/X search results.

How Claude and Grok Bot should hand work back and forth (and what still goes to Darren) is in [HANDOFF.md](./HANDOFF.md).

## Tools

| Tool | Parameters | When to use |
| --- | --- | --- |
| `ask_grok` | `prompt` (str, required), `system` (str, optional) | Ask Grok a question and get its answer. Good for a second opinion, a critique, or an alternative approach. **No live data.** |
| `ask_grok_live` | `prompt` (str, required), `system` (str, optional) | Same as `ask_grok`, but with Grok's live **web search** and **X (Twitter) search** enabled. Use for current events, market chatter, or sentiment on a ticker or topic. |

Both tools return a plain string. On an upstream error the string is `Grok API error <status>` (no response body). Over-budget, over-rate, or over-length prompts return a short `Error: ...` string and do not call xAI.

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

The server fails fast at startup if `XAI_API_KEY` or `BRIDGE_TOKEN` is missing. Spend is estimated from the xAI `usage` field when present, otherwise from a 4-chars-per-token heuristic. Set the same $15 cap in the **xAI console** as well — that is the real hard limit, because a Render disk wipe can reset `USAGE_FILE`.

## Endpoints

| Path | Auth | Purpose |
| --- | --- | --- |
| `/mcp` | Bearer token required | The MCP streamable-HTTP endpoint (JSON responses, stateless). This is the URL you register with an MCP client. |
| `/health` | Open (no auth) | Health check. Returns `ok`, the model name, month-to-date budget totals, and the rate-limit setting. No secrets. |

## Existing Render service

A service named `grok-mcp-bridge` already exists and is live:

- URL: `https://grok-mcp-bridge-8dgo.onrender.com`
- `/health` currently returns `{"ok": true}` (older image; the richer health body ships when this branch is deployed)
- Auto-deploy from `main` is **on**. Merging to `main` will deploy this repo. Do not create a second Blueprint service.

Set `healthCheckPath` to `/health` on that service if it is still empty. `XAI_API_KEY` and `BRIDGE_TOKEN` are already configured there.

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

## Security notes

- **Bearer-token auth is the only identity gate.** Every request except `/health` must send `Authorization: Bearer <BRIDGE_TOKEN>`. The comparison is constant-time and the `Bearer` scheme is case-insensitive. There is no per-user auth or IP allow-listing built in.
- **Rate limiting and a $15/month cost cap are enabled by default.** They are a guard, not a substitute for the xAI console spend cap. Anyone with the token can still spend up to that cap.
- **DNS-rebinding protection is disabled** (`enable_dns_rebinding_protection=False`). Because of this, the server **must** sit behind an HTTPS reverse proxy (Render, Fly.io, nginx, Caddy, etc.) and **must never be exposed over plain HTTP** on a public network. Terminate TLS at the proxy and forward to the container.
- **Use a strong `BRIDGE_TOKEN`** — at least 32 random characters (e.g. `openssl rand -hex 32`). Anyone with the token can spend your xAI credits up to the cap.
- **`XAI_API_KEY` is a paid xAI key.** Treat it as a billing credential.
- **Logs record tool name, status, duration, prompt length, token counts, and cost only.** They never record secrets, prompt text, or response text.
- **Never commit secrets.** Keep `XAI_API_KEY` and `BRIDGE_TOKEN` in your platform's secret store or a local `.env` that is git-ignored. See `.env.example` for the shape.

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

Exact UI steps vary by client, but the essentials are always the same: point it at the `https://.../mcp` endpoint as a **streamable-HTTP** MCP server and attach the `Authorization: Bearer <BRIDGE_TOKEN>` header. Once connected, the `ask_grok` and `ask_grok_live` tools become available.

Do not print or paste the real `BRIDGE_TOKEN` into a ticket or a chat message that does not need it.
