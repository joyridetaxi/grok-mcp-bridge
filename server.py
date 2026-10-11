"""
Grok MCP bridge: lets Manus (an MCP client) call Grok through the xAI API.

Tools exposed:
  - ask_grok(prompt, system?)       plain reasoning / second opinion
  - ask_grok_live(prompt, system?)  Grok with live web + X (Twitter) search

Env vars:
  XAI_API_KEY    required  your xAI API key
  BRIDGE_TOKEN   required  shared secret; Manus sends it as "Authorization: Bearer <token>"
  GROK_MODEL     optional  default "grok-4.3" (check docs.x.ai for the current name)
  PORT           optional  default 8000

Run:  python server.py     (MCP endpoint will be at /mcp)
"""

import os
import secrets

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

XAI_API_KEY = os.environ["XAI_API_KEY"]
BRIDGE_TOKEN = os.environ["BRIDGE_TOKEN"]
GROK_MODEL = os.environ.get("GROK_MODEL", "grok-4.3")
XAI_URL = "https://api.x.ai/v1/responses"

mcp = FastMCP("grok-bridge", stateless_http=True, json_response=True)


def _extract_text(data: dict) -> str:
    """Pull the assistant text out of an xAI Responses API payload."""
    if isinstance(data.get("output_text"), str) and data["output_text"]:
        return data["output_text"]
    parts = []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for block in item.get("content", []):
                if block.get("type") in ("output_text", "text") and block.get("text"):
                    parts.append(block["text"])
    return "\n".join(parts) or str(data)


async def _call_grok(prompt: str, system: str | None, tools: list[dict] | None) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body: dict = {"model": GROK_MODEL, "input": messages}
    if tools:
        body["tools"] = tools

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            XAI_URL,
            headers={"Authorization": f"Bearer {XAI_API_KEY}"},
            json=body,
        )
    if resp.status_code != 200:
        return f"Grok API error {resp.status_code}: {resp.text[:500]}"
    return _extract_text(resp.json())


@mcp.tool()
async def ask_grok(prompt: str, system: str | None = None) -> str:
    """Ask Grok a question and get its answer. Use for a second opinion,
    critique, or alternative approach. No live data."""
    return await _call_grok(prompt, system, tools=None)


@mcp.tool()
async def ask_grok_live(prompt: str, system: str | None = None) -> str:
    """Ask Grok with live web search and X (Twitter) search enabled. Use for
    current events, market chatter, or sentiment on a ticker or topic."""
    return await _call_grok(
        prompt,
        system,
        tools=[{"type": "web_search"}, {"type": "x_search"}],
    )


class BearerAuth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path == "/health":
            return JSONResponse({"ok": True})
        header = request.headers.get("authorization", "")
        expected = f"Bearer {BRIDGE_TOKEN}"
        if not secrets.compare_digest(header, expected):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


app = mcp.streamable_http_app()
app.add_middleware(BearerAuth)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
