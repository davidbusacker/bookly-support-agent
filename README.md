# Bookly Support Agent

Conversational support agent for **Bookly**, built in a **Decagon-style AOP architecture**.

## Architecture

```
aops/*.md              Agent Operating Policies (title + description catalog; startup AOPs inlined)
  ↓ read at startup / via read_aop skill
Claude (Riley)         Reasons over policies + customer context
  ↓ tool calls
Bookly MCP API         Orders, returns, refunds (live backend)
  ↓
skills.py              Thin helpers only (verify_phone, read_aop dispatch)
bookly_client.py       MCP client → Bookly /mcp (REST fallback)
trace_client.py        Admin audit logs → bookly.davidbusacker.com/admin/agent-traces
guardrails.py          Orchestrator classifiers — intent (<50% clarify), resolution (≥90%), restock
app.py                 Flask server + orchestration loop + TTS
```

**Business rules live in markdown, not Python.** Change loyalty, verification, or tone by editing `aops/`. Add a new AOP by adding a `.md` file with `title` / `description` frontmatter — the system prompt catalog and `read_aop` enum update automatically.

## Agent Operating Policies

| AOP | Purpose |
|-----|---------|
| `aops/core.md` | Role, tool discipline |
| `aops/conversation-style.md` | Tone, progressive disclosure |
| `aops/identity-verification.md` | Phone last-4 before account data |
| `aops/returns-and-refunds.md` | Standard return flow |
| `aops/loyalty-early-refund.md` | 3-order early refund — agent reads, counts orders, decides |
| `aops/restock-offer.md` | After customer confirms resolution, offer a restocked title from prior traces |

## Skills (minimal code)

| Skill | Purpose |
|-------|---------|
| `read_aop` | Load full policy text on demand |
| `verify_phone_last_four` | Pass/fail phone check; sets session authenticated for tool ACL |
| 42× Bookly tools | Live MCP at `https://bookly.davidbusacker.com/mcp` (REST `tools.json` fallback) |

## Quick start

```bash
./run.sh
```

Open **http://127.0.0.1:5000** in Chrome for voice.

## License

MIT
