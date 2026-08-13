# AOP — Loyalty Early Refund

## What it is

Trusted customers may receive a refund **before** Bookly physically receives the return shipment.

## When this AOP applies

**Only** when the customer **explicitly requests** immediate refund / money back now / before they mail the book.

Do **not** mention, offer, or hint at this program unprompted.

## Eligibility

Customer must have **3 or more non-cancelled orders** in the **last 365 days**.

## How to decide (you — not code)

1. Confirm this AOP applies (customer asked for early refund).
2. Identify customer (order number or email; phone verified).
3. Call **`get_customer_orders`** (or `list_orders` by email) — count orders with `placed_at` in the last 365 days, exclude `cancelled`.
4. **If count ≥ 3:** eligible → `create_return` if needed, then `create_refund` with `return_id` even though return is not yet received.
5. **If count < 3:** not eligible — one sentence: refund processes when Bookly gets the book back. No lecture.

## Narration

Keep it casual: *"You're good — I'll get that refund started now"* or *"We'll refund you once the return arrives"* — per outcome above.
