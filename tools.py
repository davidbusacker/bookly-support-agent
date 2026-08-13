"""
Agent tool layer — Bookly OMS API with phone verification and loyalty gates.
"""

from bookly_client import BooklyClient, tool_result_content
from loyalty import CHECK_LOYALTY_TOOL, gate_early_refund, loyalty_status
from verification import (
    VERIFY_PHONE_TOOL,
    check_tool_allowed,
    init_session,
    verify_phone_last_four,
)

_client = BooklyClient.from_env()

# Manifest tools + custom verification + loyalty check
TOOLS_SCHEMA = _client.anthropic_tools() + [VERIFY_PHONE_TOOL, CHECK_LOYALTY_TOOL]

BOOKLY_AGENT_INSTRUCTIONS = _client.instructions


def _bookly_execute(name: str, tool_input: dict) -> dict:
    """Raw Bookly API call (bypasses gates — internal use only)."""
    return _client.execute_tool(name, tool_input)


def execute_tool(name: str, tool_input: dict, session_id: str | None = None) -> dict:
    """Dispatch a Claude tool_use call with verification and loyalty enforcement."""
    if name == "verify_phone_last_four":
        if not session_id:
            return {"ok": False, "error": "missing_session"}
        return verify_phone_last_four(
            _bookly_execute,
            session_id,
            last_four=str(tool_input.get("last_four", "")),
            order_id=tool_input.get("order_id"),
            email=tool_input.get("email"),
        )

    if name == "check_loyalty_early_refund":
        return loyalty_status(
            _bookly_execute,
            order_id=tool_input.get("order_id"),
            email=tool_input.get("email"),
        )

    if session_id:
        blocked = check_tool_allowed(session_id, name, tool_input)
        if blocked:
            return blocked

    # Loyalty gate: early refund before return is received
    if name == "create_refund":
        loyalty_block = gate_early_refund(_bookly_execute, tool_input)
        if loyalty_block:
            return loyalty_block
        # Tag loyalty-approved refunds for audit trail
        if tool_input.get("return_id"):
            ret_check = _bookly_execute("get_return", {"rma": tool_input["return_id"]})
            if ret_check.get("ok") and not ret_check.get("data", {}).get("received_at"):
                reason = tool_input.get("reason") or "return refund"
                tool_input = {
                    **tool_input,
                    "reason": f"{reason} [loyalty early refund approved]",
                }

    return _client.execute_tool(name, tool_input)


def ping_bookly() -> dict:
    return _client.ping()
