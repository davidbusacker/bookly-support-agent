"""
Local skills Riley can call: read_aop and verify_phone_last_four (pass/fail only).
AOPs name these; tools.py dispatches them. Not Bookly MCP and not guardrails.
"""

import re
from typing import Any, Callable

from aop_loader import all_aop_ids


def phone_last_four(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    return digits[-4:] if len(digits) >= 4 else None


def verify_phone_last_four(
    bookly_execute: Callable,
    *,
    last_four: str,
    order_id: str | None = None,
    email: str | None = None,
) -> dict[str, Any]:
    """
    Skill: compare customer-provided last 4 digits to phone on file.
    Returns verified true/false only — never exposes account data.
    """
    last_four_clean = re.sub(r"\D", "", last_four)[-4:]
    if len(last_four_clean) != 4:
        return {"ok": True, "verified": False, "message": "Need exactly 4 digits."}

    if not order_id and not email:
        return {"ok": True, "verified": False, "message": "Need order_id or email."}

    phone_on_file: str | None = None
    customer: dict[str, Any] = {}
    order_number: str | None = None

    if order_id:
        result = bookly_execute("get_order", {"id": order_id})
        if not result.get("ok"):
            return {"ok": True, "verified": False, "message": "Order not found."}
        data = result.get("data") or {}
        customer = data.get("customer") or {}
        phone_on_file = customer.get("phone")
        order_number = data.get("order_number") or order_id

    elif email:
        result = bookly_execute("list_customers", {"email": email})
        if not result.get("ok") or not result.get("data"):
            return {"ok": True, "verified": False, "message": "Account not found."}
        customer = result["data"][0] or {}
        phone_on_file = customer.get("phone")

    expected = phone_last_four(phone_on_file)
    if not expected:
        return {"ok": True, "verified": False, "message": "No phone on file."}

    if last_four_clean != expected:
        return {"ok": True, "verified": False, "message": "Digits do not match."}

    # Orchestrator-only: stripped before Claude sees the tool result.
    session_patch: dict[str, Any] = {}
    found_email = customer.get("email") or email
    if found_email:
        session_patch["customer_email"] = str(found_email).lower()
    if order_number or order_id:
        session_patch["order_id"] = order_number or order_id

    payload: dict[str, Any] = {"ok": True, "verified": True, "message": "Verified."}
    session_patch["authenticated"] = True
    if order_number or order_id:
        session_patch["auth_subject"] = str(order_number or order_id)
    elif found_email:
        session_patch["auth_subject"] = str(found_email).lower()
    payload["_session"] = session_patch
    return payload


def _read_aop_tool() -> dict[str, Any]:
    ids = list(all_aop_ids())
    return {
        "name": "read_aop",
        "description": (
            "Load a Bookly Agent Operating Policy (AOP) by policy_id. "
            "The system prompt catalog lists every AOP with when to load it. "
            "Call before acting on any on-demand policy."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "policy_id": {
                    "type": "string",
                    "enum": ids,
                    "description": "Which AOP to load. Must match an id from the catalog.",
                }
            },
            "required": ["policy_id"],
            "additionalProperties": False,
        },
    }


READ_AOP_TOOL: dict[str, Any] = _read_aop_tool()

VERIFY_PHONE_TOOL: dict[str, Any] = {
    "name": "verify_phone_last_four",
    "description": (
        "Skill referenced by identity-verification AOP. Checks last 4 phone digits against "
        "the account on file. This is the only identity check — email is lookup, not auth. "
        "Returns verified true/false only."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "Bookly order number, e.g. BK-10001."},
            "email": {"type": "string", "description": "Customer email."},
            "last_four": {"type": "string", "description": "Last 4 digits of phone on file."},
        },
        "required": ["last_four"],
        "additionalProperties": False,
    },
}
