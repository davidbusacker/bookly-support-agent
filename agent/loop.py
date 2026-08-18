"""
Claude tool-use loop — stream one turn, run tools, repeat until end_turn.

Called from pipeline.chat_events() after intent passes and the user message
is appended to session.messages. This is Riley's "brain" for one customer turn.

Flow (run_agent_turn):
  1. Trim old tool outputs so context stays small
  2. Loop: call Claude with AOPs + caller context + full message history
  3. If Claude wants tools → execute each, append results, loop again
  4. If Claude replies with text → stream done, return final reply + tool list
"""

from __future__ import annotations

from typing import Any

from tools import TOOLS_SCHEMA, execute_tool, tool_result_content
from trace_client import trace_message

from agent.config import ANTHROPIC_MODEL, TOOL_RESULT_KEEP, anthropic_client
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
    """Shrink old tool_result payloads so later Claude calls stay small."""
    tool_idxs = []
    # User messages after tool rounds look like: role=user, content=[tool_result, ...]
    for i, msg in enumerate(messages):
        content = msg.get("content")
        if msg.get("role") != "user" or not isinstance(content, list):
            continue
        if any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
            tool_idxs.append(i)
    drop = set(tool_idxs[:-keep_recent])
    if not drop:
        return
    # Keep the last N tool rounds intact; stub out older ones.
    for i in drop:
        compacted = []
        for block in messages[i]["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                compacted.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.get("tool_use_id"),
                        "content": '{"ok": true, "omitted": "prior tool result trimmed"}',
                    }
                )
            else:
                compacted.append(block)
        messages[i] = {"role": "user", "content": compacted}


def _claude_message(history: list, system: str):
    """One streaming Claude call. Yields live text deltas, then the full Message."""
    tool_started = False
    with anthropic_client.messages.stream(
        model=ANTHROPIC_MODEL,
        max_tokens=512,
        system=system,
        tools=TOOLS_SCHEMA,
        messages=history,
    ) as stream:
        # Stop forwarding text once Claude begins a tool_use block (avoids leaking partial JSON).
        for event in stream:
            event_type = getattr(event, "type", None)
            if event_type == "content_block_start":
                block = getattr(event, "content_block", None)
                if block is not None and getattr(block, "type", None) == "tool_use":
                    tool_started = True
            elif event_type == "content_block_delta" and not tool_started:
                delta = getattr(event, "delta", None)
                if delta is not None and getattr(delta, "type", None) == "text_delta":
                    chunk = getattr(delta, "text", "") or ""
                    if chunk:
                        yield {"type": "delta", "text": chunk}
        yield {"type": "_message", "message": stream.get_final_message()}


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
    tool_traces: list[dict[str, Any]] = []  # logged to Bookly trace by pipeline.py
    streamed_text = False

    # Outer loop: each pass is one Claude API call. Re-enter when stop_reason == tool_use.
    while True:
        response = None
        # Inner loop: relay streamed tokens to the browser until the Message is complete.
        for event in _claude_message(history, system_prompt(session)):
            if event.get("type") == "_message":
                response = event["message"]
            else:
                streamed_text = True
                yield event
        if response is None:
            raise RuntimeError("Claude stream ended without a message.")

        # --- Tool path: Claude chose one or more tools instead of (or before) replying ---
        if response.stop_reason == "tool_use":
            history.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                yield {"type": "status", "text": tool_peek_title(block.name)}
                # tools.execute_tool routes to Bookly MCP, read_aop, or verify_phone; ACL checks auth.
                result = execute_tool(
                    block.name,
                    block.input,
                    session_id=session_id,
                    authenticated=session.authenticated,
                )
                if result.get("error") == "authentication_required":
                    yield {"type": "status", "text": f"Blocked {block.name} — not authenticated"}
                # verify_phone may set session.authenticated; get_order may set customer_email.
                maybe_update_identity_from_tool(session, block.name, result)
                if block.name == "verify_phone_last_four" and result.get("verified"):
                    yield {"type": "status", "text": "Authenticated"}
                tools_used.append(block.name)
                tool_traces.append(
                    trace_message(
                        role="tool",
                        speaker="Riley",
                        content=f"Called `{block.name}`",
                        tool_name=block.name,
                        tool_input=block.input,
                        tool_output=result,
                    )
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": tool_result_content(result),
                    }
                )
            # Anthropic expects tool results as the next user message, then Claude is called again.
            history.append({"role": "user", "content": tool_results})
            continue

        # --- Reply path: Claude finished with customer-facing text (stop_reason == end_turn) ---
        final_text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        # If nothing was streamed (e.g. text-only after tools), emit the full reply once.
        if not streamed_text and final_text:
            yield {"type": "delta", "text": final_text}
        history.append({"role": "assistant", "content": response.content})
        yield {
            "type": "_result",
            "reply": final_text,
            "tools": tools_used,
            "traces": tool_traces,
        }
        return
