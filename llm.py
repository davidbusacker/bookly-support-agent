"""
OpenAI Chat Completions helpers: tool schema conversion, forced-function classifiers, JSON args.
Riley's loop and guardrails.py call these.
"""

from __future__ import annotations

import json
from typing import Any

from openai import OpenAI


def openai_tools_from_schema(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted = []
    for tool in tools:
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description") or "",
                    "parameters": tool.get("input_schema")
                    or {"type": "object", "properties": {}},
                },
            }
        )
    return converted


def parse_tool_arguments(raw: str | None) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def complete_text(
    client: OpenAI,
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 120,
) -> str:
    response = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (response.choices[0].message.content or "").strip()


def complete_tool(
    client: OpenAI,
    *,
    model: str,
    system: str,
    user: str,
    tool: dict[str, Any],
    max_tokens: int = 256,
) -> dict[str, Any]:
    """Force one function call and return its arguments dict."""
    name = tool["name"]
    response = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        tools=openai_tools_from_schema([tool]),
        tool_choice={"type": "function", "function": {"name": name}},
    )
    message = response.choices[0].message
    for call in message.tool_calls or []:
        if call.function and call.function.name == name:
            return parse_tool_arguments(call.function.arguments)
    return {}
