# AOP — Returns and Refunds

## Standard path (default)

1. Confirm order and reason with the customer.
2. `return_eligibility` on the order — explain outcome in plain language.
3. `get_order_items` for line IDs if creating a return.
4. `create_return` → give RMA number and label info briefly.
5. **Refund timing:** money goes back after Bookly **receives** the return (`receive_return` / normal settlement). Do not promise instant refund unless loyalty AOP applies.

## Before any write action

- Phone verified per `identity-verification` AOP.
- Eligibility checked — do not skip `return_eligibility`.

## Cancellations

Processing/backordered orders: `cancel_order`. If 409, offer return instead.

## Do not volunteer

Early refunds, loyalty perks, or exceptions — unless the customer explicitly asks for money **before** shipping the book back (see `loyalty-early-refund` AOP).
