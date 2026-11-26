# grok-mcp-bridge

A small [MCP](https://modelcontextprotocol.io) server that bridges Claude (or any MCP client) to **Grok** (xAI). It exposes two tools that forward a prompt to the xAI [Responses API](https://docs.x.ai/) and return Grok's answer as text. Each call is one-shot and stateless — no conversation history is kept, and responses are not streamed. Built on FastMCP over streamable HTTP.

Use it to get a second opinion from Grok inside a Claude workflow, or to pull in Grok's live web/X search results.

## Tools

| Tool | Parameters | When to use |
| --- | --- | --- |
| `ask_grok` | `prompt` (str, required), `system` (str, optional) | Ask Grok a question and get its answer. Good for a second opinion, a critique, or an alternative approach. **No live data.** |
| `ask_grok_live` | `prompt` (str, required), `system` (str, optional) | Same as `ask_grok`, but with Grok's live **web search** and **X (Twitter) search** enabled. Use for current events, market chatter, or sentiment on a ticker or topic. |

Both tools return a plain string. On an upstream error the string is prefixed `Grok API error <status>: ...` rather than raising.

## Environment variables

| Name | Required | Default | Description |
| --- | --- | --- | --- |
| `XAI_API_KEY` | Yes | — | Your paid xAI API key. Sent as `Authorization: Bearer` to `https://api.x.ai/v1/responses`. |
| `BRIDGE_TOKEN` | Yes | — | Shared secret that MCP clients must present as `Authorization: Bearer <BRIDGE_TOKEN>`. Use a long, random value. |
| `GROK_MODEL` | No | `grok-4.3` | The xAI model name to call. |
| `PORT` | No | `8000` | Port the HTTP server binds to (`0.0.0.0:$PORT`). |

The server reads `XAI_API_KEY` and `BRIDGE_TOKEN` at startup and will fail fast (`KeyError`) if either is missing.

## Endpoints

| Path | Auth | Purpose |
| --- | --- | --- |
| `/mcp` | Bearer token required | The MCP streamable-HTTP endpoint (JSON responses, stateless). This is the URL you register with an MCP client. |
| `/health` | Open (no auth) | Health check. Returns `{"ok": true}`. |

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
  -e PORT="8000" \
  grok-mcp-bridge
```

Check it's up (no token needed for `/health`):

```bash
curl http://localhost:8000/health
# {"ok": true}
```

The MCP endpoint itself is at `http://localhost:8000/mcp` and requires the bearer token.

## Security notes

- **Bearer-token auth is the only gate.** Every request to `/mcp` must send `Authorization: Bearer <BRIDGE_TOKEN>`; `/health` is intentionally open. There is no per-user auth, rate limiting, or IP allow-listing built in.
- **DNS-rebinding protection is disabled** (`enable_dns_rebinding_protection=False`). Because of this, the server **must** sit behind an HTTPS reverse proxy (Render, Fly.io, nginx, Caddy, etc.) and **must never be exposed over plain HTTP** on a public network. Terminate TLS at the proxy and forward to the container.
- **Use a strong `BRIDGE_TOKEN`** — a long random string (e.g. `openssl rand -hex 32`). Anyone with the token can spend your xAI credits.
- **`XAI_API_KEY` is a paid xAI key.** Treat it as a billing credential.
- **Never commit secrets.** Keep `XAI_API_KEY` and `BRIDGE_TOKEN` in your platform's secret store or a local `.env` that is git-ignored. See `.env.example` for the shape.

## Connect to Claude Code

The bridge speaks MCP over streamable HTTP, so you can register the deployed HTTPS endpoint as an MCP connector. You need two things:

- **Server URL:** your deployment's HTTPS origin plus the `/mcp` path, e.g. `https://your-app.onrender.com/mcp`
- **Auth header:** `Authorization: Bearer <BRIDGE_TOKEN>`

### Claude Code CLI

```bash
claude mcp add --transport http grok-bridge https://your-app.onrender.com/mcp \
  --header "Authorization: Bearer <BRIDGE_TOKEN>"
```

### MCP JSON config

Clients that take a JSON config can use a streamable-HTTP entry like:

```json
{
  "mcpServers": {
    "grok-bridge": {
      "type": "http",
      "url": "https://your-app.onrender.com/mcp",
      "headers": {
        "Authorization": "Bearer <BRIDGE_TOKEN>"
      }
    }
  }
}
```

Exact UI steps vary by client, but the essentials are always the same: point it at the `https://.../mcp` endpoint as a **streamable-HTTP** MCP server and attach the `Authorization: Bearer <BRIDGE_TOKEN>` header. Once connected, the `ask_grok` and `ask_grok_live` tools become available.
