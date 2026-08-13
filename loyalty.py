"""
Customer loyalty early-refund clause.

Loyal customers (3+ purchases in the last 365 days) may receive a refund before
Bookly has physically received the return. Enforced when create_refund is called
against an open return that has not yet been received.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

LOYALTY_MIN_PURCHASES = 3
LOYALTY_WINDOW_DAYS = 365

# Return statuses that mean the package has not been received yet
OPEN_RETURN_STATUSES = frozenset({"requested", "label_sent", "in_transit"})

CHECK_LOYALTY_TOOL: dict[str, Any] = {
    "name": "check_loyalty_early_refund",
    "description": (
        "Check whether the customer qualifies for the loyalty early-refund program "
        "(3+ purchases in the last year). Use before promising a refund before their "
        "return shipment is received. Provide order_id or email."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "Bookly order number or UUID."},
            "email": {"type": "string", "description": "Customer email."},
        },
        "additionalProperties": False,
    },
}


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def resolve_customer_id(bookly_execute: Callable, *, order_id: str | None, email: str | None) -> str | None:
    """Resolve customer UUID from order number or email."""
    if order_id:
        result = bookly_execute("get_order", {"id": order_id})
        if result.get("ok"):
            data = result.get("data", {})
            customer = data.get("customer") or {}
            return customer.get("id") or data.get("customer_id")

    if email:
        result = bookly_execute("list_customers", {"email": email})
        if result.get("ok"):
            customers = result.get("data") or []
            if customers:
                return customers[0].get("id")

    return None


def count_purchases_last_year(bookly_execute: Callable, customer_id: str) -> int:
    """Count non-cancelled orders placed in the last 365 days."""
    result = bookly_execute("get_customer_orders", {"id": customer_id, "limit": 100})
    if not result.get("ok"):
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=LOYALTY_WINDOW_DAYS)
    count = 0
    for order in result.get("data") or []:
        placed_at = order.get("placed_at")
        if not placed_at:
            continue
        if _parse_dt(placed_at) < cutoff:
            continue
        if order.get("status") == "cancelled":
            continue
        count += 1
    return count


def loyalty_status(bookly_execute: Callable, *, order_id: str | None = None, email: str | None = None) -> dict[str, Any]:
    """Return loyalty eligibility summary for the agent (no PII beyond counts)."""
    customer_id = resolve_customer_id(bookly_execute, order_id=order_id, email=email)
    if not customer_id:
        return {
            "ok": True,
            "eligible": False,
            "purchase_count_last_year": 0,
            "required_purchases": LOYALTY_MIN_PURCHASES,
            "message": "Could not find customer for loyalty check.",
        }

    count = count_purchases_last_year(bookly_execute, customer_id)
    eligible = count >= LOYALTY_MIN_PURCHASES
    return {
        "ok": True,
        "eligible": eligible,
        "purchase_count_last_year": count,
        "required_purchases": LOYALTY_MIN_PURCHASES,
        "message": (
            "Customer qualifies for loyalty early refund — refund may be issued before the return is received."
            if eligible
            else f"Loyalty early refund requires {LOYALTY_MIN_PURCHASES}+ purchases in the last year "
            f"(customer has {count}). Standard process: refund after Bookly receives the return."
        ),
    }


def _return_awaiting_receipt(bookly_execute: Callable, return_id: str) -> bool:
    """True when the RMA exists but Bookly has not marked it received yet."""
    result = bookly_execute("get_return", {"rma": return_id})
    if not result.get("ok"):
        return False

    ret = result.get("data", {})
    if ret.get("received_at"):
        return False
    return ret.get("status") in OPEN_RETURN_STATUSES


def gate_early_refund(
    bookly_execute: Callable,
    tool_input: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Block create_refund when it would refund before return receipt unless loyalty applies.
    Returns an error dict to short-circuit, or None to allow the API call.
    """
    return_id = tool_input.get("return_id")
    if not return_id:
        # Goodwill / non-return refunds are not covered by this gate
        return None

    if not _return_awaiting_receipt(bookly_execute, return_id):
        return None

    order_id = tool_input.get("order_id")
    customer_id = resolve_customer_id(bookly_execute, order_id=order_id, email=None)
    if not customer_id and order_id:
        order = bookly_execute("get_order", {"id": order_id})
        if order.get("ok"):
            customer_id = (order.get("data", {}).get("customer") or {}).get("id")

    if not customer_id:
        return {
            "ok": False,
            "error": "loyalty_check_failed",
            "detail": "Could not verify customer for loyalty early refund.",
        }

    count = count_purchases_last_year(bookly_execute, customer_id)
    if count >= LOYALTY_MIN_PURCHASES:
        return None  # allowed — annotate reason in wrapper if needed

    return {
        "ok": False,
        "error": "loyalty_early_refund_denied",
        "purchase_count_last_year": count,
        "required_purchases": LOYALTY_MIN_PURCHASES,
        "detail": (
            "This refund would be issued before Bookly receives the return. "
            f"Loyalty early refund requires {LOYALTY_MIN_PURCHASES}+ purchases in the last year; "
            f"this customer has {count}. Refund after the return is received, or use receive_return with auto_refund."
        ),
    }
