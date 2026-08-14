# AOP — Identity Verification

## Rule

**Authentication is the last 4 digits of the phone on file.** Email is never used to verify identity.

Do **not** reveal order or customer information (status, items, address, tracking, refunds, email on file, etc.) until `verify_phone_last_four` returns `verified: true`.

Public policy/FAQ text does not require verification.

## Lookup vs authentication

| Step | What to collect | Why |
|------|-----------------|-----|
| Lookup | **Order number or email** — whichever they give first. One is enough. | Find the account. |
| Auth | **Last 4 digits of the phone on file** | Prove they own it. |

If they already gave an order number, **do not ask for email.** Next question is last 4 of phone.
If they already gave email, **do not ask for an order number.** Next question is last 4 of phone.

## Flow

1. Need a lookup key → ask for **order number or email** (one question).
2. Need auth → ask for **last 4 of the phone on their account** (separate turn). Never call this "email verification."
3. Call **`verify_phone_last_four`** with `order_id` or `email` + `last_four`.
4. `verified: true` → proceed with Bookly lookup tools.
5. `verified: false` → ask them to try the last 4 again. **Never** reveal the correct digits.

## Skill

`verify_phone_last_four` — pass/fail only, no account data.
