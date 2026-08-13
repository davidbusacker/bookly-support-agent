# AOP — Identity Verification

## Rule

Do **not** reveal order or customer information (status, items, address, tracking, refunds, email on file, etc.) until the customer verifies the **last 4 digits** of the primary phone on their account.

Public policy/FAQ text does not require verification.

## Flow

1. Customer asks about an account or order → get **order number or email** (one ask).
2. Ask for **last 4 digits of phone on file** (separate turn).
3. Call skill **`verify_phone_last_four`** with order_id or email + last_four.
4. If `verified: true` → proceed with Bookly lookup tools.
5. If false → ask them to try again. **Never** reveal the correct digits.

## Skill

`verify_phone_last_four` — returns pass/fail only, no account data.
