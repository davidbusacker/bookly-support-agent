---
title: Restock Offer
description: Do not pitch mid-issue. The orchestrator applies this after the customer confirms resolution. Call only if you need the full restock rules.
startup: false
---

# AOP — Restock offer (demo)

## When it runs

The orchestrator (not Riley) runs this after the customer confirms the support issue is done. Riley must not pitch books during an open issue.

1. Read this caller's recent **agent traces**.
2. Pull **`get_inventory`** (format-level stock — hardcover / paperback / ebook / audiobook are separate SKUs).
3. Offer only if they previously asked about a title/author that was **out of stock**, and that title (or another title by the same author) now has **at least one in-stock format**.

## Inventory rule

Never offer a format that is not in the snapshot. A title can be "in stock" as paperback while hardcover does not exist or is 0. Pick one in-stock SKU (ISBN) and stick to it. Do not ask which format they want.

## If a match is now in stock — one yes/no, then done

Pitch (one sentence, no format question):
*"Hey, looks like The Blue Hour Diaries by Nina Okonkwo is back in stock — want me to put in an order for you?"*

If they say **yes** (yeah, sure, go ahead, I'd love to): place **one copy** of the already-chosen in-stock SKU, then close:
*"Great — that's order BK-xxxxx. Thanks so much, have a nice day."*

If they say **no**: thank them and end. Do not keep selling.

## Do not

- Pitch during an unresolved issue, or before they confirm.
- Offer if they never asked about availability / out of stock.
- Ask quantity, format, or shipping — those are already decided.
- Repeat the offer in the same session.
- Soften into a generic "anything else?"
