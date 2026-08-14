# Bookly Support Agent

Conversational support agent for **Bookly**, built in a **Decagon-style AOP architecture**.

## Architecture

```
aops/*.md              Agent Operating Policies (behavior — edit these)
  ↓ read at startup / via read_aop skill
Claude (Riley)         Reasons over policies + customer context
  ↓ tool calls
Bookly MCP API         Orders, returns, refunds (live backend)
  ↓
skills.py              Thin helpers only (verify_phone, read_aop dispatch)
bookly_client.py       HTTP execution of manifest tools
trace_client.py        Admin audit logs → bookly.davidbusacker.com/admin/agent-traces
intent.py              Intent confidence guardrail (<50% → clarify)
resolution.py          Resolution score (≥90% → restock check)
restock.py             Inventory snapshot + prior-trace restock offer
app.py                 Flask server + orchestration loop + TTS
```

**Business rules live in markdown, not Python.** Change loyalty, verification, or tone by editing `aops/`.

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
| `verify_phone_last_four` | Pass/fail phone check (referenced by identity AOP) |
| 33× Bookly tools | From [tools.json](https://bookly-agent-api.lovable.app/api/public/tools.json) |

## Quick start

```bash
./run.sh
```

Open **http://127.0.0.1:5000** in Chrome for voice.

## License

MIT
