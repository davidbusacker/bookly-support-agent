# Bookly Agent Operating Policies (AOPs)

Instructions Riley follows at runtime. Edit these files to change agent behavior — not Python.

| ID | File | When to read |
|----|------|--------------|
| `core` | core.md | Always loaded at startup |
| `conversation-style` | conversation-style.md | Always loaded at startup |
| `identity-verification` | identity-verification.md | Always loaded at startup |
| `returns-and-refunds` | returns-and-refunds.md | Returns, refunds, cancellations |
| `loyalty-early-refund` | loyalty-early-refund.md | Customer asks for money back before sending the return |
| `restock-offer` | restock-offer.md | After customer confirms resolution: offer a restocked title |

Use the `read_aop` skill to load a policy in full when the situation calls for it.
