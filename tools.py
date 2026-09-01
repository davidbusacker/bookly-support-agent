"""
The door for every Riley tool call: local skills plus Bookly MCP, with an auth ACL.
Account/PII Bookly tools hard-fail until session.authenticated; public catalog/FAQ tools do not.
"""

from __future__ import annotations

from typing import Any

from aop_loader import read_aop
from bookly_client import BooklyClient, tool_result_content
from skills import READ_AOP_TOOL, VERIFY_PHONE_TOOL, verify_phone_last_four
from trace_client import ORCHESTRATION_TRACE_TOOLS

_client = BooklyClient.from_env()

# Tools the agent may call before phone verification (public / auth bootstrap).
PUBLIC_TOOLS = frozenset(
    {
        "health",
        "meta",
        "read_aop",
        "verify_phone_last_four",
        "list_policies",
        "get_policy",
        "list_faqs",
        "list_books",
        "get_book",
        "get_inventory",
        "create_ticket",  # escalate without account data
    }
)


def requires_authentication(tool_name: str) -> bool:
    """True when the agent must have verified last-4 before calling this tool."""
    if tool_name in PUBLIC_TOOLS:
        return False
    if tool_name in ORCHESTRATION_TRACE_TOOLS:
        return False  # not exposed to the agent anyway
    return True


def get_bookly_client() -> BooklyClient:
    return _client


def reload_bookly_client() -> BooklyClient:
    global _client, TOOLS_SCHEMA, BOOKLY_AGENT_INSTRUCTIONS
    _client = BooklyClient.from_env()
    TOOLS_SCHEMA = _build_tools_schema()
    BOOKLY_AGENT_INSTRUCTIONS = _client.instructions
    return _client


# Live MCP text can still say 30-day windows / non-returnable ebooks. Overlay our 90-day rule.
_RETURN_WINDOW_TOOL_BLURBS: dict[str, str] = {
    "return_eligibility": (
        "Check return eligibility. Bookly support standard window is 90 days from "
        "delivery for every format and reason. Do not tell the customer a type cannot "
        "be returned if delivery was within 90 days. If this tool fails on window or "
        "format inside 90 days, still create_return with override_eligibility true."
    ),
    "get_book": (
        "Fetch a book by UUID or ISBN-13. Confirm format if needed. Format does not "
        "block a return inside the 90-day window."
    ),
}


def _apply_return_window_blurbs(tools: list) -> list:
    patched = []
    for tool in tools:
        name = tool.get("name")
        if name in _RETURN_WINDOW_TOOL_BLURBS:
            tool = {**tool, "description": _RETURN_WINDOW_TOOL_BLURBS[name]}
        patched.append(tool)
    return patched


def _build_tools_schema() -> list:
    # Trace write tools are orchestrated by app.py; agent may read traces.
    bookly_tools = [
        t for t in _client.anthropic_tools() if t["name"] not in ORCHESTRATION_TRACE_TOOLS
    ]
    return _apply_return_window_blurbs(bookly_tools) + [READ_AOP_TOOL, VERIFY_PHONE_TOOL]


TOOLS_SCHEMA = _build_tools_schema()
BOOKLY_AGENT_INSTRUCTIONS = _client.instructions


def execute_tool(
    name: str,
    tool_input: dict,
    session_id: str | None = None,
    *,
    authenticated: bool = False,
) -> dict[str, Any]:
    """Route tool calls. Account tools hard-fail until authenticated=True."""
    if name == "read_aop":
        return read_aop(tool_input.get("policy_id", ""))

    if name == "verify_phone_last_four":
        # Skill may call get_order / list_customers via the Bookly client directly
        # (bypasses this ACL) — only to read phone digits, never returned to Claude.
        return verify_phone_last_four(
            _client.execute_tool,
            last_four=str(tool_input.get("last_four", "")),
            order_id=tool_input.get("order_id"),
            email=tool_input.get("email"),
        )

    if requires_authentication(name) and not authenticated:
        return {
            "ok": False,
            "error": "authentication_required",
            "message": (
                "Blocked: customer is not authenticated. "
                "Call verify_phone_last_four with order_id or email + last_four first. "
                "Do not invent account data."
            ),
            "tool": name,
        }

    return _client.execute_tool(name, tool_input)


def ping_bookly() -> dict:
    return _client.ping()
