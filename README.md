# Bookly Support Agent

A conversational customer support agent prototype for **Bookly**, a fictional online bookstore. Built as a Decagon Solutions Engineering take-home exercise.

## Thesis

Great CX agents should **ask before they act** and **ground every factual claim in tools** — not free-form LLM generation. This demo implements explicit orchestration: classify intent → collect required slots → execute a tool → generate a natural-language response.

## Features

- **Web chat UI** with multi-turn conversations
- **Tool use**: order lookup, return initiation, knowledge-base search (mocked backend)
- **Clarifying questions** when intent is ambiguous or required info is missing
- **OpenAI integration** with deterministic mock fallback (no API key required for demo)

## Quick start

```bash
git clone https://github.com/YOUR_USERNAME/bookly-support-agent.git
cd bookly-support-agent
chmod +x run.sh
./run.sh
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000)

### Optional: enable live LLM

```bash
cp .env.example .env
# Add your OpenAI API key to .env
```

## Demo script

Try these flows to see multi-turn slot filling, tool use, and clarifying questions:

| Flow | Example messages |
|------|------------------|
| **Order status** | "Where is my order?" → provide `BK-10042` → provide `alex@example.com` |
| **Return** | "I want a refund" → `BK-10015` → `sam@example.com` → "Book arrived damaged" |
| **Clarifying question** | "I need help" → agent asks what you need |
| **Policy FAQ** | "What's your shipping policy?" → instant knowledge-base answer |

### Sample data

| Order ID | Email | Status |
|----------|-------|--------|
| BK-10042 | alex@example.com | shipped |
| BK-10087 | jamie@example.com | processing |
| BK-10015 | sam@example.com | delivered (return eligible) |

## Architecture

```
User → FastAPI /api/chat → AgentOrchestrator
                              ├─ Intent classification (LLM or mock)
                              ├─ Slot filling / clarifying questions
                              ├─ Tools (lookup_order, initiate_return, search_knowledge_base)
                              └─ Response generation (LLM or template)
```

See [`deck/pitch-deck.md`](deck/pitch-deck.md) for the full solution pitch.

## Project structure

```
app/
  main.py                 # FastAPI server
  agent/
    orchestrator.py       # Core agent pipeline
    memory.py             # Session state for multi-turn flows
    prompts.py            # System and task prompts
    tools/                # Mock tool implementations
  llm/client.py           # OpenAI + mock NLU
static/                   # Chat UI
deck/pitch-deck.md        # 5-slide pitch deck
```

## Tech stack

- Python 3.11+, FastAPI, Uvicorn
- OpenAI API (optional)
- Vanilla HTML/CSS/JS frontend

## License

MIT
