# Bookly Support Agent

Conversational customer support agent for **Bookly** (fictional online bookstore).
Built for the Decagon Solutions Engineering take-home.

## Architecture

```
Browser (chat + mic via Web Speech API)
  → POST /api/chat   (Claude tool-use loop — prompt-guided, agentic)
  → POST /api/tts    (ElevenLabs text-to-speech)
```

Claude decides when to call tools based on the conversation. No slot-filling
state machine — guardrails live in the system prompt and tool schemas.

## Quick start

```bash
chmod +x run.sh
./run.sh
```

Open **http://127.0.0.1:5000** in **Chrome** (required for microphone / STT).

Copy `.env.example` to `.env` and add your keys if needed:

```bash
cp .env.example .env
```

## Demo flows

| Flow | Try saying |
|------|------------|
| Order status | "What's the status of order BK-1001?" |
| Clarifying question | "Where's my order?" (no ID — agent asks) |
| Multi-turn refund | "I want a refund" → BK-1003 → "book arrived damaged" |
| Policy | "How long do I have to return something?" |
| Out of scope | "Recommend a sci-fi novel" (agent declines) |

Mock order IDs: `BK-1001`, `BK-1002`, `BK-1003`.

## Voice

- **STT (speech in):** Browser Web Speech API — fast, free, works on localhost in Chrome
- **TTS (speech out):** ElevenLabs via `/api/tts`
- Mic auto-sends after transcription; toggle spoken replies in the UI

## Files

| File | Purpose |
|------|---------|
| `app.py` | Flask server + Claude tool-use orchestration loop |
| `tools.py` | Mock Bookly backend + tool schemas |
| `templates/index.html` | Chat UI with mic + voice playback |

## License

MIT
