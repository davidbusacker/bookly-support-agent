"""
Tool dispatch — Bookly MCP manifest + AOP/skills layer.

Business rules: aops/*.md (Agent Operating Policies)
Execution: bookly_client.py (HTTP to Bookly API)
Skills: skills.py (verify_phone, read_aop)
"""

from aop_loader import read_aop
from bookly_client import BooklyClient, tool_result_content
from skills import READ_AOP_TOOL, VERIFY_PHONE_TOOL, verify_phone_last_four

_client = BooklyClient.from_env()

# 33 Bookly manifest tools + 2 local skills
TOOLS_SCHEMA = _client.anthropic_tools() + [READ_AOP_TOOL, VERIFY_PHONE_TOOL]

BOOKLY_AGENT_INSTRUCTIONS = _client.instructions


def execute_tool(name: str, tool_input: dict, session_id: str | None = None) -> dict:
    """Route tool calls — policies are in AOPs; this only executes."""
    if name == "read_aop":
        return read_aop(tool_input.get("policy_id", ""))

    if name == "verify_phone_last_four":
        return verify_phone_last_four(
            _client.execute_tool,
            last_four=str(tool_input.get("last_four", "")),
            order_id=tool_input.get("order_id"),
            email=tool_input.get("email"),
        )

    return _client.execute_tool(name, tool_input)


def ping_bookly() -> dict:
    return _client.ping()
