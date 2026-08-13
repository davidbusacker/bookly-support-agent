"""
Mocked Bookly backend + Claude tool schemas.

Production would call real OMS / payments / CMS APIs. Here we use in-memory
data so the agent's tool-calling loop is real while the data is fake.
"""

import json

# ---------------------------------------------------------------------------
# Mock data — pretend this is Bookly's order management system
# ---------------------------------------------------------------------------

MOCK_ORDERS = {
    "BK-1001": {
        "status": "Shipped",
        "carrier": "UPS",
        "eta": "Aug 15, 2026",
        "items": ["The Midnight Library"],
        "total": "$18.99",
    },
    "BK-1002": {
        "status": "Processing",
        "carrier": None,
        "eta": None,
        "items": ["Atomic Habits", "Dune"],
        "total": "$34.50",
    },
    "BK-1003": {
        "status": "Delivered",
        "carrier": "USPS",
        "eta": "Aug 10, 2026",
        "items": ["Project Hail Mary"],
        "total": "$16.00",
    },
}

POLICIES = {
    "shipping": (
        "Standard shipping is 5-7 business days and free on orders over $25 "
        "(otherwise $4.99). Express shipping (2-3 business days) is $9.99."
    ),
    "returns": (
        "Items can be returned within 30 days of delivery for a full refund, "
        "as long as the book is unmarked and in resellable condition. "
        "Refunds are issued to the original payment method within 5-7 business days."
    ),
    "password_reset": (
        "To reset your password: go to bookly.com/login, click 'Forgot password', "
        "and follow the emailed link. The link expires after 24 hours."
    ),
}

# ---------------------------------------------------------------------------
# Tool schemas — Claude reads these to decide WHEN and HOW to call tools.
# The model chooses tools freely; guardrails live in the system prompt.
# ---------------------------------------------------------------------------

TOOLS_SCHEMA = [
    {
        "name": "get_order_status",
        "description": (
            "Look up the current status, carrier, ETA, and items for a Bookly order. "
            "Only call when you have a valid order ID from the customer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "Bookly order ID, e.g. BK-1001.",
                }
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "initiate_refund",
        "description": (
            "Start a refund for an order. Requires BOTH order_id AND reason from "
            "the customer — never guess either value."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "Bookly order ID, e.g. BK-1002.",
                },
                "reason": {
                    "type": "string",
                    "description": "Customer's stated reason for the return/refund.",
                },
            },
            "required": ["order_id", "reason"],
        },
    },
    {
        "name": "lookup_policy",
        "description": "Fetch official Bookly policy text for shipping, returns, or password reset.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": ["shipping", "returns", "password_reset"],
                    "description": "Which policy topic to retrieve.",
                }
            },
            "required": ["topic"],
        },
    },
]


def execute_tool(name: str, tool_input: dict) -> dict:
    """
    Dispatch a Claude tool_use call to the mocked backend.
    Returns a plain dict; app.py serializes it to JSON for Claude.
    """
    if name == "get_order_status":
        order_id = tool_input.get("order_id", "").strip().upper()
        order = MOCK_ORDERS.get(order_id)
        if not order:
            return {
                "error": f"No order found with ID {order_id}. Ask the customer to double-check it."
            }
        return {"order_id": order_id, **order}

    if name == "initiate_refund":
        order_id = tool_input.get("order_id", "").strip().upper()
        reason = tool_input.get("reason", "").strip()
        if order_id not in MOCK_ORDERS:
            return {"error": f"No order found with ID {order_id}. Cannot initiate refund."}
        return {
            "status": "refund_initiated",
            "order_id": order_id,
            "reason": reason,
            "refund_id": f"RF-{order_id[-4:]}",
            "eta_business_days": 5,
            "message": "Prepaid return label will be emailed within 24 hours.",
        }

    if name == "lookup_policy":
        topic = tool_input.get("topic", "")
        policy_text = POLICIES.get(topic)
        if not policy_text:
            return {"error": f"No policy found for topic '{topic}'."}
        return {"topic": topic, "policy": policy_text}

    return {"error": f"Unknown tool '{name}'."}


def tool_result_content(result: dict) -> str:
    """Serialize tool output as JSON so Claude parses structured data reliably."""
    return json.dumps(result)
