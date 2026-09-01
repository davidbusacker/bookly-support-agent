"""
Riley's tool-use loop for one turn: stream OpenAI, run tools via tools.py, repeat until text.
Called from pipeline.chat_events after intent passes; compact_history trims old tool JSON first.
"""

from __future__ import annotations

import json
from typing import Any

from llm import openai_tools_from_schema, parse_tool_arguments
from tools import TOOLS_SCHEMA, execute_tool, tool_result_content
from trace_client import trace_message

from agent.config import OPENAI_MODEL, TOOL_RESULT_KEEP, openai_client
from agent.session import Session, maybe_update_identity_from_tool, system_prompt

# Demo UI labels for the peek panel when a tool runs (unlisted tools get a generic label).
TOOL_PEEK_TITLES: dict[str, str] = {
    "read_aop": "Reading a policy",
    "verify_phone_last_four": "Verifying phone (last 4)",
    "get_order": "Looking up the order",
    "list_orders": "Listing past orders",
    "list_customers": "Looking up the customer",
    "get_customer": "Looking up the customer",
    "list_books": "Checking inventory",
    "get_book": "Looking up a title",
    "create_return": "Opening a return",
    "create_refund": "Issuing a refund",
    "update_refund": "Updating a refund",
    "cancel_order": "Cancelling the order",
    "cancel_return": "Cancelling a return",
    "create_ticket": "Escalating to a ticket",
    "list_agent_traces": "Reading prior conversations",
    "get_agent_trace": "Reading a prior conversation",
    "list_policies": "Checking store policy",
    "get_policy": "Checking store policy",
    "list_faqs": "Checking FAQs",
    "list_tickets": "Checking open tickets",
    "reship_order": "Arranging a reship",
    "address_change": "Updating the address",
    "password_reset": "Starting a password reset",
    "receive_return": "Marking a return received",
}


def tool_peek_title(name: str) -> str:
    return TOOL_PEEK_TITLES.get(name, f"Using {name.replace('_', ' ')}")


def compact_history(messages: list, keep_recent: int = TOOL_RESULT_KEEP) -> None:
    """Shrink old tool payloads so later OpenAI calls stay small."""
    round_starts = [
        i
        for i, msg in enumerate(messages)
        if msg.get("role") == "assistant" and msg.get("tool_calls")
    ]
    drop = round_starts[:-keep_recent]
    if not drop:
        return
    stub_ids: set[str] = set()
    for i in drop:
        for call in messages[i].get("tool_calls") or []:
            call_id = call.get("id")
            if call_id:
                stub_ids.add(call_id)
    if not stub_ids:
        return
    for i, msg in enumerate(messages):
        if msg.get("role") == "tool" and msg.get("tool_call_id") in stub_ids:
            messages[i] = {
                **msg,
                "content": '{"ok": true, "omitted": "prior tool result trimmed"}',
            }


def _openai_message(history: list, system: str):
    """One streaming OpenAI call. Yields live text deltas, then the assembled turn."""
    tool_started = False
    text_parts: list[str] = []
    acc: dict[int, dict[str, str]] = {}
    finish_reason = None

    stream = openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        max_tokens=512,
        messages=[{"role": "system", "content": system}, *history],
        tools=openai_tools_from_schema(TOOLS_SCHEMA),
        stream=True,
    )
    for chunk in stream:
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        if choice.finish_reason:
            finish_reason = choice.finish_reason
        delta = choice.delta
        if delta is None:
            continue
        if delta.tool_calls:
            tool_started = True
            for call in delta.tool_calls:
                slot = acc.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
                if call.id:
                    slot["id"] = call.id
                function = call.function
                if function is None:
                    continue
                if function.name:
                    slot["name"] = function.name
                if function.arguments:
                    slot["arguments"] += function.arguments
        elif delta.content and not tool_started:
            text_parts.append(delta.content)
            yield {"type": "delta", "text": delta.content}

    tool_calls = []
    for index in sorted(acc):
        slot = acc[index]
        tool_calls.append(
            {
                "id": slot["id"],
                "name": slot["name"],
                "input": parse_tool_arguments(slot["arguments"]),
                "arguments_raw": slot["arguments"],
            }
        )
    yield {
        "type": "_message",
        "message": {
            "finish_reason": finish_reason,
            "text": "".join(text_parts),
            "tool_calls": tool_calls,
        },
    }


def run_agent_turn(
    history: list,
    session_id: str,
    *,
    session: Session,
):
    """
    Riley's tool-use loop for one customer turn.

    Yields NDJSON-friendly events ({type: delta|status}) for the UI, then a
    final {type: _result} with reply text, tools called, and trace records.
    Mutates `history` in place (same list as session.messages).
    """
    compact_history(history)
    tools_used: list[str] = []
    tool_traces: list[dict[str, Any]] = []
    streamed_text = False

    while True:
        response = None
        for event in _openai_message(history, system_prompt(session)):
            if event.get("type") == "_message":
                response = event["message"]
            else:
                streamed_text = True
                yield event
        if response is None:
            raise RuntimeError("OpenAI stream ended without a message.")

        tool_calls = response.get("tool_calls") or []
        if response.get("finish_reason") == "tool_calls" or tool_calls:
            history.append(
                {
                    "role": "assistant",
                    "content": response.get("text") or None,
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": call["arguments_raw"]
                                or json.dumps(call["input"]),
                            },
                        }
                        for call in tool_calls
                    ],
                }
            )
            for call in tool_calls:
                yield {"type": "status", "text": tool_peek_title(call["name"])}
                result = execute_tool(
                    call["name"],
                    call["input"],
                    session_id=session_id,
                    authenticated=session.authenticated,
                )
                if result.get("error") == "authentication_required":
                    yield {"type": "status", "text": f"Blocked {call['name']} — not authenticated"}
                maybe_update_identity_from_tool(session, call["name"], result)
                if call["name"] == "verify_phone_last_four" and result.get("verified"):
                    yield {"type": "status", "text": "Authenticated"}
                tools_used.append(call["name"])
                tool_traces.append(
                    trace_message(
                        role="tool",
                        speaker="Riley",
                        content=f"Called `{call['name']}`",
                        tool_name=call["name"],
                        tool_input=call["input"],
                        tool_output=result,
                    )
                )
                history.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": tool_result_content(result),
                    }
                )
            continue

        final_text = response.get("text") or ""
        if not streamed_text and final_text:
            yield {"type": "delta", "text": final_text}
        history.append({"role": "assistant", "content": final_text})
        yield {
            "type": "_result",
            "reply": final_text,
            "tools": tools_used,
            "traces": tool_traces,
        }
        return
