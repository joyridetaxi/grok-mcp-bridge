# Handoff: Grok Bot and Claude

This is how the two coding agents should work together on Joyride and this bridge, with as little back-and-forth through Darren as possible.

## Who does what

- **Claude** (external coding agent, commits as `claude`) implements application code, opens pull requests, and drives CI to green on those PRs.
- **Grok Bot** (Darren's in-chat agent) reviews PRs and CI logs, checks Render and GitHub, drafts docs or hardening on a PR branch when asked, and talks to Darren.
- **Darren** is the owner. He is the only person who can approve spend, production changes, merges, and legal or public statements.

Claude should not wait for Darren to paste a wall of CI logs if the answer is already on GitHub. Grok Bot should not wait for Claude to "confirm green" when the checks API already shows the result.

## How to exchange work

1. Put the current status on the pull request: what changed, what is blocked, and the exact next step. The other agent will read the PR, checks, and job logs directly.
2. Do not edit the same branch at the same time. If Grok Bot pushes to a Claude-owned branch (this repo's `claude/deploy-config` is an example), the commit message must say so clearly so Claude pulls or rebases before pushing again.
3. Prefer a short PR comment over asking Darren to relay. Darren should only have to say "yes" or "no" on decisions listed below.
4. Once this bridge is deployed and connected to Claude Code as an MCP server, Claude can call `ask_grok` (second opinion, critique) or `ask_grok_live` (current info, web/X search) in-session. That does not replace Darren on spend, production, or merges.
5. On Joyride repos, Claude still waits for Darren to say `commit` before committing or pushing, unless Darren has said otherwise for that task. On this draft PR, Grok Bot may push improvement commits to the PR branch only, never to `main`.

## What goes to Darren only

Ask Darren, and wait, before doing any of these:

- Spending money, creating paid keys, or raising a budget cap
- Merging a pull request
- Deploying to production, or changing production settings (including Joyride Render auto-deploy)
- Legal documents, contracts, or public statements
- Anything irreversible

## What not to bounce to Darren

- Reading CI logs and naming the actual failing step
- Review comments and requested code fixes
- Tests, docs, and hardening commits on an already-open draft/feature branch
- Re-adding `ready-to-merge` on `joyride-web` when the label is already present (it is not stripped on push)

## This service specifically

- A Render service already exists: `grok-mcp-bridge` (`srv-db5l78nlot8c73fbq0q0`), URL `https://grok-mcp-bridge-8dgo.onrender.com`. Do not create a second one.
- Auto-deploy is **on** for `main`. Merging this PR will deploy whatever is on the branch. Confirm with Darren before merge.
- `XAI_API_KEY` and `BRIDGE_TOKEN` are already set on that service (names only; never print values).
- Default monthly cap in this app is **$15 USD** (`MONTHLY_BUDGET_USD`). Also set the same cap in the xAI console; the in-process tally can reset if the instance disk is wiped. The console cap is the real hard limit.
- Never log or commit secrets, prompt text, or response text.
