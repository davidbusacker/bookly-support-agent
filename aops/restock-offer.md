---
title: Restock Offer
description: Do not pitch mid-issue. The orchestrator applies this after the customer confirms resolution. Call only if you need the full restock rules.
startup: false
---

# AOP — Restock offer (demo)

## When it runs

The orchestrator asks **"Did we resolve everything today?"** after it decides the request is handled. Then:

1. Wait. Do **not** pitch yet.
2. If they already said they're done ("that's all", "that's everything", "I'm good") — even in the same message as the original ask — treat that as confirmation. Do not wait for another turn.
3. Only if they confirm they're good:
   - Read this caller's recent **agent traces**.
   - Pull a **full inventory snapshot** (`list_books`).
   - Check whether they previously asked about a book or author that was **out of stock**.

## If a match is now in stock

One direct sentence. Offer that title, or another in-stock title by the same author. Ask if they want to buy.

Good: "Quick note — Salt Light by Dana Feldman is back in stock. Want me to add a copy?"
Bad: stacking several titles, or pitching before they confirm the issue is done.

## Do not

- Pitch during an unresolved issue, or before they confirm.
- Offer if they never asked about availability / out of stock.
- Repeat the offer in the same session.
- Soften into a generic "anything else?"
