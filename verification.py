"""
Phone verification gate for order/customer data.

The agent must verify the last 4 digits of the primary phone on file before
any tool may return order or customer information. Enforced in code, not prompt-only.
"""

from __future__ import annotations

import re
from typing import Any

# Tools that may return or act on order/customer data — blocked until verified
GATED_TOOLS: frozenset[str] = frozenset(
    {
        "list_orders",
        "get_order",
        "get_order_items",
        "get_order_shipments",
        "cancel_order",
        "reship_order",
        "return_eligibility",
        "list_returns",
        "create_return",
        "get_return",
        "receive_return",
        "cancel_return",
        "list_refunds",
        "create_refund",
        "get_refund",
        "list_transactions",
        "list_customers",
        "get_customer",
        "get_customer_orders",
        "get_shipment",
        "get_shipment_events",
        "list_tickets",
        "create_ticket",
        "update_ticket",
        "address_change",
        "check_loyalty_early_refund",
    }
)

# Custom tool — always allowed; returns verified true/false only (no PII)
VERIFY_PHONE_TOOL: dict[str, Any] = {
    "name": "verify_phone_last_four",
    "description": (
        "Verify the customer by matching the last 4 digits of the primary phone on file. "
        "Call this BEFORE revealing any order or customer details. Provide order_id OR email, "
        "plus last_four. Returns only verified:true/false — never exposes account data."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "order_id": {
                "type": "string",
                "description": "Bookly order number, e.g. BK-10001.",
            },
            "email": {
                "type": "string",
                "description": "Customer email on the account.",
            },
            "last_four": {
                "type": "string",
                "description": "Last 4 digits of the phone number on file, e.g. 0170.",
            },
        },
        "required": ["last_four"],
        "additionalProperties": False,
    },
}

# session_id -> set of verified identity keys (order:BK-10001, email:user@example.com, ...)
SESSION_VERIFIED: dict[str, set[str]] = {}


def init_session(session_id: str) -> None:
    SESSION_VERIFIED[session_id] = set()


def phone_last_four(phone: str | None) -> str | None:
    """Extract last 4 digits from a phone string like +1-408-555-0170."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    return digits[-4:] if len(digits) >= 4 else None


def _norm_order(order_id: str) -> str:
    return order_id.strip().upper()


def _norm_email(email: str) -> str:
    return email.strip().lower()


def identity_keys_from_tool(tool_name: str, tool_input: dict[str, Any]) -> set[str]:
    """Map a gated tool call to the identity keys it requires."""
    keys: set[str] = set()

    order_id = tool_input.get("order_id") or tool_input.get("id")
    if order_id and tool_name not in {"get_customer", "get_return", "get_refund", "update_ticket"}:
        keys.add(f"order:{_norm_order(str(order_id))}")

    email = tool_input.get("email") or tool_input.get("customer_email")
    if email:
        keys.add(f"email:{_norm_email(str(email))}")

    order_number = tool_input.get("order_number")
    if order_number:
        keys.add(f"order:{_norm_order(str(order_number))}")

    customer_id = tool_input.get("customer_id")
    if customer_id:
        keys.add(f"customer:{str(customer_id).strip()}")

    if tool_name == "get_customer" and tool_input.get("id"):
        keys.add(f"customer:{str(tool_input['id']).strip()}")

    if tool_name == "get_return" and tool_input.get("rma"):
        keys.add(f"return:{str(tool_input['rma']).strip().upper()}")

    if tool_name == "get_refund" and tool_input.get("id"):
        keys.add(f"refund:{str(tool_input['id']).strip().upper()}")

    if tool_name == "get_shipment" and tool_input.get("tracking"):
        keys.add(f"tracking:{str(tool_input['tracking']).strip().upper()}")

    if tool_name == "update_ticket" and tool_input.get("id"):
        keys.add(f"ticket:{str(tool_input['id']).strip().upper()}")

    # Broad list/search without identity — require any prior verification in session
    if tool_name in {"list_orders", "list_returns", "list_refunds", "list_transactions", "list_customers", "list_tickets"}:
        if not keys:
            keys.add("_any_verified")

    return keys


def is_verified(session_id: str, required_keys: set[str]) -> bool:
    verified = SESSION_VERIFIED.get(session_id, set())
    if not verified:
        return False
    if "_any_verified" in required_keys and verified:
        return True
    return bool(required_keys & verified)


def mark_verified(session_id: str, keys: set[str]) -> None:
    if session_id not in SESSION_VERIFIED:
        SESSION_VERIFIED[session_id] = set()
    SESSION_VERIFIED[session_id].update(keys)


def verification_required_response(tool_name: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "verification_required",
        "detail": (
            f"Tool '{tool_name}' is blocked until the customer verifies the last 4 digits "
            "of their phone on file. Ask for their order number or email, then call "
            "verify_phone_last_four. Do not reveal any order or customer details until verified."
        ),
    }


def verify_phone_last_four(
    bookly_execute,
    session_id: str,
    *,
    last_four: str,
    order_id: str | None = None,
    email: str | None = None,
) -> dict[str, Any]:
    """
    Look up phone on file via Bookly API and compare last 4 digits.
    On success, marks the session verified for that order/email/customer.
    """
    last_four_clean = re.sub(r"\D", "", last_four)[-4:]
    if len(last_four_clean) != 4:
        return {"ok": True, "verified": False, "message": "Need exactly 4 digits."}

    if not order_id and not email:
        return {
            "ok": True,
            "verified": False,
            "message": "Need order_id or email to verify against.",
        }

    phone_on_file: str | None = None
    verified_keys: set[str] = set()

    if order_id:
        result = bookly_execute("get_order", {"id": order_id})
        if not result.get("ok"):
            return {"ok": True, "verified": False, "message": "Order not found."}
        data = result.get("data", {})
        customer = data.get("customer") or {}
        phone_on_file = customer.get("phone")
        verified_keys.add(f"order:{_norm_order(data.get('order_number') or order_id)}")
        if customer.get("email"):
            verified_keys.add(f"email:{_norm_email(customer['email'])}")
        if customer.get("id"):
            verified_keys.add(f"customer:{customer['id']}")

    elif email:
        result = bookly_execute("list_customers", {"email": email})
        if not result.get("ok"):
            return {"ok": True, "verified": False, "message": "Could not look up account."}
        customers = result.get("data") or []
        if not customers:
            return {"ok": True, "verified": False, "message": "No account found for that email."}
        customer = customers[0]
        phone_on_file = customer.get("phone")
        verified_keys.add(f"email:{_norm_email(customer.get('email') or email)}")
        if customer.get("id"):
            verified_keys.add(f"customer:{customer['id']}")

    expected = phone_last_four(phone_on_file)
    if not expected:
        return {"ok": True, "verified": False, "message": "No phone number on file for this account."}

    if last_four_clean != expected:
        return {"ok": True, "verified": False, "message": "Phone digits do not match our records."}

    mark_verified(session_id, verified_keys)
    return {
        "ok": True,
        "verified": True,
        "message": "Verified. You may now look up and share order or customer details for this account.",
    }


def check_tool_allowed(session_id: str, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any] | None:
    """
    Return an error dict if the tool is blocked; None if allowed.
    verify_phone_last_four is always allowed.
    """
    if tool_name not in GATED_TOOLS:
        return None

    required = identity_keys_from_tool(tool_name, tool_input)
    if not required:
        required = {"_any_verified"}

    if is_verified(session_id, required):
        return None

    return verification_required_response(tool_name)
