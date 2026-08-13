# Bookly Support Agent

Conversational customer support agent for **Bookly**, wired to the live
[Bookly Support API](https://bookly-agent-api.lovable.app) on Lovable.

## Architecture

```
Browser (chat + mic)
  → POST /api/chat   Claude tool-use loop (33 tools from MCP manifest)
  → Bookly API      https://bookly-agent-api.lovable.app/api/public/v1/...
  → POST /api/tts    ElevenLabs text-to-speech
```

Claude calls real read/write endpoints (orders, returns, refunds, policies,
tickets, etc.) via the MCP-style manifest at `/api/public/tools.json`.

## Quick start

```bash
chmod +x run.sh
./run.sh
```

Open **http://127.0.0.1:5000** in **Chrome** for voice.

## API integration

| Resource | URL |
|----------|-----|
| Tool manifest | https://bookly-agent-api.lovable.app/api/public/tools.json |
| OpenAPI | https://bookly-agent-api.lovable.app/api/public/openapi.json |
| LLM overview | https://bookly-agent-api.lovable.app/llms.txt |

Tools are loaded from `BOOKLY_MANIFEST_URL` at startup (cached in `data/bookly_tools.json`).

## Demo flows (live data)

| Flow | Example |
|------|---------|
| Order status | "What's the status of order BK-10001?" |
| Lookup by email | "Find orders for priya.nair@example.com" |
| Return | "I want to return BK-10003, it arrived damaged" |
| Policy | "What's your return policy?" |
| Password reset | "I can't log in — email is ava.brooks@example.com" |

Call `GET /api/public/v1/meta` for fresh sample IDs.

## Files

| File | Purpose |
|------|---------|
| `app.py` | Flask + Claude orchestration loop |
| `bookly_client.py` | Manifest loader + HTTP tool executor |
| `tools.py` | Thin wrapper exposing tools to the agent |
| `data/bookly_tools.json` | Cached MCP manifest |
| `templates/index.html` | Chat + voice UI |

## License

MIT
