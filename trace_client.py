"""
Bookly agent-trace API helpers: start/append/update traces and look up prior chats by email/order.
agent/traces.py uses this on a thread pool; write-trace tools are hidden from Riley's tool menu.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from bookly_client import BooklyClient

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
ORDER_RE = re.compile(r"\bBK-\d{5}\b", re.IGNORECASE)

AGENT_NAME = "Riley"
AGENT_VERSION = "1.1.0"
CHANNEL = "chat"

# Written by orchestration — not exposed to Riley as callable tools.
ORCHESTRATION_TRACE_TOOLS = frozenset(
    {"log_agent_trace", "append_agent_trace_messages", "update_agent_trace"}
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_email(text: str) -> str | None:
    match = EMAIL_RE.search(text or "")
    return match.group(0).lower() if match else None


def extract_order_id(text: str) -> str | None:
    match = ORDER_RE.search(text or "")
    return match.group(0).upper() if match else None


def trace_message(
    *,
    role: str,
    content: str = "",
    speaker: str | None = None,
    metadata: dict[str, Any] | None = None,
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
    tool_output: dict[str, Any] | None = None,
    duration_ms: int | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "role": role,
        "occurred_at": occurred_at or utc_now(),
        "intent_confidence": float((metadata or {}).get("intent_confidence") or 0.0),
        "resolution_confidence": float((metadata or {}).get("resolution_confidence") or 0.0),
    }
    if content:
        msg["content"] = content
    if speaker:
        msg["speaker"] = speaker
    if metadata:
        msg["metadata"] = metadata
    if tool_name:
        msg["tool_name"] = tool_name
    if tool_input is not None:
        msg["tool_input"] = tool_input
    if tool_output is not None:
        msg["tool_output"] = _trim(tool_output)
    if duration_ms is not None:
        msg["duration_ms"] = duration_ms
    return msg


def _trim(value: Any, limit: int = 4000) -> Any:
    text = str(value)
    if len(text) <= limit:
        return value
    return {"truncated": True, "preview": text[:limit]}


class TraceClient:
    """Sync conversation turns to Bookly agent-traces API."""

    def __init__(self, bookly: BooklyClient, *, model: str) -> None:
        self.bookly = bookly
        self.model = model

    def start_trace(
        self,
        *,
        subject: str,
        customer_email: str | None = None,
        order_id: str | None = None,
        intent: str | None = None,
        messages: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "subject": subject,
            "messages": messages,
            "agent_name": AGENT_NAME,
            "agent_version": AGENT_VERSION,
            "model": self.model,
            "channel": CHANNEL,
            "status": "in_progress",
            "started_at": utc_now(),
        }
        if customer_email:
            payload["customer_email"] = customer_email
        if order_id:
            payload["order_id"] = order_id
        if intent:
            payload["intent"] = intent
        if metadata:
            payload["metadata"] = metadata
            if metadata.get("intent_confidence") is not None:
                payload["intent_confidence"] = metadata["intent_confidence"]
        payload.setdefault("intent_confidence", 0.0)
        payload.setdefault("resolution_confidence", 0.0)
        return self.bookly.execute_tool("log_agent_trace", payload)

    def append_messages(
        self,
        trace_id: str,
        messages: list[dict[str, Any]],
        *,
        status: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": trace_id, "messages": messages}
        if status:
            payload["status"] = status
        return self.bookly.execute_tool("append_agent_trace_messages", payload)

    def update_trace(self, trace_id: str, **fields: Any) -> dict[str, Any]:
        payload = {"id": trace_id, **fields}
        return self.bookly.execute_tool("update_agent_trace", payload)

    def list_traces_for_caller(
        self,
        *,
        email: str | None = None,
        order_id: str | None = None,
        limit: int = 5,
        exclude_trace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        # Query email and order separately. Combining them ANDs filters and
        # drops email-only traces (e.g. a prior stock check with no order_id).
        queries: list[dict[str, Any]] = []
        if email:
            queries.append({"limit": limit, "email": email})
        if order_id:
            queries.append({"limit": limit, "order_id": order_id})
        if not queries:
            return []

        seen: set[str] = set()
        traces: list[dict[str, Any]] = []
        for query in queries:
            result = self.bookly.execute_tool("list_agent_traces", query)
            if not result.get("ok"):
                logger.warning("list_agent_traces failed: %s", result)
                continue
            for trace in result.get("data") or []:
                tid = str(trace.get("id") or trace.get("trace_number") or "")
                if not tid or tid in seen:
                    continue
                if exclude_trace_id and trace.get("id") == exclude_trace_id:
                    continue
                seen.add(tid)
                traces.append(trace)
        return traces

    def format_caller_history(self, traces: list[dict[str, Any]]) -> str:
        if not traces:
            return ""
        lines = [
            "## Prior agent conversations with this caller (honour earlier promises)\n"
        ]
        for trace in traces[:3]:
            num = trace.get("trace_number", trace.get("id", "?"))
            subject = trace.get("subject", "")
            summary = trace.get("summary") or "(no summary)"
            intent = trace.get("intent") or "unknown"
            when = trace.get("started_at", "")
            lines.append(f"- **{num}** ({when[:10]}) — {subject}")
            lines.append(f"  Intent: {intent}. Summary: {summary}")
            msgs = trace.get("messages") or []
            for msg in msgs[-4:]:
                role = msg.get("role", "")
                content = (msg.get("content") or "")[:160]
                if content and role in ("customer", "agent"):
                    lines.append(f"  · {role}: {content}")
        return "\n".join(lines)


def extract_trace_id(result: dict[str, Any]) -> str | None:
    if not result.get("ok"):
        return None
    data = result.get("data")
    if isinstance(data, dict) and data.get("id"):
        return str(data["id"])
    return None


def extract_trace_number(result: dict[str, Any]) -> str | None:
    if not result.get("ok"):
        return None
    data = result.get("data")
    if isinstance(data, dict) and data.get("trace_number"):
        return str(data["trace_number"])
    meta = result.get("meta") or {}
    if meta.get("trace_number"):
        return str(meta["trace_number"])
    return None
