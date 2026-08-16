"""
Tool dispatch — Bookly MCP manifest + AOP/skills layer.

Business rules: aops/*.md (Agent Operating Policies)
Execution: bookly_client.py (MCP to Bookly, REST fallback)
Skills: skills.py (verify_phone, read_aop)
Hard guardrail: account/PII tools blocked until session.authenticated.
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


def _build_tools_schema() -> list:
    # Trace write tools are orchestrated by app.py; agent may read traces.
    bookly_tools = [
        t for t in _client.anthropic_tools() if t["name"] not in ORCHESTRATION_TRACE_TOOLS
    ]
    return bookly_tools + [READ_AOP_TOOL, VERIFY_PHONE_TOOL]


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
